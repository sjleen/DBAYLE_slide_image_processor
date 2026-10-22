# ============================================================
# SectionOrienterCore.py  (stage 3: Section Orienter)
#
# Per-section orientation on top of stage 1: flip_h + rotation
# (degrees clockwise, any angle; 90-multiples are exact).
#   oriented = rotateCW(rot)( flipH(section) )   (same convention
#   as SectionPipelineProject slide orientation)
#
# Sources
#   project mode: a Section Cropper export folder (sections.csv);
#                 tags in project.json['orientation'][sec_id]
#   folder mode : any image folder; files grouped into sections by
#                 FileNameParser (channel token); tags in
#                 <folder>/orientation.json
# Output: a new folder mirroring the source tree, transformed files
# for tagged sections, copies for the rest.
# ============================================================

import os, re, csv, json, math, time, shutil, codecs

from ij           import IJ, ImagePlus, ImageStack, CompositeImage
from ij.io        import FileSaver
from ij.process   import ImageProcessor, ByteProcessor, ColorProcessor

import SectionPipelineProject as spp
import FileNameParser as fnp


IMG_EXT = ('.tif', '.tiff', '.jpg', '.jpeg', '.png')
TAGS_FILE = "orientation.json"
IDENTITY = {'rot': 0.0, 'flip_h': False}


def norm_rot(r):
    r = float(r) % 360.0
    if r > 180.0:
        r -= 360.0
    if abs(r) < 1e-9:
        r = 0.0
    return r


def is_identity(t):
    return not t.get('flip_h') and norm_rot(t.get('rot', 0)) == 0.0


def tag_text(t):
    parts = []
    if t.get('flip_h'):
        parts.append("F")
    r = norm_rot(t.get('rot', 0))
    if r:
        parts.append(("R%g" % r))
    return " ".join(parts)


# ==============================================================
# Loading
# ==============================================================

class Item(object):
    def __init__(self, key, label, files, sort_key, sample=""):
        self.key = key            # sec_id (project) or set id (folder)
        self.label = label
        self.files = files        # absolute paths
        self.sort_key = sort_key
        self.sample = sample or "(all)"
        self.tags = dict(IDENTITY)
        self.validated = False
        self.base = None          # export folder this item came from

    def modified(self):
        return not is_identity(self.tags)

    def set_saved(self, t):
        self.tags = {'rot': norm_rot(t.get('rot', 0)), 'flip_h': bool(t.get('flip_h'))}
        self.validated = bool(t.get('validated'))

    def record(self):
        d = dict(self.tags)
        d['validated'] = self.validated
        return d

    def worth_saving(self):
        return self.modified() or self.validated


def _kind_label(base, path):
    rel = os.path.relpath(path, base).replace('\\', '/')
    d = rel.split('/')[0] if '/' in rel else "."      # the type folder, not the subfolders below it
    stem = os.path.splitext(os.path.basename(rel))[0]
    ch = re.split(r"[_\-\s]+", stem)[-1]
    return "%s/%s" % (d, ch)


def find_project_root(folder):
    """Walk up from an export folder to a project that lists it."""
    d = os.path.abspath(folder)
    for _ in range(8):
        p = os.path.dirname(d) if _ else d
        if os.path.exists(spp.project_path(p)):
            return p
        if p == os.path.dirname(p):
            break
        d = p
    return None


def _load_json(path):
    f = open(path, 'r')
    try:
        return json.load(f)
    finally:
        f.close()


def load_export(root, proj, export_dir):
    f = open(os.path.join(export_dir, "sections.csv"), 'rb')
    try:     # csv gives utf-8 bytes; names may be non-ASCII
        rows = [dict((k, v.decode("utf-8") if isinstance(v, str) else v)
                     for k, v in r.items()) for r in csv.DictReader(f)]
    finally:
        f.close()
    items = []
    for r in rows:
        files = [os.path.join(export_dir, f) for f in (r.get('files') or "").split(';') if f]
        files = [f for f in files if os.path.exists(f)]
        if not files:
            continue
        it = Item(r['sec_id'], "%s %s" % (r['NNN'], r.get('sample', '')),
                  files, (int(r['NNN']), r['sec_id']), r.get('sample', ''))
        it.base = export_dir
        t = (proj or {}).get('orientation', {}).get(r['sec_id'])
        if t:
            it.set_saved(t)
        items.append(it)
    items.sort(key=lambda i: i.sort_key)
    return items


