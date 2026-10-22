# ============================================================
# SectionCropperCore.py  (stage 2: Section Cropper)
#
# Non-UI logic:
#   - SlideSource: pyramid access for one slide, VIEW orientation
#   - composite(): per-channel exposure + LUT, additive RGB (Java ops)
#   - detect_sections(): threshold -> close -> fill -> outlines
#   - wand_polygon(), split_polygon(), merge_polygons()
#   - auto_contrast(): stage-1 defaults when no MISP project exists
# ============================================================

import os, math

from jarray import zeros, array
from java.awt.geom import Path2D, Area, PathIterator
from java.awt import BasicStroke
from java.util import Arrays

from ij           import ImagePlus
from ij.gui       import Wand, ShapeRoi, PolygonRoi, Roi
from ij.process   import (ByteProcessor, ColorProcessor, AutoThresholder,
                          FloodFiller, ImageProcessor, Blitter)
from ij.plugin.filter import (GaussianBlur, RankFilters, ThresholdToSelection, EDM,
                              MaximumFinder)
from ij.plugin import RoiEnlarger

from loci.formats import ImageReader, FormatTools

import SectionPipelineProject as spp


LUT_COLORS = {
    "Grays": (255, 255, 255), "Red": (255, 0, 0), "Green": (0, 255, 0),
    "Blue": (0, 0, 255), "Cyan": (0, 255, 255), "Magenta": (255, 0, 255),
    "Yellow": (255, 255, 0), "Orange": (255, 165, 0),
}
THRESHOLD_METHODS = ["Li", "Otsu", "Triangle", "Huang", "Default",   # first = default
                     "MaxEntropy", "Mean", "Moments"]
SIGNALS = ["texture", "intensity"]          # first = default
TISSUE_MODES = ["islands", "threshold"]     # first = default

DETECT_DEFAULTS = {
    'channels'   : None,       # None = all
    'signal'     : "texture",  # texture (ignores tile shading) | intensity
    'tissue'     : "islands",  # islands (not slide background) | threshold
    'method'     : "Li",
    'manual'     : None,       # manual threshold (8-bit) or None
    'invert'     : False,      # tissue darker than background
    'target_px'  : 2000,       # detection image long side (px mode / unknown pixel size)
    'len_unit'   : "um",       # um (default; needs the pixel size) | px: unit of the 3 lengths below
    'blur'       : 0.0,        # sigma (texture is already smoothed)
    'close'      : 200.0,      # closing radius (seals tissue edges), um (~14 detection px)
    'separate'   : 900.0,      # split touching sections: neck depth, um (~63 px); 0 = off
    'size_split' : 2.0,        # blob >= this x typical section = several sections (0 = off)
    'border'     : True,       # drop objects touching the image border (label, slide edge)
    'min_frac'   : 0.02,       # legacy (migrated to size_min, pct)
    'size_unit'  : 'pct',      # pct (of largest) | px (full-res px^2) | mm2
    'size_min'   : 10.0,       # in size_unit; None = no limit
    'size_max'   : None,
    'circ_min'   : 0.7,        # 4*pi*area/perimeter^2, 0..1
    'circ_max'   : 1.0,
    'circ_on'    : 'padded',   # padded (+ padding, default) | raw (section outline)
    'fill_min'   : 40.0,       # % of the ROI that is above-threshold signal
    'outline'    : "Smooth",   # Smooth | Hull
    'simplify'   : 1.0,        # Douglas-Peucker tol, detection px
    'row_tol'    : 0.5,
    'right_to_left': False,
}


# ==============================================================
# Pyramid access
# ==============================================================

def manifest_levels(entry):
    n = int(entry.get('pyramid_levels', '1'))
    return [(int(entry['L%d_w' % i]), int(entry['L%d_h' % i]))
            for i in range(n)]


class ReaderPool(object):
    """Bio-Formats readers are not thread-safe: one pool per thread."""
    def __init__(self):
        self.readers = {}

    def get(self, path):
        r = self.readers.get(path)
        if r is None:
            r = ImageReader()
            r.setId(path)
            self.readers[path] = r
        return r

    def close(self):
        for r in self.readers.values():
            try: r.close()
            except Exception: pass
        self.readers = {}


_UM_CACHE = {}
# inch is left out on purpose: 72 / 96 / 300 dpi is a default, not a calibration
_UNIT_UM = {'micron': 1.0, 'microns': 1.0, 'um': 1.0, unichr(181) + u'm': 1.0, 'nm': 0.001,
            'mm': 1000.0, 'cm': 10000.0, 'm': 1e6}


def file_um_per_px(path):
    """Pixel width in um from the file itself (TIFF calibration, else
    Bio-Formats OME metadata); None when the file does not say."""
    import FileNameParser as fnp
    real = fnp.real_path(path)
    if real in _UM_CACHE:
        return _UM_CACHE[real]
    um = None
    try:
        if os.path.splitext(real)[1].lower() in ('.tif', '.tiff'):
            from ij.io import TiffDecoder
            fi = TiffDecoder(os.path.dirname(real) + os.sep, os.path.basename(real)).getTiffInfo()[0]
            unit = (fi.unit or "").strip().lower()
            k = _UNIT_UM.get(unit)
            if k and fi.pixelWidth > 0 and fi.pixelWidth != 1.0:
                um = fi.pixelWidth * k
            elif unit in ('inch', 'in'):
                _UM_CACHE[real] = None            # dpi only: no physical size
                return None
        if not um:
            from loci.formats import MetadataTools
            r = ImageReader()
            m = MetadataTools.createOMEXMLMetadata()
            r.setMetadataStore(m)
            try:
                r.setId(real)
                q = m.getPixelsPhysicalSizeX(0)
                if q is not None:
                    from ome.units import UNITS
                    v = q.value(UNITS.MICROMETER)
                    um = float(v) if v and float(v) > 0 else None
            finally:
                r.close()
    except Exception:
        um = None
    if um is not None and not (0.01 <= um <= 1000):     # nonsense calibration
        um = None
    _UM_CACHE[real] = um
    return um


