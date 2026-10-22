# ============================================================
# MultichannelImageSetPreviewerWorkspace.py
#
# Workspace (pyramid cache) + manifest logic, split out of
# MultichannelImageSetPreviewer.py (Jython parser size limit).
# Loaded via execfile() into the main globals.
#
# Speed-ups vs. the original in-file version (manifest v5):
#   1. Cascaded downsampling: L(k) is built from L(k-1), not from L0
#   2. L0 is NOT copied when the source is a TIFF that Bio-Formats
#      can region-read efficiently (gray 8/16/32-bit, uncompressed
#      or tiled / small strips) -> the source itself is level 0
#   3. Quick fingerprint (size + first/last 4 MB) instead of a full
#      MD5 read of every converted file
#   4. Parallel conversion, bounded by a JVM memory budget
#   5. RGB (pseudo-colored) sources -> 8-bit gray via per-pixel
#      max(R,G,B) instead of failing
#   6. Optional workspace root outside the image folder
#      (misp_workspace_root.txt next to this file, one path inside)
#
# v5 manifests stay valid (their L0 files already exist).
# ============================================================

import os, time, threading, traceback, codecs
from jarray import zeros

from java.lang     import Runtime, System as JSystem, String as JString
from java.security import MessageDigest
from java.io       import FileInputStream, RandomAccessFile

from ij           import IJ, ImagePlus, Prefs
from ij.io        import Opener, FileSaver
from ij.plugin    import Binner
from ij.process   import (ByteProcessor, ColorProcessor, Blitter, ShortProcessor,
                          FloatProcessor)
from java.nio      import ByteBuffer, ByteOrder
from java.io       import File
from javax.imageio import ImageIO

import FileNameParser as _fnp     # component paths ("file|C2")

from loci.formats       import ImageReader, FormatTools, ChannelSeparator
from loci.formats.tiff  import TiffParser, TiffCompression


MANIFEST_COMPAT     = ("5", "6")
WORKSPACE_ROOT_FILE = "misp_workspace_root.txt"
QHASH_CHUNK         = 4 * 1024 * 1024
MAX_WORKERS         = 4
MEM_BUDGET_FRAC     = 0.6     # of free JVM heap at start
MAX_STRIP_ROWS      = 512     # compressed strip TIFFs above this: copy L0

# Skip-L1 option (launcher checkbox, on by default): L1 is neither written
# nor read; viewers read L0 where L1 would be used. Entries built without it
# record l1_skipped=1. With the option off, stage 1 builds only the missing
# L1 files (fill_l1=True); other tools just run without them.
SKIP_L1_PREF = "sjleen.misp.pyramid_skip_l1"

def skip_l1_enabled():
    return Prefs.get(SKIP_L1_PREF, "true") == "true"


def unusable_levels(level_paths):
    """Level indices a viewer must not read: absent files, plus L1 under
    the skip option (unless L1 is the coarsest level)."""
    out = set(i for i, p in enumerate(level_paths)
              if i > 0 and not os.path.exists(p))
    if skip_l1_enabled() and len(level_paths) > 2:
        out.add(1)
    return out

# When imported as a module (e.g. by Section Cropper) instead of being
# execfile'd into the MISP globals, define what MISP would provide.
try:
    WORKSPACE_DIR
except NameError:
    WORKSPACE_DIR    = ".misp_workspace"
    MANIFEST_NAME    = "manifest.txt"
    MANIFEST_VERSION = "6"
    PYR_MIN_DIM      = 512

    def bit_depth_to_pixel_type(bd):
        if bd == 8:  return FormatTools.UINT8
        if bd == 16: return FormatTools.UINT16
        if bd == 32: return FormatTools.FLOAT
        raise Exception("Unsupported bit depth: " + str(bd))

    def _locate_module(filename):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        return (p if os.path.exists(p) else None), [p]

# (folder key, source_filename) -> source path, for entries whose level 0 is
# the source. Keyed by folder so two open projects never share entries; kept
# across reload() (Section Cropper reloads this module on every start while
# an earlier window may still read through it).
try:
    _L0_SOURCE
except NameError:
    _L0_SOURCE = {}


def _folder_key(folder):
    return os.path.normcase(os.path.abspath(folder))
_ws_root_cache = [None, False]   # [value, resolved?]


# --------------------------------------------------------------
# Paths
# --------------------------------------------------------------

def _workspace_root_override():
    if _ws_root_cache[1]:
        return _ws_root_cache[0]
    _ws_root_cache[1] = True
    try:
        path, _tried = _locate_module(WORKSPACE_ROOT_FILE)
    except Exception:
        path = None
    if path and os.path.isfile(path):
        f = open(path, 'r')
        try:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    _ws_root_cache[0] = line
                    break
        finally:
            f.close()
    if _ws_root_cache[0]:
        IJ.log("[WS] workspace root: " + _ws_root_cache[0])
    return _ws_root_cache[0]


def workspace_dir(folder):
    root = _workspace_root_override()
    if not root:
        return os.path.join(folder, WORKSPACE_DIR)
    norm = os.path.normcase(os.path.abspath(folder))
    md = MessageDigest.getInstance("MD5")
    md.update(JString(norm).getBytes("UTF-8"))
    tag = ''.join(['%02x' % (b & 0xFF) for b in md.digest()])[:8]
    leaf = os.path.basename(norm.rstrip('\\/')) or "root"
    return os.path.join(root, u"{}_{}".format(leaf, tag))


def ensure_workspace_dir(folder):
    ws = workspace_dir(folder)
    if not os.path.exists(ws):
        os.makedirs(ws)
    return ws