def is_cropper_export(d):
    """A Section Cropper export not yet re-oriented (an 'oriented in place'
    folder also holds the Cropper md, but its files are already turned)."""
    if not os.path.exists(os.path.join(d, "sections.csv")):
        return False
    try:
        names = os.listdir(d)
        if any(f.startswith("SectionOrienter__") for f in names):
            return False
        return any(f.startswith("SectionCropper__") and f.endswith("_export parameters.md")
                   for f in names)
    except Exception:
        return False


def find_exports(folder, max_depth=3):
    """Section Cropper export folders at or below folder (stage-3
    outputs, which carry a copied sections.csv, are not included)."""
    out = []
    top = os.path.abspath(folder).rstrip("\\/")
    for d, dirs, files in os.walk(top):
        depth = d[len(top):].count(os.sep)
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = sorted(x for x in dirs if not x.startswith('.'))
        if is_cropper_export(d):
            out.append(d)
            dirs[:] = []
    return out


def load_exports(proj, dirs, log=None):
    """All exports as one series. A section present in several exports
    is taken from the newest one (sections.csv modification time)."""
    dirs = sorted(dirs, key=lambda d: os.path.getmtime(os.path.join(d, "sections.csv")))
    by_key = {}
    for d in dirs:
        for it in load_export(None, proj, d):
            if it.key in by_key and log:
                log("[Orienter] %s: using %s (newer than %s)" % (
                    it.label, os.path.basename(d), os.path.basename(by_key[it.key].base)))
            by_key[it.key] = it
    items = list(by_key.values())
    items.sort(key=lambda i: i.sort_key)
    return items


def apply_folder_tags(items, folder):
    tp = os.path.join(folder, TAGS_FILE)
    if os.path.exists(tp):
        saved = _load_json(tp).get('tags', {})
        for it in items:
            if it.key in saved:
                it.set_saved(saved[it.key])


def list_images(folder):
    out = []
    for d, dirs, files in os.walk(folder):
        dirs[:] = sorted(x for x in dirs if not x.startswith('.'))
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in IMG_EXT:
                out.append(os.path.join(d, f))
    return out


def load_folder(folder):
    files = list_images(folder)
    groups = None
    if files:
        try:
            res = fnp.parse_files(files)
            if res['status'] == 'OK':
                groups = res['sets']
        except Exception:
            groups = None
    items = []
    if groups:
        for sid in sorted(groups):
            fs = [groups[sid][c] for c in sorted(groups[sid])]
            items.append(Item(sid, sid, fs, sid))
    else:
        for f in files:
            k = os.path.relpath(f, folder).replace('\\', '/')
            items.append(Item(k, os.path.basename(f), [f], k))
    tp = os.path.join(folder, TAGS_FILE)
    if os.path.exists(tp):
        saved = _load_json(tp).get('tags', {})
        for it in items:
            t = saved.get(it.key)
            if t:
                it.set_saved(t)
    return items


def samples_in_order(items):
    out = []
    for it in items:
        if it.sample not in out:
            out.append(it.sample)
    return out


def preview_kinds(items, base):
    kinds = {}
    for it in items:
        for f in it.files:
            k = _kind_label(it.base or base, f)
            kinds[k] = kinds.get(k, 0) + 1
    # most complete first, merged-looking first
    return sorted(kinds, key=lambda k: (-kinds[k], 'merge' not in k.lower(), k))


def preview_file(item, base, kind):
    for f in item.files:
        if _kind_label(item.base or base, f) == kind:
            return f
    return item.files[0]


# ==============================================================
# Images
# ==============================================================

def _imageio_reader(path):
    from javax.imageio import ImageIO
    from java.io import File
    iis = ImageIO.createImageInputStream(File(path))
    rs = ImageIO.getImageReaders(iis)
    if not rs.hasNext():
        iis.close()
        return None, None
    rd = rs.next()
    rd.setInput(iis)
    return rd, iis


def image_dims(path):
    """(w, h) from the file header only (~1 ms). Bio-Formats is avoided
    here: for JPEG it decodes the whole image just to report a size."""
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.tif', '.tiff'):
        from ij.io import TiffDecoder
        fi = TiffDecoder(os.path.dirname(path) + os.sep, os.path.basename(path)).getTiffInfo()
        return fi[0].width, fi[0].height
    rd, iis = _imageio_reader(path)
    if rd is not None:
        try:
            return rd.getWidth(0), rd.getHeight(0)
        finally:
            rd.dispose(); iis.close()
    imp = IJ.openImage(path)
    return imp.getWidth(), imp.getHeight()