class SlideSource(object):
    """One slide (all channels) of the pyramid workspace, addressed in
    slide VIEW orientation (stage-1 rotation / flip applied)."""

    def __init__(self, root, proj, sid, manifest, ws):
        self.sid   = sid
        slide      = proj['slides'][sid]
        self.rot   = int(slide.get('rotation', 0))
        self.flip  = bool(slide.get('flip_h', False))
        self.chans = {}
        w0 = h0 = 0
        for ch, rel in sorted(slide['channels'].items()):
            name  = ws.source_key(root, spp.abs_path(root, rel))
            entry = manifest.get(name)
            if not entry:
                continue
            dims  = manifest_levels(entry)
            paths = [ws.workspace_level_path(root, name, i)
                     for i in range(len(dims))]
            self.chans[ch] = {'name': name, 'dims': dims, 'paths': paths,
                              'bit_depth': int(entry.get('bit_depth', '8'))}
            w0 = max(w0, dims[0][0]); h0 = max(h0, dims[0][1])
        self.w0, self.h0 = w0, h0
        self.vw, self.vh = spp.view_size(w0, h0, self.rot)
        # um per full-res pixel: the project setting, else the image metadata
        set_um = proj.get('settings', {}).get('pixel_size_um')
        self.um0 = float(set_um) if set_um else None
        self.um_source = "project" if set_um else None
        if not self.um0:
            for ch, rel in sorted(slide['channels'].items()):
                um = file_um_per_px(spp.abs_path(root, rel))
                if um:
                    self.um0, self.um_source = um, "image metadata"
                    break
        self.n_levels = min([len(c['dims']) for c in self.chans.values()]
                            or [1])
        # levels not to read: absent files, L1 under the skip-L1 option
        self.missing = set()
        for c in self.chans.values():
            self.missing |= ws.unusable_levels(c['paths'])

    def channels(self):
        return sorted(self.chans.keys())

    def level_scale(self, L):
        c = self.chans[self.channels()[0]]
        return float(c['dims'][L][0]) / c['dims'][0][0]

    def level_view_dims(self, ch, L):
        lw, lh = self.chans[ch]['dims'][L]
        return spp.view_size(lw, lh, self.rot)

    def pick_level(self, max_scale):
        """Coarsest level whose resolution is still >= max_scale
        (screen px per L0 px)."""
        best = 0
        for L in range(self.n_levels):
            if self.level_scale(L) >= max_scale * 0.999 and L not in self.missing:
                best = L
        return best

    def level_for_target(self, target_px):
        """Coarsest level whose long view side is >= target_px."""
        best = 0
        for L in range(self.n_levels):
            ch = self.channels()[0]
            vw, vh = self.level_view_dims(ch, L)
            if max(vw, vh) >= target_px and L not in self.missing:
                best = L
        return best

    def read_view(self, pool, ch, L, rect):
        """rect = (x, y, w, h) in LEVEL view pixels -> processor."""
        c = self.chans[ch]
        lw, lh = c['dims'][L]
        vw, vh = spp.view_size(lw, lh, self.rot)
        x, y, w, h = rect
        x = max(0, min(vw - 1, x)); y = max(0, min(vh - 1, y))
        w = max(1, min(vw - x, w)); h = max(1, min(vh - y, h))
        return spp.read_view_region(pool.get(c['paths'][L]), lw, lh,
                                    self.rot, self.flip, (x, y, w, h))

    def read_full(self, pool, ch, L):
        vw, vh = self.level_view_dims(ch, L)
        return self.read_view(pool, ch, L, (0, 0, vw, vh))


# ==============================================================
# Exposure + compositing
# ==============================================================

def to8(ip, cmin, cmax):
    """Exposure-applied 8-bit copy."""
    if ip.getBitDepth() == 8:
        d = ip.duplicate()
        d.setMinAndMax(cmin, cmax)
        if cmin != 0 or cmax != 255:
            d.applyLut()
        return d
    d = ip.duplicate()
    d.setMinAndMax(cmin, cmax)
    return d.convertToByte(True)


def channel_params(proj, ch):
    p = proj['stage1'].get('channel_params', {}).get(ch) or {}
    return (float(p.get('min', 0)), float(p.get('max', 65535)),
            p.get('lut') or "Grays")


def composite(ips, proj, chans):
    """ips: {ch: processor (same size)} -> additive RGB ColorProcessor."""
    w = h = None
    acc = None
    for ch in chans:
        if ch not in ips:
            continue
        ip = ips[ch]
        if acc is None:
            w, h = ip.getWidth(), ip.getHeight()
            acc = [ByteProcessor(w, h), ByteProcessor(w, h), ByteProcessor(w, h)]
        cmin, cmax, lut = channel_params(proj, ch)
        p8 = to8(ip, cmin, cmax)
        if p8.getWidth() != w or p8.getHeight() != h:
            p8 = p8.resize(w, h)
        col = LUT_COLORS.get(lut, (255, 255, 255))
        for a, c in zip(acc, col):
            if c == 0:
                continue
            if c == 255:
                a.copyBits(p8, 0, 0, Blitter.ADD)
            else:
                t = p8.duplicate(); t.multiply(c / 255.0)
                a.copyBits(t, 0, 0, Blitter.ADD)
    if acc is None:
        return None
    cp = ColorProcessor(w, h)
    cp.setRGB(acc[0].getPixels(), acc[1].getPixels(), acc[2].getPixels())
    return cp


def colorize(ip, proj, ch):
    return composite({ch: ip}, proj, [ch])


