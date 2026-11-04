# ============================================================
# SectionPipelineProject.py
#
# Shared module for the section pipeline plugins:
#   Stage 1  Multichannel Image Set Previewer (exposure, slide rotation)
#   Stage 2  Section Cropper   (slide -> section images)
#   Stage 3  Section orientation (per-section rotate / flip)
#
# Terms: sample / slide / section.
#   slide   = one FileNameParser image set (all channels of one image)
#   section = one polygon ROI on a slide
#   sample  = free label (animal etc.); stored per slide, overridable
#             per section; used in filenames only (info)
#
# Contents
#   - project.json load / atomic save / merge-on-save (update_project)
#   - slide sync from a FileNameParser result
#   - slide orientation geometry (flip + k*90 CW) for points / rects /
#     processors
#   - global section numbering + filename templates
#   - section crop: view-space polygon -> source region read -> orient
#     -> pad + fill outside
#
# Imported as a normal module (no Fiji menu entry: no '_' in name).
# Pure Jython 2.7 + ImageJ / Bio-Formats.
# ============================================================

import os, re, json, math, time, copy, threading

from java.io        import File, RandomAccessFile
from java.nio.file  import Files, StandardCopyOption
from java.nio       import ByteBuffer, ByteOrder
from jarray         import zeros, array

from ij             import IJ
from ij.gui         import PolygonRoi, Roi, ShapeRoi
from ij.plugin      import RoiEnlarger
from ij.process     import ByteProcessor, ShortProcessor, FloatProcessor

from loci.formats   import FormatTools


SCHEMA          = "section_pipeline/1"
VERSION         = "1.0.2"                  # Slide Image Processor release
PROJECT_DIR     = ".section_pipeline"
PROJECT_FILE    = "project.json"
HISTORY_DIR     = "history"
HISTORY_KEEP    = 20
LOCK_FILE       = "project.lock"   # held while project.json is written

ROTATIONS       = (0, 90, 180, 270)          # stage-1 slide rotation (CW)
NAME_DELIMS     = re.compile(r'[_ \t]')     # FileNameParser delimiters ('-' does not split)
BAD_FILE_CHARS  = re.compile(r'[\\/:*?"<>|]')

DEFAULT_SETTINGS = {
    'filename_template': "{NNN}_{sample}_{slide}_{channel}",
    'padding_frac'     : 0.05,    # of section bbox diagonal (px)
    'padding_mode'     : 'hull',  # hull (convex, no concave part) | outline
    'padding_overlap'  : False,   # True = paddings may overlap neighbours
    'fill_value'       : 0,       # outside padded polygon
    'pixel_size_um'    : None,
}


# ==============================================================
# Project file
# ==============================================================

def project_dir(root):
    return os.path.join(root, PROJECT_DIR)


def project_path(root):
    return os.path.join(project_dir(root), PROJECT_FILE)


def new_project(root):
    now = _now()
    return {
        'schema'     : SCHEMA,
        'root'       : os.path.abspath(root),
        'created'    : now,
        'modified'   : now,
        'stage1'     : {'channel_position': None,
                        'channels'        : [],
                        'channel_params'  : {}},
        'slides'     : {},
        'sections'   : {},
        'orientation': {},
        'settings'   : dict(DEFAULT_SETTINGS),
        'next_section_id': 1,
    }


def load_project(root, create=True):
    """Return the project dict (fresh skeleton if none and create)."""
    p = project_path(root)
    if not os.path.exists(p):
        return new_project(root) if create else None
    f = open(p, 'r')
    try:
        proj = json.load(f)
    finally:
        f.close()
    if proj.get('schema') != SCHEMA:
        raise ValueError("Unknown project schema: %r" % proj.get('schema'))
    _fill_defaults(proj, root)
    return proj


# One writer at a time: a lock shared by every tool in this Fiji (kept across
# reload()) plus a file lock against another Fiji on the same project.
try:
    _SAVE_LOCK
except NameError:
    _SAVE_LOCK = threading.RLock()
_held = threading.local()