def scaled8(proc, cmin, cmax):
    """8-bit image with the display range cmin..cmax applied. ImageJ's
    convertToByte(True) hands an 8-bit image back UNCHANGED (the range is
    ignored), so 8-bit sources get the same mapping as 16-bit ones here:
    (v - min) * 256 / (max - min + 1), rounded, clipped to 0..255."""
    if proc.getBitDepth() != 8:
        proc.setMinAndMax(cmin, cmax)
        return proc.convertToByte(True)
    lo, hi = float(cmin), float(cmax)
    if lo == 0 and hi == 255:
        return proc.duplicate()
    return _table8(proc, lo, hi)


def _table8(proc, lo, hi):
    scale = 256.0 / max(1.0, hi - lo + 1)
    t = zeros(256, 'i')
    for v in range(256):
        o = int((v - lo) * scale + 0.5)
        t[v] = 0 if v <= lo else (255 if o > 255 else max(0, o))
    d = proc.duplicate()
    d.applyTable(t)
    return d


KEY_SEP = "~"     # folder separator inside a workspace key


def source_key(folder, path):
    """Workspace / manifest key of a source image: its path relative to the
    loaded folder ('01/1-6x16/a.tif' -> '01~1-6x16~a.tif'), so files with
    the same name in different subfolders never clash. A file directly in
    the folder keeps its plain name (older workspaces stay valid)."""
    real, comp = _fnp.split_component(path)
    try:
        rel = os.path.relpath(os.path.abspath(real), os.path.abspath(folder))
    except ValueError:                       # other drive
        rel = os.path.basename(real)
    if rel.startswith(".."):
        rel = os.path.basename(real)
    key = rel.replace("\\", "/").replace("/", KEY_SEP)
    return key + (_fnp.COMP_SEP + comp if comp else "")


def _ws_level_file(folder, source_filename, level):
    """Workspace file for a level (never the source)."""
    real, comp = _fnp.split_component(source_filename)
    stem = os.path.splitext(real)[0] + ("__" + comp if comp else "")
    return os.path.join(workspace_dir(folder),
                        u"{}_L{}.tif".format(stem, level))


def workspace_level_path(folder, source_filename, level):
    """Path to READ a level from (level 0 may be the source)."""
    if level == 0:
        src = _L0_SOURCE.get((_folder_key(folder), source_filename))
        if src:
            return src
    return _ws_level_file(folder, source_filename, level)