# ==============================================================
# Detection
# ==============================================================

def _fill_holes_fast(mask):
    w, h = mask.getWidth(), mask.getHeight()
    pad = ByteProcessor(w + 2, h + 2)
    pad.insert(mask, 1, 1)
    pad.setValue(128)
    FloodFiller(pad).fill(0, 0)
    # 128 = outside; everything else (0 holes, 255 fg) becomes fg
    lut = zeros(256, 'i')
    for v in range(256):
        lut[v] = 0 if v == 128 else 255
    pad.applyTable(lut)
    pad.setRoi(1, 1, w, h)
    return pad.crop()


TEXTURE_RADIUS = 2       # local std radius (detection px): nuclei / cell texture
TEXTURE_BLUR   = 4.0     # smoothing of the texture map
OPEN_RADIUS    = 6       # cut thin bridges / debris after the tissue mask


def _texture8(ip):
    """8-bit map of local texture (std of the raw values). Tissue is grainy,
    slide background smooth whatever the tile shading of the scan."""
    fp = ip.convertToFloatProcessor()
    RankFilters().rank(fp, TEXTURE_RADIUS, RankFilters.VARIANCE)
    fp.sqrt()
    GaussianBlur().blurGaussian(fp, TEXTURE_BLUR, TEXTURE_BLUR, 0.01)
    st = fp.getStatistics()
    fp.setMinAndMax(0, max(1e-6, st.mean * 6))     # mostly background: bright end ~ tissue
    return fp.convertToByteProcessor(True)


def _textured_area(p8):
    """Share of the image above the Otsu level of a texture map."""
    h = p8.getHistogram()
    t = AutoThresholder().getThreshold(AutoThresholder.Method.Otsu, h)
    n1 = sum(h[t + 1:])
    return n1 / float(max(1, sum(h)))


DET_UM = 14.3          # um per detection px in length mode (the size the defaults were tuned at)
LENGTH_KEYS = ('blur', 'close', 'separate')     # detection lengths: um or px (params len_unit)


def detection_scale(src, params):
    """(level, resize factor, f = detection px per full-res px, um per
    detection px, um known?). Length mode with a known pixel size: the
    detection image is always DET_UM um/px, whatever the objective or the
    scanned area. Otherwise (px mode, or no pixel size): the pyramid
    level with long side >= target_px, as before."""
    if params.get('len_unit') == 'um' and src.um0:
        best = 0
        for L in range(src.n_levels):
            if L not in src.missing and src.um0 / src.level_scale(L) <= DET_UM * 1.0001:
                best = L
        uml = src.um0 / src.level_scale(best)
        k = min(1.0, uml / DET_UM)
        return best, k, src.level_scale(best) * k, uml / k, True
    L = src.level_for_target(int(params.get('target_px', 2000)))
    f = src.level_scale(L)
    return L, 1.0, f, (src.um0 / f) if src.um0 else DET_UM, bool(src.um0)


def px_params(params, um_per_px):
    """params with the detection lengths in detection px."""
    if params.get('len_unit') != 'um':
        return params
    out = dict(params)
    for k in LENGTH_KEYS:
        v = params.get(k)
        if v not in (None, ""):
            out[k] = float(v) / um_per_px
    return out


def detection_input(src, pool, proj, params):
    """(comb 8-bit signal, f detection px per full-res px, params with
    lengths in px, info text)."""
    params = normalize_detect(params)
    L, k, f, um, known = detection_scale(src, params)
    comb = detection_image(src, pool, proj, params, L, k)
    if params.get('len_unit') == 'um':
        info = ("%.1f um/px (%s)" % (um, src.um_source) if known else
                "pixel size unknown: lengths read at %.1f um/px" % um)
    else:
        info = "lengths in detection px (%.1f um/px)" % um if known else "lengths in detection px"
    return comb, f, px_params(params, um), info


def detection_image(src, pool, proj, params, L=None, k=1.0):
    """8-bit detection signal, max over the detection channels:
    'texture' (default) = local texture of the raw pixels (tile shading
    does not matter); 'intensity' = exposure-applied brightness.
    L / k: pyramid level and resize factor (detection_scale)."""
    if L is None:
        L = src.level_for_target(int(params.get('target_px', 2000)))
    chosen = params.get('channels')
    chans = [ch for ch in (chosen or src.channels()) if ch in src.chans]
    texture = params.get('signal', 'texture') == 'texture'
    maps = []
    for ch in chans:
        raw = src.read_full(pool, ch, L)
        if k < 0.999:                     # to the fixed um/px, before the texture filter
            raw.setInterpolationMethod(ImageProcessor.BILINEAR)
            raw = raw.resize(max(1, int(round(raw.getWidth() * k))),
                             max(1, int(round(raw.getHeight() * k))), True)
        if texture:
            maps.append((ch, _texture8(raw)))
        else:
            cmin, cmax, _ = channel_params(proj, ch)
            maps.append((ch, to8(raw, cmin, cmax)))
    if texture and not chosen and len(maps) > 1:
        # all channels: use those that show tissue texture over a large area
        # (a sparse marker channel adds only specks and noise)
        area = dict((ch, _textured_area(p8)) for ch, p8 in maps)
        top = max(area.values())
        maps = [(ch, p8) for ch, p8 in maps if area[ch] >= 0.5 * top]
    comb = None
    for ch, p8 in maps:
        if comb is None:
            comb = p8
        else:
            if p8.getWidth() != comb.getWidth() or p8.getHeight() != comb.getHeight():
                p8 = p8.resize(comb.getWidth(), comb.getHeight())
            comb.copyBits(p8, 0, 0, Blitter.MAX)
    if comb is None:
        raise ValueError("No detection channel available on " + src.sid)
    if params.get('invert') and not texture:
        comb.invert()
    return comb