def load_fast(path, n):
    """Display image (java BufferedImage) reduced by the integer factor n.
    JPEG/PNG: the decoder skips pixels itself (subsampling, ~15x faster
    than open + resize). TIFF: open + n x n average binning; 16/32-bit
    get a per-image auto display range."""
    from ij.plugin import Binner
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.jpg', '.jpeg', '.png'):
        rd, iis = _imageio_reader(path)
        if rd is not None:
            try:
                p = rd.getDefaultReadParam()
                if n > 1:
                    p.setSourceSubsampling(n, n, 0, 0)
                return rd.read(0, p)
            finally:
                rd.dispose(); iis.close()
    imp = IJ.openImage(path)
    if imp is None:
        raise IOError("cannot open " + path)
    ip = imp.getProcessor()
    if n > 1:
        ip = Binner().shrink(ip, n, n, Binner.AVERAGE)
    if not isinstance(ip, ColorProcessor) and ip.getBitDepth() != 8:
        _auto_range(ip)
        ip = ip.convertToByte(True)
    imp.close()
    return ip.getBufferedImage()


def load_scaled(path, k):
    """8-bit gray or RGB processor scaled by k (<= 1). 16/32-bit images
    get a per-image auto display range."""
    imp = IJ.openImage(path)
    if imp is None:
        raise IOError("cannot open " + path)
    ip = imp.getProcessor()
    if not isinstance(ip, ColorProcessor) and ip.getBitDepth() != 8:
        _auto_range(ip)
        ip = ip.convertToByte(True)
    if k < 0.999:
        w, h = ip.getWidth(), ip.getHeight()
        ip.setInterpolationMethod(ImageProcessor.BILINEAR)
        ip = ip.resize(max(1, int(round(w * k))), max(1, int(round(h * k))), True)
    imp.close()
    return ip


def oriented_size(w, h, tags):
    a = math.radians(norm_rot(tags.get('rot', 0)))
    return (abs(w * math.cos(a)) + abs(h * math.sin(a)),
            abs(w * math.sin(a)) + abs(h * math.cos(a)))


def _auto_range(ip, sat=0.0035):
    hist = ip.getHistogram()
    total = sum(hist)
    if total <= 0:
        return
    lo_t, hi_t = total * sat, total * (1 - sat)
    run, lo, hi = 0, 0, len(hist) - 1
    for v in range(len(hist)):
        run += hist[v]
        if run <= lo_t: lo = v
        if run <= hi_t: hi = v
    ip.setMinAndMax(lo, max(lo + 1, hi))