class _ProjectLock(object):
    """with _ProjectLock(root): ...  (re-entrant within a thread)"""
    def __init__(self, root):
        self.root = root

    def __enter__(self):
        _SAVE_LOCK.acquire()
        depth = getattr(_held, 'depth', 0)
        _held.depth = depth + 1
        if depth == 0:
            self.raf = self.lock = None
            try:
                d = project_dir(self.root)
                if not os.path.isdir(d):
                    os.makedirs(d)
                self.raf = RandomAccessFile(os.path.join(d, LOCK_FILE), "rw")
                self.lock = self.raf.getChannel().lock()     # waits for another Fiji
            except Exception:
                pass                     # read-only drive etc.: in-process lock only
            _held.outer = self
        return self

    def __exit__(self, *exc):
        _held.depth -= 1
        if _held.depth == 0:
            outer = _held.outer
            try:
                if outer.lock is not None:
                    outer.lock.release()
            finally:
                if outer.raf is not None:
                    outer.raf.close()
        _SAVE_LOCK.release()
        return False


def save_project(root, proj):
    """Atomic write + history snapshot. Overwrites whatever is on disk;
    plugins should normally use update_project() instead."""
    with _ProjectLock(root):
        d = project_dir(root)
        if not os.path.isdir(d):
            os.makedirs(d)
        proj['modified'] = _now()
        p   = project_path(root)
        # own temp name per write (a shared one could be truncated by another
        # writer while this one is still writing)
        tmp = "%s.%d.%d.tmp" % (p, threading.current_thread().ident or 0,
                                int(time.time() * 1000))
        f = open(tmp, 'w')
        try:
            json.dump(proj, f, indent=1, sort_keys=True)
        finally:
            f.close()
        if os.path.exists(p):
            _snapshot(root, p)
        try:
            Files.move(File(tmp).toPath(), File(p).toPath(),
                       StandardCopyOption.REPLACE_EXISTING,
                       StandardCopyOption.ATOMIC_MOVE)
        except Exception:
            Files.move(File(tmp).toPath(), File(p).toPath(),
                       StandardCopyOption.REPLACE_EXISTING)
        return p


def update_project(root, fn):
    """Load the CURRENT file, apply fn(proj) (which writes only the
    fields its plugin owns), save. Lets several plugins share one file
    without clobbering each other. Returns the saved project. The whole
    read-modify-write runs under the project lock, so two saves never
    interleave or lose each other's changes."""
    with _ProjectLock(root):
        proj = load_project(root, create=True)
        fn(proj)
        save_project(root, proj)
        return proj


def _snapshot(root, p):
    h = os.path.join(project_dir(root), HISTORY_DIR)
    if not os.path.isdir(h):
        os.makedirs(h)
    stamp = time.strftime("%Y%m%d_%H%M%S") + "_%03d" % (int(time.time() * 1000) % 1000)
    dst = os.path.join(h, "project_%s.json" % stamp)
    try:
        Files.copy(File(p).toPath(), File(dst).toPath(),
                   StandardCopyOption.REPLACE_EXISTING)
    except Exception:
        pass
    snaps = sorted(n for n in os.listdir(h) if n.startswith("project_"))
    for n in snaps[:-HISTORY_KEEP]:
        try: os.remove(os.path.join(h, n))
        except Exception: pass


def _fill_defaults(proj, root):
    proj.setdefault('stage1', {}).setdefault('channel_params', {})
    proj['stage1'].setdefault('channels', [])
    proj['stage1'].setdefault('channel_position', None)
    proj.setdefault('slides', {})
    proj.setdefault('sections', {})
    proj.setdefault('orientation', {})
    st = proj.setdefault('settings', {})
    for k, v in DEFAULT_SETTINGS.items():
        st.setdefault(k, v)
    if 'next_section_id' not in proj:
        ids = [int(s[4:]) for s in proj['sections'] if s.startswith('sec_')
               and s[4:].isdigit()]
        proj['next_section_id'] = (max(ids) + 1) if ids else 1
    proj['root'] = os.path.abspath(root)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class WorkEta(object):
    """Time left from the real amount of work (bytes, pixels...), shared by
    the progress dialogs. set_total() when the work starts, add() as each
    piece finishes (thread-safe); text() counts down between pieces."""

    def __init__(self):
        self.total, self.done, self.t0, self.t_last = 0.0, 0.0, None, None
        self.lock = threading.Lock()

    def set_total(self, units):
        self.total, self.done = float(max(0, units)), 0.0
        self.t0 = self.t_last = time.time()

    def add(self, units):
        with self.lock:
            self.done += max(0, units)
            self.t_last = time.time()

    def fraction(self):
        return min(1.0, self.done / self.total) if self.total > 0 else None

    def text(self):
        if self.total <= 0 or self.t0 is None:
            return ""
        if self.done <= 0 or self.t_last - self.t0 < 1.0:
            return "estimating time left..."
        rate = self.done / (self.t_last - self.t0)            # units per second
        left = (self.total - self.done) / rate - (time.time() - self.t_last)
        if self.done >= self.total:
            return ""
        return "about %s left" % _duration(max(left, 1.0))