def _islands(sig):
    """Everything not connected to the slide background (the smooth area
    reaching the image border) -> 255."""
    w, h = sig.getWidth(), sig.getHeight()
    pad = ByteProcessor(w + 2, h + 2)
    pad.insert(sig, 1, 1)
    pad.setValue(128)
    FloodFiller(pad).fill(0, 0)
    lut = zeros(256, 'i')
    for v in range(256):
        lut[v] = 0 if v == 128 else 255
    pad.applyTable(lut)
    pad.setRoi(1, 1, w, h)
    return pad.crop()


def _separate(mask, depth):
    """Split touching sections: watershed of the distance map, cutting only
    where the 'neck' is at least `depth` px shallower than both sides (so a
    section's own lobes are not cut)."""
    edm = EDM().makeFloatEDM(mask, 0, False)
    seg = MaximumFinder().findMaxima(edm, float(depth), ImageProcessor.NO_THRESHOLD,
                                     MaximumFinder.SEGMENTED, False, True)
    out = mask.duplicate()
    out.copyBits(seg, 0, 0, Blitter.AND)
    return out


SEED_CLOSE   = 4      # light closing for the per-section seed mask
SEED_FRAC    = 0.2    # seed objects >= this share of the largest one
SEED_MULT    = 2.0    # default of params size_split: object >= this x the typical section = several