def compute_pyramid_dims(w, h):
    """Level 0 (original) -> last level where min(w,h) still > PYR_MIN_DIM."""
    levels = [(w, h)]
    cw, ch = w, h
    while min(cw, ch) > PYR_MIN_DIM:
        cw = max(1, cw // 2)
        ch = max(1, ch // 2)
        levels.append((cw, ch))
    return levels


# --------------------------------------------------------------
# Manifest / integrity
# --------------------------------------------------------------

def md5_of_file(path, block_size=1024*1024):
    path = _fnp.real_path(path)
    md  = MessageDigest.getInstance("MD5")
    fis = FileInputStream(path)
    try:
        buf = zeros(block_size, 'b')
        while True:
            n = fis.read(buf)
            if n <= 0:
                break
            md.update(buf, 0, n)
    finally:
        fis.close()
    return ''.join(['%02x' % (b & 0xFF) for b in md.digest()])


def quick_hash(path):
    """MD5 of (size, first 4 MB, last 4 MB). Reads <= 8 MB."""
    path = _fnp.real_path(path)
    md  = MessageDigest.getInstance("MD5")
    raf = RandomAccessFile(path, "r")
    try:
        size = raf.length()
        md.update(JString(str(size)).getBytes("UTF-8"))
        buf = zeros(QHASH_CHUNK, 'b')
        n = raf.read(buf, 0, QHASH_CHUNK)
        if n > 0:
            md.update(buf, 0, n)
        if size > 2 * QHASH_CHUNK:
            raf.seek(size - QHASH_CHUNK)
            n = raf.read(buf, 0, QHASH_CHUNK)
            if n > 0:
                md.update(buf, 0, n)
    finally:
        raf.close()
    return ''.join(['%02x' % (b & 0xFF) for b in md.digest()])


def file_stats(path):
    st = os.stat(_fnp.real_path(path))   # a component shares its file's stats
    return int(st.st_size), int(st.st_mtime * 1000)


def read_manifest(manifest_path):
    entries = {'__version__': None}
    if not os.path.exists(manifest_path):
        return entries
    current = None
    try:
        f = codecs.open(manifest_path, 'r', 'utf-8')
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('[version:') and line.endswith(']'):
                entries['__version__'] = line[9:-1].strip()
            elif line.startswith('[file:') and line.endswith(']'):
                name = line[6:-1].strip()
                current = {}
                entries[name] = current
            elif '=' in line and current is not None:
                k, v = line.split('=', 1)
                current[k.strip()] = v.strip()
        f.close()
    except Exception as e:
        IJ.log("[WARN] Manifest parse failed: " + unicode(e))
    return entries


_MANIFEST_KEYS = ['size', 'mtime', 'md5', 'qhash',
                  'bit_depth', 'width', 'height',
                  'full_min', 'full_max',
                  'pixel_type', 'little_endian',
                  'rgb_source', 'l0_source', 'l1_skipped',
                  'pyramid_levels']


def write_manifest(manifest_path, entries):
    try:
        f = codecs.open(manifest_path, 'w', 'utf-8')
        f.write("# Multichannel Image Set Previewer manifest\n")
        f.write(u"[version: {}]\n\n".format(MANIFEST_VERSION))
        for name in sorted(entries.keys()):
            if name.startswith('__'):
                continue
            e = entries[name]
            f.write(u"[file: {}]\n".format(name))
            for k in _MANIFEST_KEYS:
                if k in e:
                    f.write(u"{}={}\n".format(k, e[k]))
            level_keys = sorted([k for k in e.keys()
                                 if k.startswith('L') and k[1:2].isdigit()])
            for k in level_keys:
                f.write(u"{}={}\n".format(k, e[k]))
            f.write("\n")
        f.close()
    except Exception as ex:
        IJ.log("[WARN] Manifest write failed: " + unicode(ex))


def verify_file_against_entry(file_path, entry):
    if not entry:
        return False
    try:
        size, mtime = file_stats(file_path)
    except Exception:
        return False
    if (str(size)  == entry.get('size') and
        str(mtime) == entry.get('mtime')):
        return True
    if str(size) != entry.get('size'):
        return False
    try:
        if entry.get('qhash'):
            return quick_hash(file_path) == entry.get('qhash')
        if entry.get('md5'):                      # legacy v5 entry
            return md5_of_file(file_path) == entry.get('md5')
    except Exception:
        pass
    return False


def entry_is_complete(entry, folder, source_filename, source_path=None):
    """Check pyramid_levels field and all level files exist."""
    try:
        n = int(entry.get('pyramid_levels', '0'))
    except Exception:
        return False
    if n < 1:
        return False
    skipped = entry.get("l1_skipped") == "1"   # complete either way
    for i in range(n):
        if i == 1 and skipped:
            continue
        if i == 0 and entry.get("l0_source") == "1":
            p = source_path
        else:
            p = _ws_level_file(folder, source_filename, i)
        if not p or not os.path.exists(p):
            return False
    return True


# --------------------------------------------------------------
# Conversion
# --------------------------------------------------------------

# --------------------------------------------------------------
# Several channels in one file
#   - multi-channel file (stack / composite / OME-TIFF): split per plane
#     -> "<file>|C1", "<file>|C2", ...  (bit depth kept)
#   - RGB image: a single pseudo-colored channel (R:G:B ratio constant)
#     stays one channel (max of R, G, B); a merge of different channels
#     is split -> "<file>|R", "|G", "|B" (empty colors left out)
# Results are cached per file (size + mtime) in the workspace.
# --------------------------------------------------------------

RGB_NAMES  = ("R", "G", "B")
COMP_CACHE = "components.json"
SAMPLE_TILE = 128        # RGB check: SAMPLE_GRID x SAMPLE_GRID tiles of this size (TIFF)
SAMPLE_GRID = 7
SAMPLE_STEP = 8          # every 8th pixel of a tile
COMP_VERSION = 2         # bump when the classification changes (re-probes cached files)
SAMPLE_PX   = 768        # RGB check: subsampled long side (JPG / PNG)


def _bytes_to_ip(buf, pt, w, h, le):
    if pt in (FormatTools.UINT8, FormatTools.INT8):
        return ByteProcessor(w, h, buf)
    bb = ByteBuffer.wrap(buf).order(ByteOrder.LITTLE_ENDIAN if le else ByteOrder.BIG_ENDIAN)
    if pt in (FormatTools.UINT16, FormatTools.INT16):
        arr = zeros(w * h, 'h')
        bb.asShortBuffer().get(arr)
        return ShortProcessor(w, h, arr, None)
    if pt == FormatTools.FLOAT:
        arr = zeros(w * h, 'f')
        bb.asFloatBuffer().get(arr)
        return FloatProcessor(w, h, arr)
    raise Exception("Unsupported pixel type %s" % FormatTools.getPixelTypeString(pt))


STRIP_BYTES = 256 * 1024 * 1024


def read_plane(r, index=0):
    """Whole plane of an open reader as a processor, read in row strips
    (one Java byte array cannot hold a plane above 2 GiB)."""
    w, h = r.getSizeX(), r.getSizeY()
    pt, le = r.getPixelType(), r.isLittleEndian()
    rows = max(1, min(h, STRIP_BYTES // max(1, w * FormatTools.getBytesPerPixel(pt))))
    if rows >= h:
        return _bytes_to_ip(r.openBytes(index), pt, w, h, le)
    out, y = None, 0
    while y < h:
        n = min(rows, h - y)
        part = _bytes_to_ip(r.openBytes(index, 0, y, w, n), pt, w, n, le)
        if out is None:
            out = part.createProcessor(w, h)
        out.insert(part, 0, y)
        y += n
    return out


def _rgb_bytes(real):
    """Bytes per sample of an RGB file (1 for ordinary 24-bit RGB)."""
    r = ImageReader()
    try:
        r.setId(real)
        return FormatTools.getBytesPerPixel(r.getPixelType())
    finally:
        r.close()


def read_component(path):
    """Full-resolution gray processor of one component path."""
    real, comp = _fnp.split_component(path)
    if comp in RGB_NAMES and _rgb_bytes(real) > 1:
        # 16-bit (48-bit) RGB: ImageJ would open it as a 3-slice stack; let
        # Bio-Formats split the samples into planes and keep the bit depth
        r = ChannelSeparator(ImageReader())
        try:
            r.setId(real)
            return read_plane(r, RGB_NAMES.index(comp))
        finally:
            r.close()
    if comp in RGB_NAMES:
        imp = Opener().openImage(real)
        if imp is None:
            raise Exception("Cannot open: " + real)
        cp = imp.getProcessor()
        if not isinstance(cp, ColorProcessor):
            cp = cp.convertToRGB()
        w, h = cp.getWidth(), cp.getHeight()
        ip = cp.getChannel(RGB_NAMES.index(comp) + 1, ByteProcessor(w, h))
        imp.close()
        return ip
    r = ImageReader()
    try:
        r.setId(real)
        return read_plane(r, r.getIndex(0, int(comp[1:]) - 1, 0))   # first z / t
    finally:
        r.close()


def _rgb_samples(r, real, w, h):
    """[(r, g, b)] 8-bit samples spread over the image."""
    out = []
    ext = os.path.splitext(real)[1].lower()
    if ext in ('.jpg', '.jpeg', '.png'):
        stream = ImageIO.createImageInputStream(File(real))
        try:
            rd = ImageIO.getImageReaders(stream).next()
            rd.setInput(stream)
            prm = rd.getDefaultReadParam()
            s = max(1, max(w, h) // SAMPLE_PX)
            prm.setSourceSubsampling(s, s, 0, 0)
            bi = rd.read(0, prm)
            rd.dispose()
        finally:
            stream.close()
        bw, bh = bi.getWidth(), bi.getHeight()
        for v in bi.getRGB(0, 0, bw, bh, None, 0, bw):
            out.append(((v >> 16) & 255, (v >> 8) & 255, v & 255))
        return out
    pt = r.getPixelType()
    bpp = FormatTools.getBytesPerPixel(pt)
    inter = r.isInterleaved()
    tw, th = min(SAMPLE_TILE, w), min(SAMPLE_TILE, h)
    fr = [(i + 0.5) / SAMPLE_GRID for i in range(SAMPLE_GRID)]
    for fy in fr:
        for fx in fr:
            x0 = max(0, min(w - tw, int(w * fx) - tw // 2))
            y0 = max(0, min(h - th, int(h * fy) - th // 2))
            buf = r.openBytes(0, x0, y0, tw, th)
            n = tw * th
            hi = 0 if not r.isLittleEndian() else bpp - 1   # most significant byte
            for i in range(0, n, SAMPLE_STEP):
                if inter:
                    o = i * 3 * bpp + hi
                    out.append((buf[o] & 255, buf[o + bpp] & 255, buf[o + 2 * bpp] & 255))
                else:
                    o = i * bpp + hi
                    out.append((buf[o] & 255, buf[o + n * bpp] & 255, buf[o + 2 * n * bpp] & 255))
    return out


def _classify_rgb(samples):
    """(colors to split out, None) for a merge of different channels, or
    (None, pseudo color (r, g, b) or None) for one pseudo-colored channel."""
    bright = [(r, g, b) for r, g, b in samples if max(r, g, b) >= 32]
    if len(bright) < 50:
        return None, None                 # (nearly) empty: nothing to split
    vr = vg = vb = 0.0
    for r, g, b in bright:
        m = float(max(r, g, b))
        vr += r / m; vg += g / m; vb += b / m
    k = float(len(bright))
    vr, vg, vb = vr / k, vg / k, vb / k
    off = 0
    for r, g, b in bright:
        m = float(max(r, g, b))
        if max(abs(r / m - vr), abs(g / m - vg), abs(b / m - vb)) > 0.25:
            off += 1
    if off < 0.05 * k:                    # constant color ratio: pseudo-color
        return None, (int(255 * vr), int(255 * vg), int(255 * vb))
    need = max(5, int(0.0005 * len(samples)))     # sparse channels count too
    comps = [n for i, n in enumerate(RGB_NAMES)
             if sum(1 for s in samples if s[i] >= 32) >= need]
    return (comps, None) if len(comps) >= 2 else (None, None)


# display colors offered in stage 1 (same names as its LUT list)
LUT_COLORS = {
    "Grays": (255, 255, 255), "Red": (255, 0, 0), "Green": (0, 255, 0),
    "Blue": (0, 0, 255), "Cyan": (0, 255, 255), "Magenta": (255, 0, 255),
    "Yellow": (255, 255, 0), "Orange": (255, 165, 0),
}


def nearest_lut(color):
    """Name of the stage-1 LUT closest in hue to an (r, g, b) color."""
    m = float(max(color))
    if m <= 0:
        return None
    v = [x / m for x in color]
    return min(LUT_COLORS.items(), key=lambda kv: sum(
        (v[i] - kv[1][i] / 255.0) ** 2 for i in range(3)))[0]


def _lut_end(rs, gs, bs):
    """End color of a black-to-color LUT, None for any other LUT."""
    n = len(rs)
    lo = (rs[0] & 255, gs[0] & 255, bs[0] & 255)
    hi = (rs[n - 1] & 255, gs[n - 1] & 255, bs[n - 1] & 255)
    if max(lo) > 40 or max(hi) < 64:
        return None
    return hi


def _tiff_luts(real):
    """{channel index: end color} of LUTs an ImageJ TIFF carries."""
    if not real.lower().endswith(('.tif', '.tiff')):
        return {}
    try:
        fis = Opener.getTiffFileInfo(real)
    except Exception:
        return {}
    if not fis:
        return {}
    fi, out = fis[0], {}
    luts = getattr(fi, 'channelLuts', None)
    if luts:
        for i, l in enumerate(luts):
            if l is not None and len(l) >= 768:
                out[i] = _lut_end(l[0:256], l[256:512], l[512:768])
    elif fi.lutSize > 0 and fi.reds is not None:
        out[0] = _lut_end(fi.reds, fi.greens, fi.blues)
    return dict((k, v) for k, v in out.items() if v)


def _probe_components(real):
    """(components or None, note, colors) for one file on disk;
    colors = {component (or "" for the whole file): (r, g, b)} where the
    file states a pseudo color clearly."""
    r = ImageReader()
    try:
        r.setId(real)
        eff_c, rgb = r.getEffectiveSizeC(), r.getRGBChannelCount()
        zt = r.getSizeZ() * r.getSizeT()
        note = ("%d z / time planes: only the first is used" % zt) if zt > 1 else ""
        if eff_c > 1 and rgb == 1:
            comps = ["C%d" % (i + 1) for i in range(eff_c)]
            luts = _tiff_luts(real)
            return comps, note, dict(("C%d" % (i + 1), c) for i, c in luts.items())
        if rgb >= 3 and eff_c == 1:
            comps, color = _classify_rgb(_rgb_samples(r, real, r.getSizeX(), r.getSizeY()))
            if comps:
                return comps, note, dict((n, LUT_COLORS[{"R": "Red", "G": "Green",
                                                         "B": "Blue"}[n]]) for n in comps)
            return None, note, ({"": color} if color else {})
        luts = _tiff_luts(real)
        return None, note, ({"": luts[0]} if 0 in luts else {})
    finally:
        r.close()


def _channel_key(name, comp=None):
    """Split channels sort R < G < B and C2 < C10 after their file's channel;
    other names alphabetically."""
    if not comp:
        return (name, 2, 0)
    base = name[:-len(comp)].rstrip("-")
    if comp in RGB_NAMES:
        return (base, 0, RGB_NAMES.index(comp))
    return (base, 1, int(comp[1:]) if comp[1:].isdigit() else 0)


def expand_components(result, folder, log=None):
    """Split files that hold several channels into one channel each
    (see above). Channel names: '<channel>-C2' / '<channel>-G', or just
    'C2' / 'G' when the file names carry no channel token."""
    import json
    cache_path = os.path.join(ensure_workspace_dir(folder), COMP_CACHE)
    try:
        f = open(cache_path, 'r')
        try:
            cache = json.load(f)
        finally:
            f.close()
    except Exception:
        cache = {}
    state = {'changed': False}

    def comps_of(real):
        key = source_key(folder, real)
        size, mtime = file_stats(real)
        c = cache.get(key)
        if (not c or c.get("size") != size or c.get("mtime") != mtime
                or c.get("v") != COMP_VERSION):
            try:
                comps, note, colors = _probe_components(real)
            except Exception as e:
                comps, note, colors = None, "channel check failed: %s" % e, {}
            c = {"size": size, "mtime": mtime, "comps": comps, "note": note,
                 "colors": dict((k, list(v)) for k, v in colors.items()),
                 "v": COMP_VERSION}
            cache[key] = c
            state['changed'] = True
            if log and comps:
                log("[WS] %s holds %d channels -> %s" % (key, len(comps), ", ".join(comps)))
            if log and note:
                log("[WS] %s: %s" % (key, note))
        return c

    # every file holds several channels -> the names carry no channel token
    # (a varying token is a slide, not a channel): one set per file
    reals = sorted(set(_fnp.real_path(p) for m in result['sets'].values() for p in m.values()))
    if reals and not result.get('no_channel_token'):
        if all(comps_of(r).get('comps') for r in reals):
            if log:
                log("[WS] every file holds several channels: one set per file")
            result = _fnp._one_set_per_file(reals, result)
        elif log and any(comps_of(r).get("comps") for r in reals):
            log("[WS] WARNING: only some files hold several channels; sets follow the "
                "file names. Check the set list (put such files in separate folders).")
    n_split = 0
    sets = {}
    comp_of = {}                    # split channel name -> component
    votes = {}                      # channel -> {lut name: count}

    def vote(ch, color):
        name = nearest_lut(color) if color else None
        if name and name != "Grays":
            d = votes.setdefault(ch, {})
            d[name] = d.get(name, 0) + 1
    for sid, chmap in result['sets'].items():
        out = {}
        for ch, path in chmap.items():
            real = _fnp.real_path(path)
            c = comps_of(real)
            if c.get('comps'):
                n_split += 1
                for comp in c["comps"]:
                    name = comp if ch == "" else ch + "-" + comp
                    out[name] = real + _fnp.COMP_SEP + comp
                    comp_of[name] = comp
                    vote(name, c.get("colors", {}).get(comp))
            else:
                out[ch or "img"] = path
                vote(ch or "img", c.get("colors", {}).get(""))
        sets[sid] = out
    if state["changed"]:
        try:
            f = open(cache_path, 'w')
            try:
                json.dump(cache, f, indent=1, sort_keys=True)
            finally:
                f.close()
        except Exception as e:
            if log: log("[WS] could not write %s: %s" % (COMP_CACHE, e))
    result['sets'] = sets
    result["channels"] = sorted(set(k for m in sets.values() for k in m),
                                key=lambda n: _channel_key(n, comp_of.get(n)))
    result['split_files'] = n_split
    # pseudo colors the files state clearly -> default stage-1 LUT per channel
    result["channel_luts"] = dict((ch, max(d.items(), key=lambda kv: kv[1])[0])
                                  for ch, d in votes.items())
    if log and result["channel_luts"]:
        log("[WS] colors from the files: " + ", ".join(
            "%s=%s" % kv for kv in sorted(result["channel_luts"].items())))
    return result


def _probe_source(path):
    """Header-only probe. Returns dict(w, h, pt, rgb, direct)."""
    info = {'w': 0, 'h': 0, 'pt': None, 'rgb': False, 'direct': False}
    real, comp = _fnp.split_component(path)
    if comp:                     # one channel of a multi-channel file: gray, copied
        r = ImageReader()
        try:
            r.setId(real)
            info["w"], info["h"] = r.getSizeX(), r.getSizeY()
            info["pt"] = r.getPixelType()        # 16-bit RGB keeps its depth
        finally:
            r.close()
        return info
    r = ImageReader()
    try:
        r.setId(path)
        info['w']   = r.getSizeX()
        info['h']   = r.getSizeY()
        info['pt']  = r.getPixelType()
        info['rgb'] = r.getRGBChannelCount() > 1 or r.getSizeC() > 1
        single = (r.getSeriesCount() == 1 and r.getImageCount() == 1)
        gray_ok = info['pt'] in (FormatTools.UINT8, FormatTools.UINT16,
                                 FormatTools.FLOAT)
    finally:
        try: r.close()
        except Exception: pass

    ext = os.path.splitext(path)[1].lower()
    if ext in ('.tif', '.tiff') and single and gray_ok and not info['rgb']:
        tp = None
        try:
            tp  = TiffParser(path)
            ifd = tp.getFirstIFD()
            if ifd.isTiled():
                info['direct'] = True
            elif ifd.getCompression() == TiffCompression.UNCOMPRESSED:
                info['direct'] = True
            else:
                rps = ifd.getRowsPerStrip()
                info['direct'] = (rps is not None and len(rps) > 0
                                  and rps[0] <= MAX_STRIP_ROWS)
        except Exception as e:
            IJ.log(u"[WS] TIFF probe failed ({}): {}".format(
                os.path.basename(path), e))
        finally:
            try:
                if tp is not None: tp.getStream().close()
            except Exception:
                pass
    return info


def _estimate_bytes(probe):
    w, h = probe['w'], probe['h']
    if probe['rgb']:
        bpp = 4 + 1                  # ColorProcessor + gray copy
    elif probe['pt'] == FormatTools.UINT16:
        bpp = 2
    elif probe['pt'] == FormatTools.FLOAT:
        bpp = 4
    else:
        bpp = 1
    return int(w * h * bpp * 1.5)    # + L1 (0.25) + slack


def _rgb_to_max_gray(cp):
    """ColorProcessor -> ByteProcessor of max(R,G,B). Java-speed."""
    w, h = cp.getWidth(), cp.getHeight()
    out = cp.getChannel(1, ByteProcessor(w, h))
    for c in (2, 3):
        out.copyBits(cp.getChannel(c, ByteProcessor(w, h)), 0, 0,
                     Blitter.MAX)
    return out


def _halve(ip, lw, lh):
    """2x2 average binning (fast); resize() fallback on size mismatch."""
    try:
        out = Binner().shrink(ip, 2, 2, Binner.AVERAGE)
        if out.getWidth() == lw and out.getHeight() == lh:
            return out
    except Exception:
        pass
    return ip.resize(lw, lh, True)


def write_single_level_tiff(level_path, ip_source, pixel_type=None):
    if os.path.exists(level_path):
        try: os.remove(level_path)
        except Exception: pass
    imp = ImagePlus("level", ip_source)
    FileSaver(imp).saveAsTiff(level_path)
    imp.close()


def build_l1_only(source_path, folder, name, entry):
    """Write just L1 (2x2 binned L0) for an entry built without it."""
    t0 = time.time()
    l0 = (source_path if entry.get("l0_source") == "1"
          else _ws_level_file(folder, name, 0))
    imp = Opener().openImage(l0)
    if imp is None:
        raise Exception("Cannot open: " + l0)
    ip = imp.getProcessor()
    if imp.getType() == ImagePlus.COLOR_RGB:
        ip = _rgb_to_max_gray(ip)
    lw, lh = int(entry["L1_w"]), int(entry["L1_h"])
    write_single_level_tiff(_ws_level_file(folder, name, 1), _halve(ip, lw, lh))
    imp.close()
    IJ.log(u"[WS] {}: L1 only {:.1f}s".format(name, time.time() - t0))


def convert_to_workspace(source_path, folder, source_filename, probe=None,
                         skip_l1=False):
    """Open source once, write pyramid levels by cascaded halving."""
    t0 = time.time()
    if probe is None:
        probe = _probe_source(source_path)

    if _fnp.split_component(source_path)[1]:
        imp = None
        ip = read_component(source_path)     # gray, full resolution
        rgb = False
    else:
        imp = Opener().openImage(source_path)
        if imp is None:
            raise Exception("Cannot open: " + source_path)
        ip  = imp.getProcessor()
        rgb = (imp.getType() == ImagePlus.COLOR_RGB)
    t_open = time.time() - t0
    if rgb:
        ip = _rgb_to_max_gray(ip)
    bd = ip.getBitDepth()
    w, h = ip.getWidth(), ip.getHeight()

    if bd == 32:
        ip.resetMinAndMax()
        fmin = float(ip.getMin())
        fmax = float(ip.getMax())
    else:
        fmin = 0.0
        fmax = float(int(2 ** bd) - 1)

    pixel_type  = bit_depth_to_pixel_type(bd)
    levels_dims = compute_pyramid_dims(w, h)
    direct      = probe['direct'] and not rgb
    direct      = direct and not _fnp.split_component(source_path)[1]

    t1 = time.time()
    if not direct:
        write_single_level_tiff(_ws_level_file(folder, source_filename, 0),
                                ip, pixel_type)
    else:
        stale = _ws_level_file(folder, source_filename, 0)
        if os.path.exists(stale):
            try: os.remove(stale)
            except Exception: pass

    # never skip L1 when it is the coarsest level (overview source)
    skip_l1 = skip_l1 and len(levels_dims) > 2
    prev = ip
    for level in range(1, len(levels_dims)):
        lw, lh = levels_dims[level]
        cur = _halve(prev, lw, lh)
        lp = _ws_level_file(folder, source_filename, level)
        if level == 1 and skip_l1:
            if os.path.exists(lp):
                try: os.remove(lp)
                except Exception: pass
        else:
            write_single_level_tiff(lp, cur, pixel_type)
        prev = cur
    t_levels = time.time() - t1

    if imp is not None:
        imp.close()
    imp = None; ip = None; prev = None

    IJ.log(u"[WS] {}: open {:.1f}s, levels {:.1f}s ({} lv, L0 {})".format(
        source_filename, t_open, t_levels, len(levels_dims),
        "source" if direct else "copied" + (" from RGB" if rgb else "")
        ) + (", L1 skipped" if skip_l1 else ""))

    return {
        'bit_depth'    : bd,
        'width'        : w,
        'height'       : h,
        'full_min'     : fmin,
        'full_max'     : fmax,
        'pixel_type'   : pixel_type,
        'little_endian': True,
        'levels_dims'  : levels_dims,
        'rgb_source'   : rgb,
        'l0_source'    : direct,
        'l1_skipped'   : skip_l1,
    }


# --------------------------------------------------------------
# Workspace preparation (parallel)
# --------------------------------------------------------------

class _MemBudget(object):
    def __init__(self, total):
        self.total = total
        self.used  = 0
        self.cond  = threading.Condition()

    def acquire(self, n):
        self.cond.acquire()
        try:
            # A job larger than the budget runs alone.
            while self.used > 0 and self.used + n > self.total:
                self.cond.wait()
            self.used += n
        finally:
            self.cond.release()

    def release(self, n):
        self.cond.acquire()
        try:
            self.used -= n
            self.cond.notifyAll()
        finally:
            self.cond.release()


def _migrate_keys(folder, manifest, src_paths):
    """Workspaces from before source_key() were keyed by the bare file
    name: hand such a pyramid to the file it was built from (same size /
    mtime / hash) under its new key, renaming the level files."""
    for key, path in src_paths.items():
        old = os.path.basename(path)
        if key == old or key in manifest or old not in manifest:
            continue
        e = manifest[old]
        if not verify_file_against_entry(path, e):
            continue
        try:
            n = int(e.get('pyramid_levels', '0'))
        except ValueError:
            n = 0
        try:
            for i in range(n):
                a, b = _ws_level_file(folder, old, i), _ws_level_file(folder, key, i)
                if os.path.exists(a):
                    os.rename(a, b)
        except OSError as ex:
            IJ.log(u"[WS] could not rename the pyramid of {}: {}".format(old, ex))
            continue
        manifest[key] = manifest.pop(old)
        IJ.log(u"[WS] pyramid {} -> {}".format(old, key))


def _register_l0(folder, manifest, src_paths):
    fk = _folder_key(folder)
    for k in [k for k in _L0_SOURCE if k[0] == fk]:
        del _L0_SOURCE[k]
    for name, path in src_paths.items():
        e = manifest.get(name)
        if e and e.get("l0_source") == "1":
            _L0_SOURCE[(fk, name)] = path


def _cancelled(progress):
    """Cancel state of either progress dialog type (MISP or Cropper)."""
    f = getattr(progress, "is_cancelled", None)
    if callable(f):
        return bool(f())
    return bool(getattr(progress, "cancelled", False))


def prepare_workspace(folder, parse_result, progress, fill_l1=False):
    """fill_l1: with the skip-L1 option off, build the L1 files that an
    earlier skip-L1 run left out (stage 1 only)."""
    t_start = time.time()
    ensure_workspace_dir(folder)
    manifest_path = os.path.join(workspace_dir(folder), MANIFEST_NAME)
    manifest      = read_manifest(manifest_path)

    mver = manifest.get('__version__')
    if mver not in MANIFEST_COMPAT:
        IJ.log(u"[INFO] Manifest version {} -> {}, reconverting all.".format(
            mver, MANIFEST_VERSION))
        for k in [k for k in manifest.keys() if not k.startswith('__')]:
            del manifest[k]

    skip_l1 = skip_l1_enabled()
    if skip_l1:
        IJ.log("[WS] skip-L1 option on: L1 is not written or read")

    src_paths = {}
    for set_id, ch_map in parse_result['sets'].items():
        for ch, path in ch_map.items():
            src_paths[source_key(folder, path)] = path
    _migrate_keys(folder, manifest, src_paths)
    names = sorted(src_paths.keys())
    total = len(names)

    # 1. Validity check (cheap: size+mtime, quick hash on mismatch)
    todo = []
    for idx, name in enumerate(names):
        progress.update(idx, u"Checking {} ({}/{})".format(name, idx + 1, total))
        path  = src_paths[name]
        entry = manifest.get(name, {})
        if not (verify_file_against_entry(path, entry) and
                entry_is_complete(entry, folder, name, path)):
            todo.append(name)
    n_ok = total - len(todo)
    IJ.log(u"[WS] {} cached, {} to convert".format(n_ok, len(todo)))

    # 1b. L1-only jobs
    l1_todo = []
    if fill_l1 and not skip_l1:
        for name in names:
            e = manifest.get(name, {})
            if (name not in todo and e.get("l1_skipped") == "1"
                    and int(e.get("pyramid_levels", "0")) > 2):
                l1_todo.append(name)
        if l1_todo:
            IJ.log(u"[WS] building L1 only for {} image(s)".format(len(l1_todo)))
    l1_set = set(l1_todo)
    n_ok -= len(l1_todo)

    # 2. Parallel conversion
    if todo or l1_todo:
        rt = Runtime.getRuntime()
        free_heap = rt.maxMemory() - (rt.totalMemory() - rt.freeMemory())
        budget = _MemBudget(max(1, int(free_heap * MEM_BUDGET_FRAC)))
        n_workers = max(1, min(MAX_WORKERS, rt.availableProcessors(),
                               len(todo) + len(l1_todo)))
        lock  = threading.Lock()
        queue = list(todo) + l1_todo
        done  = [n_ok]

        # time left from the bytes to convert (a file holding several
        # channels is shared between them; an L1-only job reads less)
        users = {}
        for nm in queue:
            rp = _fnp.real_path(src_paths[nm])
            users[rp] = users.get(rp, 0) + 1
        def _work(nm):
            rp = _fnp.real_path(src_paths[nm])
            try:
                w = float(os.path.getsize(rp)) / users[rp]
            except OSError:
                w = 1.0
            return w * (0.3 if nm in l1_set else 1.0)
        weights = dict((nm, _work(nm)) for nm in queue)
        eta = getattr(progress, 'eta', None)
        if eta:
            eta.set_total(sum(weights.values()))

        def worker():
            while True:
                lock.acquire()
                try:
                    if not queue or _cancelled(progress):
                        return
                    name = queue.pop(0)
                finally:
                    lock.release()
                path = src_paths[name]
                try:
                    probe = _probe_source(path)
                    need  = _estimate_bytes(probe)
                except Exception:
                    probe, need = None, budget.total
                budget.acquire(need)
                try:
                    progress.update(done[0], u"Converting {} ({}/{})".format(
                        name, done[0] + 1, total))
                    if name in l1_set:
                        ent = dict(manifest[name])
                        build_l1_only(path, folder, name, ent)
                        ent["l1_skipped"] = "0"
                        lock.acquire()
                        try:
                            manifest[name] = ent
                        finally:
                            lock.release()
                        continue
                    info = convert_to_workspace(path, folder, name, probe, skip_l1)
                    size, mtime = file_stats(path)
                    try: qh = quick_hash(path)
                    except Exception: qh = ""
                    ent = {
                        'size'          : str(size),
                        'mtime'         : str(mtime),
                        'qhash'         : qh,
                        'bit_depth'     : str(info['bit_depth']),
                        'width'         : str(info['width']),
                        'height'        : str(info['height']),
                        'full_min'      : str(info['full_min']),
                        'full_max'      : str(info['full_max']),
                        'pixel_type'    : str(info['pixel_type']),
                        'little_endian' : "1" if info['little_endian'] else "0",
                        'rgb_source'    : "1" if info['rgb_source'] else "0",
                        'l0_source'     : "1" if info['l0_source'] else "0",
                        'l1_skipped'    : "1" if info.get('l1_skipped') else "0",
                        'pyramid_levels': str(len(info['levels_dims'])),
                    }
                    for li, (lw, lh) in enumerate(info['levels_dims']):
                        ent[u'L{}_w'.format(li)] = str(lw)
                        ent[u'L{}_h'.format(li)] = str(lh)
                    lock.acquire()
                    try:
                        manifest[name] = ent
                    finally:
                        lock.release()
                except Exception as e:
                    IJ.log(u"[ERROR] Convert failed for {}: {}".format(name, e))
                    IJ.log(traceback.format_exc())
                finally:
                    budget.release(need)
                    if eta:
                        eta.add(weights[name])
                    lock.acquire()
                    try:
                        done[0] += 1
                        d = done[0]
                    finally:
                        lock.release()
                    progress.update(d, u"Converted {}/{}".format(d, total))

        IJ.log(u"[WS] converting with {} worker(s), budget {} MB".format(
            n_workers, budget.total // (1024 * 1024)))
        threads = [threading.Thread(target=worker) for _ in range(n_workers)]
        for t in threads: t.start()
        for t in threads: t.join()
        JSystem.gc()

    # 3. Orphan cleanup (workspace files only -- never the source)
    orphans = [n for n in manifest.keys()
               if (not n.startswith('__')) and n not in src_paths]
    if orphans:
        # only images that are gone from the folder; one that is merely
        # unchecked or filtered out keeps its pyramid
        present = set()
        for d, dirs, files in os.walk(folder):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            present.update(source_key(folder, os.path.join(d, f)) for f in files)
        orphans = [n for n in orphans if _fnp.real_path(n) not in present]
    for n in orphans:
        try:
            n_lv = int(manifest[n].get('pyramid_levels', '0'))
        except Exception:
            n_lv = 0
        for i in range(n_lv):
            p = _ws_level_file(folder, n, i)
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass
        del manifest[n]

    manifest['__version__'] = MANIFEST_VERSION
    write_manifest(manifest_path, manifest)
    _register_l0(folder, manifest, src_paths)
    IJ.log(u"[WS] workspace ready in {:.1f}s".format(time.time() - t_start))
    progress.update(total, "Workspace ready.")
    return manifest