def _duration(s):
    s = int(round(s))
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min %02d s" % (s // 60, s % 60)
    return "%d h %02d min" % (s // 3600, (s % 3600) // 60)


# Save-status wording shared by all three tools (Project group, top right)
UNSAVED_TEXT = "Unsaved changes"

def saved_text(modified=None):
    """'Saved MM-DD HH:MM' from an ISO time (default: now), or
    'Not saved yet' when there is none."""
    if modified is False:
        return "Not saved yet"
    t = modified or _now()
    return "Saved %s %s" % (t[5:10], t[11:16])


# ==============================================================
# Paths (stored relative to root so the project survives moving
# between PCs / Dropbox locations)
# ==============================================================

def rel_path(root, path):
    try:
        r = os.path.relpath(path, root)
    except ValueError:            # different drive
        return os.path.abspath(path)
    return r.replace('\\', '/')


def abs_path(root, rel):
    if os.path.isabs(rel):
        return rel
    return os.path.normpath(os.path.join(root, rel))


# ==============================================================
# Slides
# ==============================================================

def new_slide(sample=""):
    return {
        'channels': {},          # ch -> rel path
        'width'   : None,        # source (unrotated) size
        'height'  : None,
        'rotation': 0,           # stage 1, CW degrees in ROTATIONS
        'flip_h'  : False,       # stage 1, applied before rotation
        'order'   : None,        # stage 2 slide order
        'sample'  : sample,
        'excluded': False,
        'reviewed': False,
        'missing' : False,
    }


def sync_slides(proj, parse_result, root, dims=None):
    """Register slides from a FileNameParser result. New slides are
    appended at the end of the order; slides no longer on disk are
    flagged 'missing' (never deleted, so user work is kept). Slides that are
    only left out of parse_result (files still on disk) are not touched.

    dims: optional {slide_id: (w, h)} of the source images.
    Returns (added_ids, missing_ids)."""
    slides = proj['slides']
    seen   = set()
    added  = []
    max_order = max([s['order'] for s in slides.values()
                     if s.get('order') is not None] or [-1])
    for sid in sorted(parse_result['sets'].keys()):
        seen.add(sid)
        s = slides.get(sid)
        if s is None:
            s = new_slide()
            max_order += 1
            s['order'] = max_order
            slides[sid] = s
            added.append(sid)
        s['missing']  = False
        s['channels'] = dict((ch, rel_path(root, p)) for ch, p in
                             parse_result['sets'][sid].items())
        if dims and sid in dims:
            s['width'], s['height'] = int(dims[sid][0]), int(dims[sid][1])
    # a slide left out of this parse result (e.g. unchecked in the stage-1
    # image list) keeps its state; only one whose files are gone is missing
    missing = []
    for sid, s in slides.items():
        if sid in seen:
            continue
        on_disk = any(os.path.exists(abs_path(root, rel.split("|")[0]))
                      for rel in s.get("channels", {}).values())
        if on_disk:
            s["missing"] = False
        elif not s.get("missing"):
            s["missing"] = True
            missing.append(sid)
    st1 = proj['stage1']
    if parse_result.get('channels'):
        st1['channels'] = list(parse_result['channels'])
    if parse_result.get('channel_position') is not None:
        st1['channel_position'] = parse_result['channel_position']
    return added, missing


def ordered_slide_ids(proj, include_excluded=False):
    out = []
    for sid, s in proj['slides'].items():
        if s.get('missing'):
            continue
        if s.get('excluded') and not include_excluded:
            continue
        out.append(sid)
    out.sort(key=lambda sid: (proj['slides'][sid].get('order', 0), sid))
    return out


def set_slide_rotation(proj, slide_ids, rotation=None, flip_h=None):
    """Group-apply stage-1 orientation. Returns slides whose sections
    must be re-checked (their view coordinates changed)."""
    if rotation is not None and rotation not in ROTATIONS:
        raise ValueError("rotation must be one of %r" % (ROTATIONS,))
    touched = []
    for sid in slide_ids:
        s = proj['slides'][sid]
        changed = False
        if rotation is not None and s.get('rotation', 0) != rotation:
            s['rotation'] = rotation; changed = True
        if flip_h is not None and bool(s.get('flip_h')) != bool(flip_h):
            s['flip_h'] = bool(flip_h); changed = True
        if changed:
            touched.append(sid)
    return touched


def set_field(proj, kind, ids, field, value):
    """Group edit: kind 'slides' | 'sections'."""
    table = proj[kind]
    for i in ids:
        table[i][field] = value


# ==============================================================
# Slide orientation geometry
#
# view = rotateCW^k( flipH(source) ),  k = rotation / 90
# Coordinates are continuous (pixel edges), so rects map exactly.
# ==============================================================

def view_size(w, h, rotation):
    return (h, w) if rotation in (90, 270) else (w, h)


def src_to_view(x, y, w, h, rotation, flip_h):
    if flip_h:
        x = w - x
    cw, ch = w, h
    for _ in range((rotation // 90) % 4):
        x, y = ch - y, x             # CW in a (cw, ch) image
        cw, ch = ch, cw
    return x, y


def view_to_src(x, y, w, h, rotation, flip_h):
    k = (rotation // 90) % 4
    cw, ch = view_size(w, h, rotation)
    for _ in range(k):               # undo CW: inverse = CCW
        x, y = y, cw - x
        cw, ch = ch, cw
    if flip_h:
        x = w - x
    return x, y


def view_rect_to_src(rx, ry, rw, rh, w, h, rotation, flip_h):
    """Integer view rect -> integer source rect (same pixels)."""
    xa, ya = view_to_src(rx, ry, w, h, rotation, flip_h)
    xb, yb = view_to_src(rx + rw, ry + rh, w, h, rotation, flip_h)
    x0, x1 = min(xa, xb), max(xa, xb)
    y0, y1 = min(ya, yb), max(ya, yb)
    return int(x0), int(y0), int(x1 - x0), int(y1 - y0)


def orient_processor(ip, rotation, flip_h):
    """Apply slide orientation to an ImageProcessor (returns new/same)."""
    if flip_h:
        ip = ip.duplicate()
        ip.flipHorizontal()
    for _ in range((rotation // 90) % 4):
        ip = ip.rotateRight()
    return ip


def reorient_polygon(poly, w, h, old, new):
    """Map a view-space polygon drawn under slide orientation `old`
    (rotation, flip_h) to the view space of orientation `new`."""
    out = []
    for x, y in poly:
        sx, sy = view_to_src(x, y, w, h, old[0], old[1])
        out.append(list(src_to_view(sx, sy, w, h, new[0], new[1])))
    return out


def compose_orientation(rotation, flip_h, extra_rot, extra_flip_h):
    """Stage-3 per-section orientation applied on top of stage 1, for
    90-degree multiples. Returns (rotation, flip_h) of the result."""
    # flip then rot r1, then flip f2 then rot r2.
    # flipH . rotCW(r) == rotCW(-r) . flipH
    r1 = rotation % 360
    if extra_flip_h:
        r1 = (-r1) % 360
        flip_h = not flip_h
    return (r1 + extra_rot) % 360, flip_h


# ==============================================================
# Sections
# ==============================================================

def new_section(proj, slide_id, polygon, origin="auto", order=None):
    sec_id = "sec_%05d" % proj['next_section_id']
    proj['next_section_id'] += 1
    if order is None:
        orders = [s['order'] for s in proj['sections'].values()
                  if s['slide'] == slide_id]
        order = (max(orders) + 1) if orders else 0
    proj['sections'][sec_id] = {
        'slide'   : slide_id,
        'polygon' : [[float(x), float(y)] for x, y in polygon],  # view px
        'padding' : None,        # None = auto (padding_frac * diagonal)
        'order'   : order,       # within slide
        'sample'  : None,        # None = inherit slide sample
        'excluded': False,
        'origin'  : origin,      # auto | edited | manual
    }
    return sec_id


def delete_sections(proj, sec_ids):
    for i in sec_ids:
        proj['sections'].pop(i, None)
        proj['orientation'].pop(i, None)


def slide_section_ids(proj, slide_id, include_excluded=True):
    ids = [i for i, s in proj['sections'].items()
           if s['slide'] == slide_id and
           (include_excluded or not s.get('excluded'))]
    ids.sort(key=lambda i: (proj['sections'][i]['order'], i))
    return ids


def reorder_slide_sections(proj, slide_id, ordered_ids):
    """ordered_ids: section ids of this slide in the new order."""
    for n, i in enumerate(ordered_ids):
        proj['sections'][i]['order'] = n


def section_sample(proj, sec_id):
    s = proj['sections'][sec_id]
    if s.get('sample'):
        return s['sample']
    return proj['slides'][s['slide']].get('sample') or ""


def polygon_bounds(poly):
    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def section_padding(proj, sec_id):
    """Effective padding (px): explicit value or padding_frac * bbox
    diagonal of the (unpadded) polygon."""
    s = proj['sections'][sec_id]
    if s.get('padding') is not None:
        return int(round(s['padding']))
    x0, y0, x1, y1 = polygon_bounds(s['polygon'])
    diag = math.hypot(x1 - x0, y1 - y0)
    return int(round(diag * float(proj['settings']['padding_frac'])))


def auto_order(polys, row_tol_frac=0.5, right_to_left=False):
    """Row-major reading order for a list of polygons (view coords).
    Rows: centroids whose y differ less than row_tol_frac * median
    height. Returns list of indices into polys."""
    if not polys:
        return []
    info = []
    for i, p in enumerate(polys):
        x0, y0, x1, y1 = polygon_bounds(p)
        info.append((i, (x0 + x1) / 2.0, (y0 + y1) / 2.0, y1 - y0))
    heights = sorted(t[3] for t in info)
    tol = row_tol_frac * heights[len(heights) // 2]
    info.sort(key=lambda t: t[2])
    rows, cur, cur_y = [], [], None
    for t in info:
        if cur and abs(t[2] - cur_y) > tol:
            rows.append(cur); cur = []
        cur.append(t)
        cur_y = sum(u[2] for u in cur) / len(cur)
    rows.append(cur)
    out = []
    for r in rows:
        r.sort(key=lambda t: t[1], reverse=right_to_left)
        out.extend(t[0] for t in r)
    return out


# ==============================================================
# Numbering + filenames
# ==============================================================

def numbered_sections(proj):
    """Global running number over all non-excluded sections:
    slide order, then section order within the slide.
    Returns list of dicts: sec_id, num, NNN, slide, sample, sidx, sec."""
    rows = []
    for sid in ordered_slide_ids(proj):
        for k, sec_id in enumerate(slide_section_ids(proj, sid, False)):
            rows.append({'sec_id': sec_id, 'slide': sid,
                         'sample': section_sample(proj, sec_id),
                         'sec': k + 1})
    width = max(3, len(str(len(rows))))
    per_sample = {}
    for n, r in enumerate(rows):
        r['num'] = n + 1
        r['NNN'] = str(n + 1).zfill(width)
        per_sample[r['sample']] = per_sample.get(r['sample'], 0) + 1
        r['sidx'] = str(per_sample[r['sample']]).zfill(3)
    return rows


TEMPLATE_FIELDS = ('NNN', 'sample', 'slide', 'channel', 'sidx', 'sec')
_TPL_RE = re.compile(r'\{(\w+)\}')


def render_filename(template, row, channel, ext=".tif", suffix=""):
    def rep(m):
        k = m.group(1)
        if k == 'channel':
            v = channel
        elif k == 'sec':
            v = "%02d" % row['sec']
        elif k in row:
            v = row[k]
        else:
            raise ValueError("Unknown template field {%s}. Use: %s" %
                             (k, ", ".join(TEMPLATE_FIELDS)))
        return BAD_FILE_CHARS.sub('-', unicode(v)) if v != "" else "NA"
    return _TPL_RE.sub(rep, template) + suffix + ext


def check_template(template):
    """Returns list of problems (empty = ok)."""
    probs = []
    fields = _TPL_RE.findall(template)
    for f in fields:
        if f not in TEMPLATE_FIELDS:
            probs.append("unknown field {%s}" % f)
    if 'NNN' not in fields:
        probs.append("{NNN} missing: names may collide")
    if 'channel' not in fields:
        probs.append("{channel} missing: channels will overwrite each other")
    return probs


def label_warnings(label):
    """FileNameParser splits on _ and spaces; a label containing them
    changes the token count and can break channel auto-detection."""
    if label and NAME_DELIMS.search(label):
        return "'%s' contains _ or space (splits filename tokens)" % label
    return None


# ==============================================================
# Section crop
# ==============================================================

def _bytes_to_processor(buf, pt, w, h, little_endian):
    n  = w * h
    bo = ByteOrder.LITTLE_ENDIAN if little_endian else ByteOrder.BIG_ENDIAN
    if pt in (FormatTools.UINT8, FormatTools.INT8):
        return ByteProcessor(w, h, buf, None)
    if pt in (FormatTools.UINT16, FormatTools.INT16):
        a = zeros(n, 'h')
        bb = ByteBuffer.wrap(buf); bb.order(bo); bb.asShortBuffer().get(a)
        return ShortProcessor(w, h, a, None)
    if pt == FormatTools.FLOAT:
        a = zeros(n, 'f')
        bb = ByteBuffer.wrap(buf); bb.order(bo); bb.asFloatBuffer().get(a)
        return FloatProcessor(w, h, a)
    raise ValueError("Unsupported pixel type %r" % pt)


def poly_roi(poly):
    return PolygonRoi(array([float(p[0]) for p in poly], "f"),
                      array([float(p[1]) for p in poly], "f"), Roi.POLYGON)


def poly_area(poly):
    a = 0.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]; x1, y1 = poly[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


def poly_centroid(poly):
    x0, y0, x1, y1 = polygon_bounds(poly)
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def simplify_polygon(poly, tol):
    """Douglas-Peucker on a closed polygon (iterative)."""
    n = len(poly)
    if n <= 4 or tol <= 0:
        return [list(p) for p in poly]
    # split at the two farthest-apart points (first + farthest from it)
    far = max(range(n), key=lambda i: (poly[i][0] - poly[0][0]) ** 2 +
                                      (poly[i][1] - poly[0][1]) ** 2)
    keep = [False] * n
    keep[0] = keep[far] = True
    stack = [(0, far), (far, n)]
    while stack:
        a, b = stack.pop()
        pa = poly[a]; pb = poly[b % n]
        dx = pb[0] - pa[0]; dy = pb[1] - pa[1]
        L = math.hypot(dx, dy) or 1e-9
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            p = poly[i]
            d = abs(dy * (p[0] - pa[0]) - dx * (p[1] - pa[1])) / L
            if d > best:
                best, bi = d, i
        if bi >= 0 and best > tol:
            keep[bi] = True
            stack.append((a, bi)); stack.append((bi, b))
    out = [list(poly[i]) for i in range(n) if keep[i]]
    return out if len(out) >= 3 else [list(p) for p in poly]


def convex_hull(points):
    pts = sorted(set((float(p[0]), float(p[1])) for p in points))
    if len(pts) < 3:
        return [list(p) for p in pts]
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]


def padded_polygon(poly, pad, max_dim=1200, mode='outline'):
    """Polygon grown by pad px; mode 'hull' grows its convex hull
    (no concave parts), 'outline' follows the polygon. The dilation runs on a copy scaled so
    its bbox is <= max_dim (fast, bounded memory); error < 1/scale px."""
    if mode == 'hull':
        poly = convex_hull(poly)
    if pad <= 0:
        return [list(p) for p in poly]
    x0, y0, x1, y1 = polygon_bounds(poly)
    span = max(x1 - x0, y1 - y0) + 2.0 * pad
    k = min(1.0, float(max_dim) / span) if span > 0 else 1.0
    m = pad + 2.0
    roi = poly_roi([[(p[0] - x0 + m) * k, (p[1] - y0 + m) * k] for p in poly])
    big = RoiEnlarger.enlarge(roi, pad * k)
    if isinstance(big, ShapeRoi):
        parts = list(big.getRois())
        big = max(parts, key=lambda r: r.getBounds().width * r.getBounds().height)
    fp = big.getFloatPolygon()
    out = [[fp.xpoints[i] / k + x0 - m, fp.ypoints[i] / k + y0 - m]
           for i in range(fp.npoints)]
    return simplify_polygon(out, 1.0 / k)


def _path(poly):
    from java.awt.geom import Path2D
    p = Path2D.Double()
    p.moveTo(poly[0][0], poly[0][1])
    for x, y in poly[1:]:
        p.lineTo(x, y)
    p.closePath()
    return p


def _largest_subpath(area):
    from java.awt.geom import PathIterator
    out, cur = [], []
    it = area.getPathIterator(None)
    c = zeros(6, 'd')
    while not it.isDone():
        t = it.currentSegment(c)
        if t == PathIterator.SEG_MOVETO:
            if len(cur) >= 3: out.append(cur)
            cur = [[c[0], c[1]]]
        elif t == PathIterator.SEG_LINETO:
            cur.append([c[0], c[1]])
        elif t == PathIterator.SEG_CLOSE:
            if len(cur) >= 3: out.append(cur)
            cur = []
        it.next()
    if len(cur) >= 3:
        out.append(cur)
    return max(out, key=poly_area) if out else None


def padded_shape(proj, sec_id, max_dim=1200):
    """The section's crop outline: padded polygon (padding mode) with the
    parts given up where it overlaps a neighbour's padding. In an overlap
    the border lies at equal distance from the two sections, so paddings
    of neighbouring sections never overlap (unless the 'padding_overlap'
    setting is on). Excluded sections do not count as neighbours."""
    s = proj['sections'][sec_id]
    mode = padding_mode(proj)
    pad = section_padding(proj, sec_id)
    pp = padded_polygon(s['polygon'], pad, max_dim=max_dim, mode=mode)
    if proj['settings'].get('padding_overlap'):
        return pp
    x0, y0, x1, y1 = polygon_bounds(pp)
    nbs = []
    for j in slide_section_ids(proj, s['slide'], include_excluded=False):
        if j == sec_id:
            continue
        q = proj['sections'][j]['polygon']
        pj = section_padding(proj, j)
        a0, b0, a1, b1 = polygon_bounds(q)
        if a0 - pj < x1 and a1 + pj > x0 and b0 - pj < y1 and b1 + pj > y0:
            nbs.append((q, pj))
    if not nbs:
        return pp
    try:
        return _voronoi_clip(s['polygon'], pp, nbs, mode, max_dim) or pp
    except Exception as e:
        IJ.log("[padding] overlap split skipped for %s: %s" % (sec_id, e))
        return pp


def _voronoi_clip(poly, pp, nbs, mode, max_dim):
    """Remove from pp every point that lies in a neighbour's padding AND
    is closer to that neighbour than to this section (raster, scaled so
    the working box is <= max_dim px)."""
    from ij.plugin.filter import EDM, ThresholdToSelection
    from ij.process import Blitter, ImageProcessor
    boxes = [polygon_bounds(pp)] + [polygon_bounds(q) for q, _ in nbs]
    X0 = min(b[0] for b in boxes); Y0 = min(b[1] for b in boxes)
    X1 = max(b[2] for b in boxes); Y1 = max(b[3] for b in boxes)
    k = min(1.0, float(max_dim) / max(X1 - X0, Y1 - Y0, 1))
    W, H = int((X1 - X0) * k) + 3, int((Y1 - Y0) * k) + 3
    tr = lambda pg: [[(x - X0) * k + 1, (y - Y0) * k + 1] for x, y in pg]

    def dist_to(pg):
        """Float image: distance (raster px) to polygon pg (0 inside)."""
        m = ByteProcessor(W, H)
        m.setValue(255); m.fill()
        m.setValue(0); m.fill(poly_roi(tr(pg)))
        return EDM().makeFloatEDM(m, 0, False)

    keep = ByteProcessor(W, H)
    keep.setValue(255)
    keep.fill(poly_roi(tr(pp)))
    d_self = dist_to(poly)
    for q, pj in nbs:
        d_nb = dist_to(q)
        d_base = dist_to(convex_hull(q)) if mode == 'hull' else d_nb
        d_base.setThreshold(-1.0, pj * k, ImageProcessor.NO_LUT_UPDATE)
        in_pad = d_base.createMask()                 # inside the neighbour's padding
        closer = d_nb.duplicate()
        closer.copyBits(d_self, 0, 0, Blitter.SUBTRACT)   # d_nb - d_self
        closer.setThreshold(-1e30, -1e-6, ImageProcessor.NO_LUT_UPDATE)
        lose = closer.createMask()                   # closer to the neighbour
        lose.copyBits(in_pad, 0, 0, Blitter.AND)
        keep.copyBits(lose, 0, 0, Blitter.SUBTRACT)
    keep.setThreshold(255, 255, ImageProcessor.NO_LUT_UPDATE)
    roi = ThresholdToSelection().convert(keep)
    if roi is None:
        return None
    rois = list(roi.getRois()) if isinstance(roi, ShapeRoi) else [roi]
    best = None
    for r in rois:
        fp = r.getFloatPolygon()
        pg = [[fp.xpoints[i], fp.ypoints[i]] for i in range(fp.npoints)]
        if len(pg) >= 3 and (best is None or poly_area(pg) > poly_area(best)):
            best = pg
    if best is None:
        return None
    out = [[(x - 1) / k + X0, (y - 1) / k + Y0] for x, y in best]
    return simplify_polygon(out, 1.0 / k)


def section_roi(proj, sec_id):
    """Padded section ROI in slide VIEW coordinates."""
    return poly_roi(padded_shape(proj, sec_id)), section_padding(proj, sec_id)


def padding_mode(proj):
    return proj['settings'].get('padding_mode') or 'hull'


def section_crop_rect(roi, view_w, view_h):
    """Bounding box of the padded ROI clipped to the slide (view)."""
    b = roi.getBounds()
    x0 = max(0, b.x); y0 = max(0, b.y)
    x1 = min(view_w, b.x + b.width); y1 = min(view_h, b.y + b.height)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def read_view_region(reader, src_w, src_h, rotation, flip_h, rect):
    """Read a VIEW-space rect from a source reader (level 0) and return
    the processor already in view orientation."""
    sx, sy, sw, sh = view_rect_to_src(rect[0], rect[1], rect[2], rect[3],
                                      src_w, src_h, rotation, flip_h)
    buf = reader.openBytes(0, sx, sy, sw, sh)
    ip  = _bytes_to_processor(buf, reader.getPixelType(), sw, sh,
                              reader.isLittleEndian())
    return orient_processor(ip, rotation, flip_h)


def crop_section(proj, sec_id, readers, mask_outside=True):
    """Crop one section from all channels.

    readers: {channel: Bio-Formats reader opened on that channel's
              level-0 file (source TIFF or workspace L0)}
    Returns ({channel: processor}, info dict)."""
    s     = proj['sections'][sec_id]
    slide = proj['slides'][s['slide']]
    rot   = slide.get('rotation', 0)
    fh    = bool(slide.get('flip_h'))
    out, info = {}, {}
    for ch, reader in readers.items():
        w, h = reader.getSizeX(), reader.getSizeY()
        vw, vh = view_size(w, h, rot)
        roi, pad = section_roi(proj, sec_id)
        rect = section_crop_rect(roi, vw, vh)
        if rect is None:
            raise ValueError("%s lies outside slide %s" % (sec_id, s['slide']))
        ip = read_view_region(reader, w, h, rot, fh, rect)
        if mask_outside:
            b = roi.getBounds()
            roi.setLocation(b.x - rect[0], b.y - rect[1])
            ip.setValue(float(proj['settings']['fill_value']))
            ip.fillOutside(roi)
        out[ch] = ip
        info = {'rect': rect, 'padding': pad, 'rotation': rot, 'flip_h': fh}
    return out, info


def slide_signature(proj, sid):
    """Changes whenever any section geometry / padding / exclusion on the
    slide changes (padding outlines depend on the neighbours)."""
    out = [padding_mode(proj), proj['settings'].get('padding_frac'),
           bool(proj['settings'].get('padding_overlap'))]
    for i in slide_section_ids(proj, sid):
        s = proj['sections'][i]
        p = s['polygon']
        out.append((i, len(p), tuple(p[0]), tuple(p[len(p) // 2]),
                    s.get('padding'), bool(s.get('excluded'))))
    return tuple(out)