def _centres(roi, k, step=8):
    """k-means (k centres) of the pixels inside roi, on a coarse grid."""
    b = roi.getBounds()
    pts = [(x, y) for y in range(b.y, b.y + b.height, step)
           for x in range(b.x, b.x + b.width, step) if roi.contains(x, y)]
    if len(pts) < k * 4:
        return []
    n = float(len(pts))
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    if k == 1:
        return [(cx, cy)]
    cen = [max(pts, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)]
    while len(cen) < k:          # spread-out start: farthest from the chosen ones
        cen.append(max(pts, key=lambda p: min((p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2
                                              for c in cen)))
    cen = [(float(c[0]), float(c[1])) for c in cen]
    for it in range(30):
        acc = [[0.0, 0.0, 0] for c in cen]
        for p in pts:
            j = min(range(k), key=lambda i: (p[0] - cen[i][0]) ** 2 + (p[1] - cen[i][1]) ** 2)
            a = acc[j]
            a[0] += p[0]; a[1] += p[1]; a[2] += 1
        new = [(a[0] / a[2], a[1] / a[2]) if a[2] else c for a, c in zip(acc, cen)]
        moved = max(abs(u[0] - c[0]) + abs(u[1] - c[1]) for u, c in zip(new, cen))
        cen = new
        if moved < 0.5:
            break
    return cen


def _split_by_size(big, sig, depth, mult=SEED_MULT):
    """Sections pressed together along long edges (one blob even after the
    watershed): find each section's centre from a lightly closed mask
    (objects much larger than the typical section on the slide hold
    several sections: k-means centres), then split every blob holding
    several centres between them. _refine_cuts then moves the cuts onto
    the real gaps."""
    rf = RankFilters()
    fine = sig.duplicate()
    rf.rank(fine, SEED_CLOSE, RankFilters.MAX)
    rf.rank(fine, SEED_CLOSE, RankFilters.MIN)
    fine = _islands(fine)
    fine.copyBits(big, 0, 0, Blitter.AND)
    rf.rank(fine, OPEN_RADIUS, RankFilters.MIN)
    rf.rank(fine, OPEN_RADIUS, RankFilters.MAX)
    fine = _separate(fine, depth)
    objs = [(p, spp.poly_area(p)) for p in mask_polygons(fine) if len(p) >= 3]
    if not objs:
        return big
    amax = max(a for p, a in objs)
    objs = [(p, a) for p, a in objs if a >= SEED_FRAC * amax]
    ref = sorted(a for p, a in objs)[(len(objs) - 1) // 2]     # lower median: typical single section
    seeds = []
    for p, a in objs:
        k = int(a / ref + 0.5) if a >= mult * ref else 1
        seeds.extend(_centres(spp.poly_roi(p), max(1, k)))
    out = big.duplicate()
    for bp in mask_polygons(big):
        if len(bp) < 3:
            continue
        broi = spp.poly_roi(bp)
        b = broi.getBounds()
        inside = [(x - b.x, y - b.y) for x, y in seeds if broi.contains(int(x), int(y))]
        if len(inside) < 2:
            continue
        bg = ByteProcessor(b.width, b.height)
        bg.setValue(255)
        bg.fill()
        bg.setValue(0)
        for x, y in inside:
            bg.fillOval(int(x) - 3, int(y) - 3, 7, 7)
        edm = EDM().makeFloatEDM(bg, 0, False)      # distance to the nearest centre
        edm.multiply(-1.0)
        vor = MaximumFinder().findMaxima(edm, 0.5, ImageProcessor.NO_THRESHOLD,
                                         MaximumFinder.SEGMENTED, False, False)
        vor.invert()                                 # 255 on the dividing lines
        m = broi.getMask()
        if m is not None:
            vor.copyBits(m, 0, 0, Blitter.AND)       # only inside this blob
        out.setRoi(b.x, b.y, b.width, b.height)
        sub = out.crop()
        sub.copyBits(vor, 0, 0, Blitter.SUBTRACT)
        out.resetRoi()
        out.insert(sub, b.x, b.y)
    return out


def make_mask(comb, params, want_raw=False):
    """Blur, threshold, close, then the tissue mask:
    'islands' (default) = all that is not the slide background, so dim or
    patchy tissue stays whole; 'threshold' = above-threshold area with
    holes filled. Thin bridges are opened and touching sections split
    (params 'separate'). want_raw: also the thresholded signal (for the
    signal-fill filter)."""
    ip = comb.duplicate()
    s = float(params.get('blur', 0))
    if s > 0:
        GaussianBlur().blurGaussian(ip, s, s, 0.01)
    if params.get('manual') not in (None, ""):
        t = int(params['manual'])
    else:
        m = AutoThresholder.Method.valueOf(params.get('method', THRESHOLD_METHODS[0]))
        t = AutoThresholder().getThreshold(m, ip.getHistogram())
    ip.threshold(t)                   # <= t -> 0, > t -> 255
    sig = ip.duplicate()
    raw = sig if want_raw else None
    rf = RankFilters()
    r = float(params.get('close', 0))
    if r > 0:
        rf.rank(ip, r, RankFilters.MAX)
        rf.rank(ip, r, RankFilters.MIN)
    if params.get('tissue', 'islands') == 'islands':
        ip = _islands(ip)
    else:
        ip = _fill_holes_fast(ip)
    rf.rank(ip, OPEN_RADIUS, RankFilters.MIN)
    rf.rank(ip, OPEN_RADIUS, RankFilters.MAX)
    depth = params.get('separate')
    if depth not in (None, "") and float(depth) > 0:
        mult = params.get('size_split')
        mult = SEED_MULT if mult in (None, "") else float(mult)
        split = _split_by_size(ip, sig, depth, mult) if mult > 0 else ip
        ip = _refine_cuts(ip, _separate(split, depth), sig, r)
    if want_raw:
        return ip, t, raw
    return ip, t


def _refine_cuts(whole, split, sig, close_r):
    """Near the lines where touching sections were split, the closing has
    bridged the real gap between them; there the boundary is redrawn from
    the unclosed signal (holes that this leaves inside a section are
    filled again), so it follows the true gap instead of a straight cut."""
    lines = whole.duplicate()
    lines.copyBits(split, 0, 0, Blitter.DIFFERENCE)      # the cut lines
    if lines.getStatistics().max == 0:
        return split
    rf = RankFilters()
    zone = lines
    rf.rank(zone, 4 * max(close_r, 1) + 8, RankFilters.MAX)
    fine = sig.duplicate()
    rf.rank(fine, 2, RankFilters.MAX)
    rf.rank(fine, 2, RankFilters.MIN)
    zone.invert()                                        # 255 = away from the cuts
    fine.copyBits(zone, 0, 0, Blitter.OR)
    out = split.duplicate()
    out.copyBits(fine, 0, 0, Blitter.AND)
    out = _fill_holes_fast(out)
    rf.rank(out, 2, RankFilters.MIN)
    rf.rank(out, 2, RankFilters.MAX)
    return out


def touches_border(p, w, h, margin=2):
    xs = [x for x, y in p]
    ys = [y for x, y in p]
    return (min(xs) <= margin or min(ys) <= margin or
            max(xs) >= w - 1 - margin or max(ys) >= h - 1 - margin)


def mask_polygons(mask):
    """Outer outlines of the mask's connected components (mask px)."""
    mask.setThreshold(255, 255, ImageProcessor.NO_LUT_UPDATE)
    roi = ThresholdToSelection().convert(mask)
    mask.resetThreshold()
    if roi is None:
        return []
    rois = list(roi.getRois()) if isinstance(roi, ShapeRoi) else [roi]
    out = []
    for r in rois:
        fp = r.getFloatPolygon()
        out.append([[fp.xpoints[i], fp.ypoints[i]] for i in range(fp.npoints)])
    return out


SIZE_UNITS = [('pct', "% of largest"), ('px', "px^2 (full res)"), ('mm2', "mm^2")]


def normalize_detect(d):
    """Fill new keys; migrate the old relative min_frac setting."""
    out = dict(DETECT_DEFAULTS)
    if d and 'signal' not in d:
        # saved before texture / island detection: keep its behaviour
        out.update({'signal': 'intensity', 'tissue': 'threshold',
                    'separate': 0, 'border': False})
    if d and 'len_unit' not in d:
        # saved before length units: its blur / close / depth are detection px
        out.update({'len_unit': 'px', 'close': 12, 'separate': 60})
    out.update(d or {})
    if d and 'size_unit' not in d and 'min_frac' in d:
        out['size_unit'] = 'pct'
        out['size_min'] = float(d['min_frac']) * 100.0
    return out


def _traced_circ(p):
    """Circularity of an integer (traced) outline: 4*pi*area/perimeter^2
    with ImageJ's traced-perimeter corner correction (same numbers as
    Analyze Particles)."""
    pts = [(int(round(q[0])), int(round(q[1]))) for q in p]
    # ImageJ's corner correction counts vertices, so straight runs must
    # not carry extra (collinear) vertices
    n = len(pts)
    pts = [pts[i] for i in range(n)
           if (pts[i][0] - pts[i - 1][0]) * (pts[(i + 1) % n][1] - pts[i][1]) !=
              (pts[i][1] - pts[i - 1][1]) * (pts[(i + 1) % n][0] - pts[i][0])]
    if len(pts) < 3:
        return 0.0
    xs = array([q[0] for q in pts], 'i')
    ys = array([q[1] for q in pts], 'i')
    per = PolygonRoi(xs, ys, len(pts), Roi.TRACED_ROI).getLength()
    a = spp.poly_area(p)
    return min(1.0, 4.0 * math.pi * a / (per * per)) if per > 0 else 0.0


def object_stats(p, pad_frac=0.0, mode='outline'):
    """(area, circularity) of a traced mask outline, in mask px.
    pad_frac > 0: circularity of the outline grown by the section
    padding (pad_frac * bbox diagonal, the default crop padding); the
    padding is relative, so the value does not depend on resolution."""
    a = spp.poly_area(p)
    if pad_frac <= 0:
        return a, _traced_circ(p)
    x0, y0, x1, y1 = spp.polygon_bounds(p)
    pad = pad_frac * math.hypot(x1 - x0, y1 - y0)
    m = int(math.ceil(pad)) + 2
    base = spp.convex_hull(p) if mode == 'hull' else p
    shifted = [[q[0] - x0 + m, q[1] - y0 + m] for q in base]
    xs = array([int(round(q[0])) for q in shifted], 'i')
    ys = array([int(round(q[1])) for q in shifted], 'i')
    kind = Roi.POLYGON if mode == 'hull' else Roi.TRACED_ROI
    big = RoiEnlarger.enlarge(PolygonRoi(xs, ys, len(base), kind), pad)
    if isinstance(big, ShapeRoi):
        parts = list(big.getRois())
        big = max(parts, key=lambda r: r.getBounds().width * r.getBounds().height)
    fp = big.getPolygon()
    return a, _traced_circ([[fp.xpoints[i], fp.ypoints[i]] for i in range(fp.npoints)])


def size_in_unit(a_mask, f, unit, amax_mask, px_um):
    if unit == 'pct':
        return 100.0 * a_mask / amax_mask if amax_mask else 0.0
    a_l0 = a_mask / (f * f)
    if unit == 'mm2':
        if not px_um:
            raise ValueError("Pixel size is not set (needed for mm2)")
        return a_l0 * float(px_um) ** 2 / 1e6
    return a_l0


def signal_fill(raw, p):
    """% of the outline's area that is above-threshold signal (raw =
    thresholded mask before closing / hole filling)."""
    x0, y0, x1, y1 = spp.polygon_bounds(p)
    bx, by = max(0, int(x0)), max(0, int(y0))
    bw = min(raw.getWidth(), int(math.ceil(x1))) - bx
    bh = min(raw.getHeight(), int(math.ceil(y1))) - by
    if bw <= 0 or bh <= 0:
        return 0.0
    region = ByteProcessor(bw, bh)
    region.setValue(255)
    region.fill(spp.poly_roi([[x - bx, y - by] for x, y in p]))
    n_roi = region.getHistogram()[255]
    raw.setRoi(bx, by, bw, bh)
    sig = raw.crop()
    raw.resetRoi()
    sig.copyBits(region, 0, 0, Blitter.AND)
    return 100.0 * sig.getHistogram()[255] / n_roi if n_roi else 0.0


def filter_objects(polys, f, params, px_um, pad_frac=0.0, pad_mode='hull', raw=None):
    """Size (min/max in the chosen unit), circularity (min/max, on the
    outline or the padded outline) and signal-fill (min %) filters.
    Returns (kept polys, report list of dicts)."""
    pf = pad_frac if params.get('circ_on') == 'padded' else 0.0
    stats = [object_stats(p, pf, pad_mode) for p in polys]
    amax = max([a for a, c in stats] or [0])
    unit = params.get('size_unit', 'pct')
    smin = params.get('size_min')
    smax = params.get('size_max')
    cmin = float(params.get('circ_min') or 0.0)
    cmax = params.get('circ_max')
    cmax = 1.0 if cmax in (None, "") else float(cmax)
    fmin = params.get('fill_min')
    kept, report = [], []
    for p, (a, c) in zip(polys, stats):
        s = size_in_unit(a, f, unit, amax, px_um)
        fill = signal_fill(raw, p) if raw is not None else None
        why = None
        if smin not in (None, "") and s < float(smin):
            why = "size"
        elif smax not in (None, "") and s > float(smax):
            why = "size"
        elif c < cmin or c > cmax:
            why = "circularity"
        elif fill is not None and fmin not in (None, "") and fill < float(fmin):
            why = "fill"
        report.append({'size': s, 'circ': c, 'fill': fill, 'rejected': why,
                       'cx': spp.poly_centroid(p)[0] / f,
                       'cy': spp.poly_centroid(p)[1] / f})
        if why is None:
            kept.append(p)
    return kept, report


def detect_sections(src, pool, proj, params):
    """Returns dict(polys=[L0 view polygons, reading order], mask, f,
    level, threshold, report=[per-object size / circularity / verdict])."""
    params = normalize_detect(params)
    comb, f, pp, info = detection_input(src, pool, proj, params)
    mask, t, raw = make_mask(comb, pp, want_raw=True)
    polys = [p for p in mask_polygons(mask) if len(p) >= 3]
    if params.get('border'):
        w, h = mask.getWidth(), mask.getHeight()
        polys = [p for p in polys if not touches_border(p, w, h)]
    polys, report = filter_objects(polys, f, params,
                                   proj['settings'].get('pixel_size_um') or src.um0,
                                   float(proj['settings'].get('padding_frac') or 0.0),
                                   spp.padding_mode(proj), raw)
    out = []
    for p in polys:
        if params.get('outline') == "Hull":
            p = spp.convex_hull(p)
        else:
            p = spp.simplify_polygon(p, float(params.get('simplify', 1.0)))
        out.append([[x / f, y / f] for x, y in p])
    order = spp.auto_order(out, float(params.get('row_tol', 0.5)),
                           bool(params.get('right_to_left')))
    return {'polys': [out[i] for i in order], 'mask': mask, 'f': f,
            'threshold': t, 'report': report, 'comb': comb, 'raw': raw,
            'params': params, 'px_params': pp, 'scale_info': info}


def wand_polygon(mask, f, x, y, params=None):
    """Component of the detection mask under L0 view point (x, y)."""
    mx, my = int(x * f), int(y * f)
    if not (0 <= mx < mask.getWidth() and 0 <= my < mask.getHeight()):
        return None
    if mask.get(mx, my) != 255:
        return None
    wd = Wand(mask)
    wd.autoOutline(mx, my, 255.0, 255.0)
    if wd.npoints < 3:
        return None
    p = [[wd.xpoints[i], wd.ypoints[i]] for i in range(wd.npoints)]
    params = params or DETECT_DEFAULTS
    if params.get('outline') == "Hull":
        p = spp.convex_hull(p)
    else:
        p = spp.simplify_polygon(p, float(params.get('simplify', 1.0)))
    return [[px / f, py / f] for px, py in p]


# ==============================================================
# Polygon geometry (java.awt.geom)
# ==============================================================

def poly_path(poly):
    path = Path2D.Double()
    path.moveTo(poly[0][0], poly[0][1])
    for x, y in poly[1:]:
        path.lineTo(x, y)
    path.closePath()
    return path


def area_polygons(area):
    """Area -> list of closed polygons."""
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
    return out


def split_polygon(poly, p1, p2):
    """Cut poly along the infinite line p1-p2. Returns [a, b] or None."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = math.hypot(dx, dy)
    if L < 1e-6:
        return None
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    x0, y0, x1, y1 = spp.polygon_bounds(poly)
    B = 4.0 * (abs(x1 - x0) + abs(y1 - y0) + L) + 10.0
    a = (p1[0] - ux * B, p1[1] - uy * B)
    b = (p1[0] + ux * B, p1[1] + uy * B)
    half = [[a[0], a[1]], [b[0], b[1]],
            [b[0] + nx * B, b[1] + ny * B], [a[0] + nx * B, a[1] + ny * B]]
    A = Area(poly_path(poly))
    H = Area(poly_path(half))
    left = Area(A); left.intersect(H)
    right = Area(A); right.subtract(H)
    res = []
    for ar in (left, right):
        ps = area_polygons(ar)
        if not ps:
            return None
        res.append(max(ps, key=spp.poly_area))
    if min(spp.poly_area(r) for r in res) < 1.0:
        return None
    return res


# ---------------------------------------------------------- polyline split

def _polyline_path(pts):
    p = Path2D.Double()
    p.moveTo(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        p.lineTo(x, y)
    return p


def extend_polyline(pts, length):
    """Prolong the first and last segments straight by `length`."""
    pts = [list(q) for q in pts]
    def ext(a, b):          # beyond b, away from a
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1.0
        return [b[0] + dx / L * length, b[1] + dy / L * length]
    return [ext(pts[1], pts[0])] + pts + [ext(pts[-2], pts[-1])]


def _cut(area, pts, width):
    stroke = BasicStroke(float(width), BasicStroke.CAP_BUTT, BasicStroke.JOIN_ROUND)
    area.subtract(Area(stroke.createStrokedShape(_polyline_path(pts))))
    return sorted(area_polygons(area), key=spp.poly_area, reverse=True)


def polyline_crosses(poly, pts):
    from java.awt.geom import Line2D
    n = len(poly)
    for k in range(len(pts) - 1):
        a, b = pts[k], pts[k + 1]
        for i in range(n):
            c, d = poly[i], poly[(i + 1) % n]
            if Line2D.linesIntersect(a[0], a[1], b[0], b[1], c[0], c[1], d[0], d[1]):
                return True
    return False


def split_by_polyline(poly, pts, width=2.0):
    """Cut poly along the (end-extended) polyline. Returns the two largest
    pieces, or None if the line does not cut it in two."""
    x0, y0, x1, y1 = spp.polygon_bounds(poly)
    ext = extend_polyline(pts, 2.0 * math.hypot(x1 - x0, y1 - y0) + 10)
    parts = _cut(Area(poly_path(poly)), ext, width)
    if len(parts) < 2 or spp.poly_area(parts[1]) < 1.0:
        return None
    return parts[0], parts[1]


def side_regions(box, pts, width=2.0):
    """The two regions of `box` (polygon) on either side of the polyline,
    whose ends are prolonged straight until they leave the box."""
    x0, y0, x1, y1 = spp.polygon_bounds(box)
    ext = extend_polyline(pts, 2.0 * math.hypot(x1 - x0, y1 - y0) + 10)
    parts = _cut(Area(poly_path(box)), ext, width)
    return parts[:2] if len(parts) >= 2 else None


def _count(ip):
    return ip.getHistogram()[255]


def split_seeded(comb, f, threshold, poly, pts, params):
    """Split one section along a drawn polyline and re-detect both slices.

    Tissue (same threshold, no closing so touching slices are not bridged)
    inside the section is cut by a band along the line; the largest tissue
    piece on each side is a seed; both seeds then grow one pixel at a time
    THROUGH tissue until it is all assigned. Tissue the line wrongly cut
    off a slice goes back to the slice it is attached to. Returns two
    contours (full-res view px) in reading order, or None."""
    params = normalize_detect(params)
    close = float(params.get('close', 0))
    pad = int(math.ceil(float(params.get('blur', 0)) * 3 + close)) + 4
    sx = [q[0] * f for q in poly]; sy = [q[1] * f for q in poly]
    bx0 = max(0, int(min(sx)) - pad); by0 = max(0, int(min(sy)) - pad)
    bx1 = min(comb.getWidth(), int(max(sx)) + pad + 1)
    by1 = min(comb.getHeight(), int(max(sy)) + pad + 1)
    if bx1 - bx0 < 3 or by1 - by0 < 3:
        return None
    comb.setRoi(bx0, by0, bx1 - bx0, by1 - by0)
    tissue = comb.crop()
    comb.resetRoi()
    W, H = tissue.getWidth(), tissue.getHeight()
    s = float(params.get('blur', 0))
    if s > 0:
        GaussianBlur().blurGaussian(tissue, s, s, 0.01)
    tissue.threshold(int(threshold))
    region = ByteProcessor(W, H)
    region.setValue(255)
    region.fill(spp.poly_roi([[x - bx0, y - by0] for x, y in zip(sx, sy)]))
    rf = RankFilters()
    rf.rank(region, max(2.0, close), RankFilters.MAX)      # this section only
    tissue.copyBits(region, 0, 0, Blitter.AND)
    # seeds: tissue minus a band along the (end-extended) line
    lp = [[x * f - bx0, y * f - by0] for x, y in pts]
    ext = extend_polyline(lp, 2.0 * (W + H))
    band = tissue.duplicate()
    band.setValue(0)
    band.setLineWidth(int(max(3, close)))
    for a, b in zip(ext[:-1], ext[1:]):
        band.drawLine(int(a[0]), int(a[1]), int(b[0]), int(b[1]))
    sides = side_regions([[0, 0], [W, 0], [W, H], [0, H]], lp)
    if not sides:
        return None
    best = [None, None]
    for comp in mask_polygons(band):
        cx, cy = spp.poly_centroid(comp)
        for k in (0, 1):
            if point_in_poly(sides[k], cx, cy) and \
                    (best[k] is None or spp.poly_area(comp) > spp.poly_area(best[k])):
                best[k] = comp
    if best[0] is None or best[1] is None:
        return None
    seeds = []
    for comp in best:
        sd = ByteProcessor(W, H)
        sd.setValue(255)
        sd.fill(spp.poly_roi(comp))
        sd.copyBits(band, 0, 0, Blitter.AND)
        seeds.append(sd)
    # grow both seeds through tissue, alternating, until nothing changes
    for _ in range(W + H):
        before = (_count(seeds[0]), _count(seeds[1]))
        for k in (0, 1):
            g = seeds[k].duplicate()
            rf.rank(g, 1.0, RankFilters.MAX)
            g.copyBits(tissue, 0, 0, Blitter.AND)
            g.copyBits(seeds[1 - k], 0, 0, Blitter.SUBTRACT)
            seeds[k] = g
        if (_count(seeds[0]), _count(seeds[1])) == before:
            break
    out = []
    for sd in seeds:
        if close > 0:
            rf.rank(sd, close, RankFilters.MAX)
            rf.rank(sd, close, RankFilters.MIN)
        sd = _fill_holes_fast(sd)
        polys = [q for q in mask_polygons(sd) if len(q) >= 3]
        if not polys:
            return None
        c = max(polys, key=spp.poly_area)
        if params.get('outline') == "Hull":
            c = spp.convex_hull(c)
        else:
            c = spp.simplify_polygon(c, float(params.get('simplify', 1.0)))
        out.append([[(x + bx0) / f, (y + by0) / f] for x, y in c])
    return [out[i] for i in spp.auto_order(out)]


def merge_polygons(polys):
    A = Area()
    for p in polys:
        A.add(Area(poly_path(p)))
    ps = area_polygons(A)
    if len(ps) == 1:
        return ps[0]
    pts = []
    for p in polys:
        pts.extend(p)
    return spp.convex_hull(pts)


def point_in_poly(poly, x, y):
    return poly_path(poly).contains(x, y)


# ==============================================================
# Default exposure (when stage 1 was skipped)
# ==============================================================

def auto_contrast(root, proj, manifest, ws, saturated=0.0035):
    """Pooled 0.35% / 99.65% percentiles of the smallest pyramid level
    of every slide, per channel. Writes proj['stage1']['channel_params']
    for channels that have no params yet."""
    pool = ReaderPool()
    params = proj['stage1'].setdefault('channel_params', {})
    try:
        hists = {}
        for sid in spp.ordered_slide_ids(proj):
            src = SlideSource(root, proj, sid, manifest, ws)
            for ch in src.channels():
                if ch in params:
                    continue
                ip = src.read_full(pool, ch, src.n_levels - 1)
                bd = ip.getBitDepth()
                top = 255 if bd == 8 else 65535
                h = hists.setdefault(ch, [zeros(1024, 'l'), top])
                hh = ip.getHistogram() if bd != 32 else None
                if hh is not None:
                    scale = 1024.0 / (len(hh))
                    for v in range(len(hh)):
                        if hh[v]:
                            h[0][min(1023, int(v * scale))] += hh[v]
        for ch, (hist, top) in hists.items():
            total = sum(hist)
            if total == 0:
                continue
            lo_t, hi_t = total * saturated, total * (1 - saturated)
            run, lo, hi = 0, 0, 1023
            for i in range(1024):
                run += hist[i]
                if run <= lo_t: lo = i
                if run <= hi_t: hi = i
            k = (top + 1) / 1024.0
            params[ch] = {'min': float(int(lo * k)),
                          'max': float(max(int(lo * k) + 1, int((hi + 1) * k))),
                          'lut': proj['stage1'].get('default_luts', {}).get(ch, "Grays")}
    finally:
        pool.close()
    return params


# ==============================================================
# Per-section metrics (section table + histograms): measured on the
# stored ROI polygon (full-res view px), the way it was detected/edited
# ==============================================================

def poly_circularity(poly):
    n = len(poly)
    if n < 3:
        return 0.0
    per = sum(math.hypot(poly[(i + 1) % n][0] - poly[i][0],
                         poly[(i + 1) % n][1] - poly[i][1]) for i in range(n))
    return min(1.0, 4.0 * math.pi * spp.poly_area(poly) / (per * per)) if per > 0 else 0.0


def section_area_circ(proj, sec_id, circ_on):
    """(area in full-res px^2, circularity) of one section ROI;
    circ_on 'padded' measures the padded shape (current padding mode)."""
    s = proj['sections'][sec_id]
    poly = s['polygon']
    shape = poly
    if circ_on == 'padded':
        shape = spp.padded_shape(proj, sec_id, max_dim=400)
    return spp.poly_area(poly), poly_circularity(shape)