def rotate_enlarge(ip, angle, fill):
    """Rotate clockwise by angle (deg); canvas grows to fit."""
    a = norm_rot(angle)
    if a == 0.0:
        return ip
    if a in (90.0, -90.0, 180.0, -180.0):
        k = int(round(a / 90.0)) % 4
        for _ in range(k):
            ip = ip.rotateRight()
        return ip
    w, h = ip.getWidth(), ip.getHeight()
    r = math.radians(a)
    nw = int(math.ceil(abs(w * math.cos(r)) + abs(h * math.sin(r))))
    nh = int(math.ceil(abs(w * math.sin(r)) + abs(h * math.cos(r))))
    big = ip.createProcessor(nw, nh)
    if isinstance(big, ColorProcessor):
        big.setColor(int(fill)); big.fill()
    else:
        big.setValue(fill); big.fill()
    big.insert(ip, (nw - w) // 2, (nh - h) // 2)
    big.setInterpolationMethod(ImageProcessor.BILINEAR)
    big.setBackgroundValue(fill)
    big.rotate(a)
    return big


def orient_ip(ip, tags, fill):
    if tags.get('flip_h'):
        ip = ip.duplicate()
        ip.flipHorizontal()
    return rotate_enlarge(ip, tags.get('rot', 0), fill)


def apply_file(src, dst, tags, fill_raw):
    imp = IJ.openImage(src)
    if imp is None:
        raise IOError("cannot open " + src)
    fill = 0 if imp.getBitDepth() in (8, 24) else fill_raw
    stack = imp.getStack()
    out = None
    for i in range(1, stack.getSize() + 1):
        ip = orient_ip(stack.getProcessor(i), tags, fill)
        if out is None:
            out = ImageStack(ip.getWidth(), ip.getHeight(), stack.getColorModel())
        out.addSlice(stack.getSliceLabel(i), ip)
    res = ImagePlus(imp.getTitle(), out)
    res.setCalibration(imp.getCalibration())
    if imp.isComposite() or imp.getNChannels() > 1:
        res.setDimensions(imp.getNChannels(), imp.getNSlices(), imp.getNFrames())
    if imp.isComposite():
        # keep channel LUTs and per-channel display ranges (merged_orig_color)
        comp = CompositeImage(res, imp.getMode())
        for c in range(1, imp.getNChannels() + 1):
            imp.setPositionWithoutUpdate(c, 1, 1)
            comp.setPositionWithoutUpdate(c, 1, 1)
            comp.setChannelLut(imp.getChannelLut(c))
            comp.setDisplayRange(imp.getDisplayRangeMin(), imp.getDisplayRangeMax())
        comp.setPositionWithoutUpdate(1, 1, 1)
        res = comp
    elif imp.getBitDepth() not in (8, 24):
        res.setDisplayRange(imp.getDisplayRangeMin(), imp.getDisplayRangeMax())
    d = os.path.dirname(dst)
    if not os.path.isdir(d):
        os.makedirs(d)
    ext = os.path.splitext(dst)[1].lower()
    fs = FileSaver(res)
    if ext in ('.jpg', '.jpeg'):
        FileSaver.setJpegQuality(95)
        ok = fs.saveAsJpeg(dst)
    elif ext == '.png':
        ok = fs.saveAsPng(dst)
    else:
        ok = fs.saveAsTiff(dst)
    imp.close()
    if not ok:
        raise IOError("could not write " + dst)


# ==============================================================
# Save / apply
# ==============================================================

def save_tags_folder(folder, items):
    data = {'tags': dict((it.key, it.record()) for it in items if it.worth_saving()),
            'saved': time.strftime("%Y-%m-%dT%H:%M:%S")}
    f = open(os.path.join(folder, TAGS_FILE), 'w')
    try:
        json.dump(data, f, indent=1, sort_keys=True)
    finally:
        f.close()


def save_tags_project(root, items):
    def fn(p):
        o = p.setdefault('orientation', {})
        for it in items:
            if it.worth_saving():
                o[it.key] = it.record()
            else:
                o.pop(it.key, None)
    return spp.update_project(root, fn)


ORIENTED_PREFIX = "oriented_"


def oriented_dir(folder):
    """'<parent>/oriented_<name>' beside a section folder."""
    f = os.path.abspath(folder).rstrip("\\/")
    return os.path.join(os.path.dirname(f), ORIENTED_PREFIX + os.path.basename(f))


def item_groups(items, base):
    """{section folder: [items]} (several Cropper exports may be merged)."""
    g = {}
    for it in items:
        g.setdefault(os.path.abspath(it.base or base), []).append(it)
    return g


def _tif_twin(f, b):
    """The TIF of the same output beside a JPG ('<kind>_JPG/<sub>/x.jpg' ->
    '<kind>_TIF/<sub>/x.tif', b = section folder), or None: JPGs are
    re-oriented from it, so they are compressed only once."""
    parts = os.path.relpath(f, b).replace('\\', '/').split('/')
    if len(parts) < 2 or not parts[0].upper().endswith("_JPG"):
        return None
    parts[0] = parts[0][:-4] + "_TIF"
    stem = os.path.splitext(parts[-1])[0]
    for ext in ('.tif', '.tiff'):
        t = os.path.join(b, *(parts[:-1] + [stem + ext]))
        if os.path.exists(t):
            return t
    return None


def plan_apply(items, base, new_folder):
    """new_folder True: every file into 'oriented_<section folder>' beside
    it (re-oriented or copied; the section files stay). False: the section
    folder itself is renamed to 'oriented_<name>' (see rename_for_apply)
    and only the re-oriented files are rewritten there."""
    jobs = []
    for b, its in sorted(item_groups(items, base).items()):
        out = oriented_dir(b)
        for it in its:
            ident = is_identity(it.tags)
            if ident and not new_folder:
                continue
            for f in it.files:
                rel = os.path.relpath(f, b)
                read = f
                twin = None if ident else _tif_twin(f, b)
                if twin:
                    read = twin
                if not new_folder:      # read after the folder rename
                    read = os.path.join(out, os.path.relpath(read, b))
                dst = os.path.join(out, rel)
                jobs.append({'label': it.label, 'key': it.key, 'src': f, 'read': read,
                             'dst': dst, 'group': b, 'out': out,
                             'file': (os.path.basename(out) + "/" + rel).replace('\\', '/'),
                             'action': "copy" if ident else (
                                 "transform (from TIF)" if twin else "transform"),
                             'tags': tag_text(it.tags) or "-",
                             'status': ("in place" if not new_folder else
                                        "exists (overwrite)" if os.path.exists(dst) else "ok"),
                             '_tags': dict(it.tags)})
    # in place, a JPG is re-oriented from its TIF before that TIF is rewritten
    jobs.sort(key=lambda j: 0 if j['read'] != j['dst'] else 1)
    return jobs


def rename_for_apply(items, base):
    """In-place mode: rename every section folder to 'oriented_<name>'
    first, so a half-done run is never mistaken for plain section files."""
    for b in sorted(item_groups(items, base)):
        os.rename(b, oriented_dir(b))


def _job_bytes(j):
    try:
        return os.path.getsize(j['read'] if os.path.exists(j['read']) else j['src'])
    except OSError:
        return 0


def run_apply(jobs, fill_raw, prog):
    done, errors = 0, []
    eta = getattr(prog, 'eta', None)
    if eta:                               # in place the folder is renamed already
        sizes = [_job_bytes(j) for j in jobs]
        eta.set_total(sum(sizes))
    for k, j in enumerate(jobs):
        if prog.cancelled:
            break
        if eta and k:
            eta.add(sizes[k - 1])
        prog.update(k, "%s  %s" % (j['label'], j['file']))
        try:
            if j['action'] == "copy":
                d = os.path.dirname(j['dst'])
                if not os.path.isdir(d):
                    os.makedirs(d)
                shutil.copy2(j['read'], j['dst'])
            else:
                apply_file(j['read'], j['dst'], j['_tags'], fill_raw)
            j['done'] = True
            done += 1
        except Exception as e:
            errors.append("%s: %s" % (j['file'], e))
    return done, errors


def write_apply_records(out_dir, items, jobs, done, errors, stamp, source, cancelled, in_place=False):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    f = open(os.path.join(out_dir, "orientation.csv"), 'wb')
    try:
        w = csv.writer(f)
        w.writerow(['key', 'label', 'sample', 'validated', 'flip_h', 'rot', 'files'])
        for it in items:
            w.writerow([unicode(v).encode("utf-8") for v in (
                it.key, it.label, it.sample, it.validated,
                it.tags.get("flip_h"), it.tags.get("rot"),
                ";".join(os.path.basename(x) for x in it.files))])
    finally:
        f.close()
    # carry each section map forward (mirrored) so the output is a valid
    # stage-3 input again
    for b in sorted(set(it.base or source for it in items)):
        sc = os.path.join(b, "sections.csv")
        d = os.path.join(out_dir, os.path.relpath(b, source))
        dst = os.path.join(d, "sections.csv")
        if os.path.exists(sc) and os.path.abspath(sc) != os.path.abspath(dst):
            if not os.path.isdir(d):
                os.makedirs(d)
            shutil.copy2(sc, dst)
    md = codecs.open(os.path.join(out_dir, "SectionOrienter__%s_export parameters.md" % stamp),
                     'w', 'utf-8')
    try:
        n_t = sum(1 for it in items if not is_identity(it.tags))
        md.write("# Section Orienter\n\n- **Slide Image Processor**: %s\n" % spp.VERSION)
        md.write("- **Timestamp**: %s\n- **Source**: `%s`\n"
                 "- **Output**: `%s`\n- **Sections**: %d (%d re-oriented)\n"
                 "- **Files written**: %d%s\n" % (stamp, source, out_dir, len(items),
                                                 n_t, done, " (CANCELLED)" if cancelled else ""))
        md.write("- **Mode**: %s\n" % ("section folder renamed, re-oriented files rewritten in place"
                                       if in_place else "new folder, all files written"))
        md.write("- JPG files are re-oriented from their TIF twin when one exists.\n")
        md.write("\nConvention: flip horizontally first, then rotate clockwise.\n")
        md.write("\n## Re-oriented sections\n\n")
        for it in items:
            if not is_identity(it.tags):
                md.write("- %s (`%s`): %s\n" % (it.label, it.key, tag_text(it.tags)))
        if errors:
            md.write("\n## Errors\n\n")
            for e in errors:
                md.write("- %s\n" % e)
    finally:
        md.close()
