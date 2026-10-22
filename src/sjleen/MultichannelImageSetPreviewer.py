# ==============================================================
#  Multichannel Image Set Previewer - Phase 2
#  (B-plan: one file per pyramid level)
#
#  Fiji/ImageJ Jython Script
#
#  Features
#    - Workspace cache: each pyramid level saved as a separate
#      single-series tiled OME-TIFF in .misp_workspace/
#    - Per-tile LRU cache keyed by (level_file_path, tx, ty)
#    - Viewport-based tile reading at the optimal pyramid level
#    - Manifest-based integrity check (name + size + mtime + MD5)
#    - Grayscale display grid; LUT only at export
#    - Cursor-centered wheel zoom; bulk zoom buttons in top bar
#    - Contrast min/max fields + slider, LUT dropdown with swatches
#    - Uniform pixel scale across panels (max image dims as ref)
# ==============================================================

import os
import sys
import math

from ij         import IJ, ImagePlus
from ij.io      import Opener, FileSaver
from ij.process import (ImageProcessor, ByteProcessor,
                        ShortProcessor, FloatProcessor)

from java.awt           import (BorderLayout, GridLayout, Dimension, Color,
                                 Font, Component, Insets)
from java.awt.geom      import AffineTransform
from java.awt.image     import BufferedImage
from java.awt.event     import (MouseAdapter, MouseMotionAdapter,
                                 MouseWheelListener, ComponentAdapter,
                                 ActionListener, WindowAdapter)
from javax.swing        import (JFrame, JPanel, JLabel, JButton, JCheckBox,
                                 JComboBox, JFileChooser, JOptionPane,
                                 JScrollPane, BorderFactory,
                                 SwingUtilities, Box, BoxLayout,
                                 JTextField, JProgressBar, JSlider,
                                 ImageIcon, DefaultListCellRenderer,
                                 ScrollPaneConstants, Timer as SwingTimer)
from javax.swing.border import EmptyBorder
from javax.swing.event  import ChangeListener
from java.lang          import Runnable, Thread as JThread, System as JSystem
from java.security      import MessageDigest
from java.io            import FileInputStream
from java.nio           import ByteBuffer, ByteOrder
from jarray             import zeros

# Bio-Formats
from loci.formats           import ImageReader, FormatTools, MetadataTools
from loci.formats.out       import OMETiffWriter
from loci.formats.services  import OMEXMLService
from loci.common.services   import ServiceFactory

# --------------------------------------------------------------
# Import FileNameParser
# --------------------------------------------------------------

# Build a list of candidate directories to add to sys.path.
# When invoked from the Fiji plugin menu, __file__ is NOT defined,
# so we must search the plugins folder (and its sub-directories)
# explicitly.
_path_candidates = []

# 1. Script's own directory (only available when run via macro editor)
try:
    if '__file__' in dir():
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here:
            _path_candidates.append(_here)
except Exception:
    pass

# 2. Plugins root
try:
    _plugins = IJ.getDirectory("plugins")
    if _plugins:
        _path_candidates.append(_plugins)
        # 3. All sub-directories of plugins/ that contain a *.py file
        for _root, _dirs, _files in os.walk(_plugins):
            _dirs[:] = [_d for _d in _dirs if not _d.startswith('.')
                        and _d.lower() not in ('jars', 'java', 'lib',
                                               'macros', 'luts', 'fonts',
                                               'images', 'samples')]
            if any(_f.endswith('.py') for _f in _files):
                if _root not in _path_candidates:
                    _path_candidates.append(_root)
except Exception:
    pass

for _p in _path_candidates:
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

PARSER_LOADED = False
try:
    # prefer the copy next to the MISP modules; drop a stale one that an
    # older script may have loaded (e.g. one without parse_files)
    for _p in _path_candidates:
        if os.path.exists(os.path.join(_p, "FileNameParser.py")):
            if _p in sys.path:
                sys.path.remove(_p)
            sys.path.insert(0, _p)
            break
    _m = sys.modules.get('FileNameParser')
    if _m is not None and not hasattr(_m, "split_component"):
        del sys.modules['FileNameParser']
    from FileNameParser import (parse_folder, parse_files, build_output_filename,
                                file_stem, real_path)
    PARSER_LOADED = True
except ImportError:
    import traceback as _tb
    IJ.log("[WARN] FileNameParser.py not found:\n" + _tb.format_exc())


# ==============================================================
# Constants
# ==============================================================

WINDOW_W          = 1720
WINDOW_H          = 960
DEFAULT_CELLS_ROW = 4
MIN_CELL_W        = 180
MIN_CELL_H        = 140
CONTRAST_PANEL_W  = 240
TILE_SIZE         = 512
TILE_CACHE_SIZE   = 800
WORKSPACE_DIR     = ".misp_workspace"
MANIFEST_NAME     = "manifest.txt"
MANIFEST_VERSION  = "6"       # v6: L0 may be the source; v5 still accepted

# Pyramid levels continue until min side <= TILE_SIZE
PYR_MIN_DIM = TILE_SIZE

ZOOM_STEPS       = [1.0, 2.0, 4.0, 8.0, 16.0,
                    32.0, 64.0, 128.0]    # 1.0 = fit (minimum)
ZOOM_DEFAULT_IDX = 0    # 1.0x

PANEL_BG   = Color( 40,  40,  40)
HEADER_BG  = Color( 55,  55,  55)
CELL_BG    = Color( 30,  30,  30)
BORDER_CLR = Color( 80,  80,  80)
TEXT_CLR   = Color(220, 220, 220)
ACCENT_CLR = Color( 70, 130, 180)
MISSING_BG = Color( 50,  30,  30)

LUT_PRESETS = [
    ("Grays",    Color.WHITE),
    ("Red",      Color(255,   0,   0)),
    ("Green",    Color(  0, 255,   0)),
    ("Blue",     Color(  0,   0, 255)),
    ("Cyan",     Color(  0, 255, 255)),
    ("Magenta",  Color(255,   0, 255)),
    ("Yellow",   Color(255, 255,   0)),
    ("Orange",   Color(255, 165,   0)),
]
LUT_BY_NAME = dict(LUT_PRESETS)


# ==============================================================
# Small Runnable helpers
# ==============================================================

class _Do(Runnable):
    """Run a plain function (e.g. on the EDT via invokeLater)."""
    def __init__(self, fn):
        self.fn = fn
    def run(self):
        self.fn()


class _SetScroll(Runnable):
    """Restore a scrollbar value on the EDT after relayout."""
    def __init__(self, scrollbar, value):
        self.sb    = scrollbar
        self.value = value
    def run(self):
        self.sb.setValue(self.value)



# ==============================================================
# Workspace / pyramid / manifest code lives in
# MultichannelImageSetPreviewerWorkspace.py (loaded at the bottom).
# ==============================================================


# ==============================================================
# Pixel <-> byte helpers (Java-native via NIO)
# ==============================================================

def pixels_to_bytes_le(pixels, pixel_type, n_pixels):
    if pixel_type == FormatTools.UINT8:
        return pixels
    if pixel_type == FormatTools.UINT16:
        buf = zeros(n_pixels * 2, 'b')
        bb  = ByteBuffer.wrap(buf)
        bb.order(ByteOrder.LITTLE_ENDIAN)
        bb.asShortBuffer().put(pixels)
        return buf
    if pixel_type == FormatTools.FLOAT:
        buf = zeros(n_pixels * 4, 'b')
        bb  = ByteBuffer.wrap(buf)
        bb.order(ByteOrder.LITTLE_ENDIAN)
        bb.asFloatBuffer().put(pixels)
        return buf
    raise Exception("Unsupported pixel type: " + str(pixel_type))


def bytes_to_processor(buf, pixel_type, w, h, little_endian):
    n  = w * h
    bo = ByteOrder.LITTLE_ENDIAN if little_endian else ByteOrder.BIG_ENDIAN
    if pixel_type == FormatTools.UINT8 or pixel_type == FormatTools.INT8:
        return ByteProcessor(w, h, buf, None)
    if pixel_type == FormatTools.UINT16 or pixel_type == FormatTools.INT16:
        shorts = zeros(n, 'h')
        bb     = ByteBuffer.wrap(buf)
        bb.order(bo)
        bb.asShortBuffer().get(shorts)
        return ShortProcessor(w, h, shorts, None)
    if (pixel_type == FormatTools.FLOAT or
        pixel_type == FormatTools.DOUBLE):
        floats = zeros(n, 'f')
        bb     = ByteBuffer.wrap(buf)
        bb.order(bo)
        bb.asFloatBuffer().get(floats)
        return FloatProcessor(w, h, floats)
    raise Exception("Unsupported pixel type: " + str(pixel_type))


def bit_depth_to_pixel_type(bd):
    if bd == 8:  return FormatTools.UINT8
    if bd == 16: return FormatTools.UINT16
    if bd == 32: return FormatTools.FLOAT
    raise Exception("Unsupported bit depth: " + str(bd))



# ==============================================================
# Tile reader pool + tile LRU cache
#
# Each level file has its own reader. Cache key = (path, tx, ty).
# ==============================================================

class TileReaderPool(object):
    def __init__(self):
        self.readers = {}

    def get(self, path):
        r = self.readers.get(path)
        if r is None:
            r = ImageReader()
            r.setId(path)
            self.readers[path] = r
        return r

    def close_all(self):
        for r in self.readers.values():
            try: r.close()
            except Exception: pass
        self.readers = {}


class TileCache(object):
    def __init__(self, max_tiles=TILE_CACHE_SIZE):
        self.max   = max_tiles
        self.store = {}
        self.order = []

    def get(self, key):
        if key in self.store:
            self.order.remove(key)
            self.order.append(key)
            return self.store[key]
        return None

    def put(self, key, proc):
        if key in self.store:
            self.order.remove(key)
        self.store[key] = proc
        self.order.append(key)
        while len(self.order) > self.max:
            victim = self.order.pop(0)
            del self.store[victim]


def read_tile(reader_pool, tile_cache, path, tx, ty, tw, th,
              pixel_type, little_endian):
    """Read tile at (path, tx, ty). Cached.

    Uses the reader's own pixel_type and endianness, since ImageJ
    FileSaver writes Big-Endian TIFFs while our caller may assume
    Little-Endian.
    """
    key = (path, tx, ty)
    cached = tile_cache.get(key)
    if cached is not None:
        return cached
    reader = reader_pool.get(path)
    buf    = reader.openBytes(0, tx, ty, tw, th)
    pt_actual = reader.getPixelType()
    le_actual = reader.isLittleEndian()
    proc   = bytes_to_processor(buf, pt_actual, tw, th, le_actual)
    tile_cache.put(key, proc)
    return proc


# ==============================================================
# LUT combobox cell renderer
# ==============================================================

class LutSwatchRenderer(DefaultListCellRenderer):
    def getListCellRendererComponent(self, list_, value, index,
                                     isSelected, cellHasFocus):
        lbl = DefaultListCellRenderer.getListCellRendererComponent(
            self, list_, value, index, isSelected, cellHasFocus)
        color = LUT_BY_NAME.get(str(value), Color.WHITE)
        icon  = BufferedImage(18, 14, BufferedImage.TYPE_INT_ARGB)
        g2    = icon.createGraphics()
        g2.setColor(color);      g2.fillRect(0, 0, 18, 14)
        g2.setColor(Color.BLACK); g2.drawRect(0, 0, 17, 13)
        g2.dispose()
        lbl.setIcon(ImageIcon(icon))
        lbl.setText("  " + str(value))
        return lbl


# ==============================================================
# MissingPanel
# ==============================================================

class MissingPanel(JPanel):
    def __init__(self):
        JPanel.__init__(self)
        self.setBackground(MISSING_BG)
        self.setBorder(BorderFactory.createLineBorder(Color(100, 50, 50), 1))

    def paintComponent(self, g):
        super(MissingPanel, self).paintComponent(g)
        g.setColor(Color(120, 60, 60))
        g.setFont(Font("SansSerif", Font.ITALIC, 12))
        fm = g.getFontMetrics()
        msg = "missing"
        g.drawString(msg,
                     (self.getWidth()  - fm.stringWidth(msg)) // 2,
                     (self.getHeight() + fm.getAscent())      // 2)


# ==============================================================
# ImagePanel  --  pyramidal tile-based rendering
#
# Coordinates (cx, cy) are ALWAYS in level-0 pixels.
# Rendering picks the best pyramid level and reads from that
# level's file.
# ==============================================================

class ImagePanel(JPanel):

    def __init__(self, level_paths, level_dims,
                 bit_depth, full_min, full_max,
                 pixel_type, little_endian,
                 ref_w, ref_h,
                 reader_pool, tile_cache,
                 set_id=None):
        JPanel.__init__(self)
        self.level_paths    = level_paths    # [L0_path, L1_path, ...]
        self.level_dims     = level_dims     # [(w0,h0), (w1,h1), ...]
        self.n_levels       = len(level_paths)
        # levels not to read: absent files, and L1 under the skip-L1 option
        self.missing_levels = unusable_levels(level_paths)
        self.img_w          = level_dims[0][0]
        self.img_h          = level_dims[0][1]
        self.bit_depth      = bit_depth
        self.full_min       = full_min
        self.full_max       = full_max
        self.pixel_type     = pixel_type
        self.little_endian  = little_endian
        self.ref_w          = ref_w
        self.ref_h          = ref_h
        self.reader_pool    = reader_pool
        self.tile_cache     = tile_cache
        self.set_id         = set_id

        # Sync callback (installed by MainWindow).
        # Signature: fn(source_panel, cx, cy, zoom)
        # The callback propagates view state to other panels in the
        # same set when sync is enabled.
        self.sync_cb        = None

        self.cx   = self.img_w / 2.0
        self.cy   = self.img_h / 2.0
        self.zoom = 1.0

        self.contrast_min = full_min
        self.contrast_max = full_max

        # Saturation overlay state (per-panel; controlled by ContrastChannelPanel)
        self.show_sat_under = True
        self.show_sat_over  = True

        self._drag_prev    = None
        self._render_cache = None

        # Stage-1 slide orientation (display only; cx/cy stay in
        # source pixels). See orient_* helpers in the Project module.
        self.view_rot  = 0
        self.view_flip = False

        self.setBackground(CELL_BG)
        self.setBorder(BorderFactory.createLineBorder(BORDER_CLR, 1))

        get_async(tile_cache).register(self, level_paths)

        self.addMouseWheelListener(self._make_wheel())
        self.addMouseListener(self._make_mouse())
        self.addMouseMotionListener(self._make_motion())

    def set_orientation(self, rot, flip):
        self.view_rot  = rot
        self.view_flip = bool(flip)
        self._render_cache = None
        self.repaint()

    def set_contrast(self, cmin, cmax):
        self.contrast_min = cmin
        self.contrast_max = cmax
        self._render_cache = None
        self.repaint()

    def set_show_sat_under(self, on):
        if on == self.show_sat_under:
            return
        self.show_sat_under = on
        self._render_cache = None
        self.repaint()

    def set_show_sat_over(self, on):
        if on == self.show_sat_over:
            return
        self.show_sat_over = on
        self._render_cache = None
        self.repaint()

    def set_zoom(self, z):
        self.zoom = z
        self.cx = self.img_w / 2.0
        self.cy = self.img_h / 2.0
        self._render_cache = None
        self.repaint()

    def apply_view(self, cx, cy, zoom):
        """Externally set view state (used for set-sync). Does not
        trigger the sync callback, to avoid feedback loops."""
        self.cx   = cx
        self.cy   = cy
        self.zoom = zoom
        self._render_cache = None
        self.repaint()

    def _notify_sync(self):
        """Called after any local view change. Invokes sync_cb if set."""
        if self.sync_cb is not None:
            try:
                self.sync_cb(self, self.cx, self.cy, self.zoom)
            except Exception as e:
                IJ.log("[WARN] sync_cb failed: " + unicode(e))

    # -- coord transforms --------------------------------------

    def _fit_scale(self):
        pw = max(1, self.getWidth())
        ph = max(1, self.getHeight())
        return min(float(pw) / self.ref_w, float(ph) / self.ref_h)

    def _effective_scale(self):
        return self._fit_scale() * self.zoom

    def _panel_to_img(self, px, py):
        s  = self._effective_scale()
        bw, bh = orient_buf_dims(self)
        bx, by = panel_to_buf(self, px, py)
        ix = (bx - bw / 2.0) / s + self.cx
        iy = (by - bh / 2.0) / s + self.cy
        return ix, iy

    def _pick_level(self):
        """Pick smallest level L with eff * 2^L >= 1."""
        eff = self._effective_scale()
        if eff <= 0:
            return 0
        try:
            target = int(math.ceil(math.log(1.0 / eff, 2)))
        except Exception:
            target = 0
        target = max(0, min(self.n_levels - 1, target))
        while target > 0 and target in self.missing_levels:
            target -= 1
        return target

    # -- rendering ---------------------------------------------

    def paintComponent(self, g):
        super(ImagePanel, self).paintComponent(g)
        pw = self.getWidth()
        ph = self.getHeight()
        if pw <= 0 or ph <= 0:
            return
        bw, bh = orient_buf_dims(self)
        bi = self._get_render(bw, bh)
        if bi is None:
            g.setColor(Color(80, 80, 80))
            g.setFont(Font("SansSerif", Font.PLAIN, 11))
            g.drawString("Loading...", 10, ph // 2)
            return
        g.drawImage(bi, orient_transform(self, bw, bh), None)

    def _get_render(self, pw, ph):
        key = (pw, ph,
               round(self.cx, 2), round(self.cy, 2),
               round(self.zoom, 4),
               self.contrast_min, self.contrast_max)
        if self._render_cache and self._render_cache[0] == key:
            return self._render_cache[1]

        eff = self._effective_scale()
        if eff <= 0:
            return None

        level        = self._pick_level()
        lw, lh       = self.level_dims[level]
        level_path   = self.level_paths[level]
        scale_factor = float(lw) / self.img_w
        eff_L        = eff / scale_factor

        cx_L = self.cx * scale_factor
        cy_L = self.cy * scale_factor

        half_iw_L = (pw / 2.0) / eff_L
        half_ih_L = (ph / 2.0) / eff_L

        ix0_f = cx_L - half_iw_L
        iy0_f = cy_L - half_ih_L
        ix1_f = cx_L + half_iw_L
        iy1_f = cy_L + half_ih_L

        ix0 = max(0, int(math.floor(ix0_f)))
        iy0 = max(0, int(math.floor(iy0_f)))
        ix1 = min(lw, int(math.ceil(ix1_f)))
        iy1 = min(lh, int(math.ceil(iy1_f)))

        if ix1 <= ix0 or iy1 <= iy0:
            bi = BufferedImage(pw, ph, BufferedImage.TYPE_INT_ARGB)
            g2 = bi.createGraphics()
            g2.setColor(CELL_BG); g2.fillRect(0, 0, pw, ph)
            g2.dispose()
            self._render_cache = (key, bi)
            return bi

        rw = ix1 - ix0
        rh = iy1 - iy0

        if self.pixel_type == FormatTools.UINT8:
            region_proc = ByteProcessor(rw, rh)
        elif self.pixel_type == FormatTools.UINT16:
            region_proc = ShortProcessor(rw, rh)
        else:
            region_proc = FloatProcessor(rw, rh)

        # Overview first (coarse), then whatever tiles are already loaded;
        # missing tiles are fetched in the background (see AsyncTiles)
        loader = get_async(self.tile_cache)
        complete = True
        try:
            ov = loader.overview(self.level_paths[-1], self.reader_pool)
            fill_from_overview(region_proc, ov, self.level_dims, level, ix0, iy0, rw, rh)
        except Exception as e:
            IJ.log(u"[WARN] overview {}: {}".format(self.level_paths[-1], e))
        tile_x0 = (ix0 // TILE_SIZE) * TILE_SIZE
        tile_y0 = (iy0 // TILE_SIZE) * TILE_SIZE

        ty = tile_y0
        while ty < iy1:
            tx = tile_x0
            while tx < ix1:
                tile_w = min(TILE_SIZE, lw - tx)
                tile_h = min(TILE_SIZE, lh - ty)
                tile_proc = loader.get(level_path, tx, ty, tile_w, tile_h,
                                       self.pixel_type, self.little_endian)
                if tile_proc is None:
                    complete = False
                else:
                    region_proc.insert(tile_proc, tx - ix0, ty - iy0)
                tx += TILE_SIZE
            ty += TILE_SIZE

        # Contrast + 8-bit
        # 8-bit sources too (convertToByte ignores the range of an 8-bit image)
        proc8 = scaled8(region_proc, self.contrast_min, self.contrast_max)

        draw_w = max(1, int(round(rw * eff_L)))
        draw_h = max(1, int(round(rh * eff_L)))

        s_px0 = pw / 2.0 + (ix0 - cx_L) * eff_L
        s_py0 = ph / 2.0 + (iy0 - cy_L) * eff_L

        bi = BufferedImage(pw, ph, BufferedImage.TYPE_INT_ARGB)
        g2 = bi.createGraphics()
        g2.setColor(CELL_BG); g2.fillRect(0, 0, pw, ph)

        # Saturation overlay: 8-bit conversion already clipped under-range
        # to 0 and over-range to 255; an indexed color table shows 0 blue
        # and 255 red (per toggle). No per-pixel loop (see sat_image).
        src_img = sat_image(proc8, self.show_sat_under, self.show_sat_over)
        src_w   = src_img.getWidth()
        src_h   = src_img.getHeight()

        at = AffineTransform()
        at.translate(float(s_px0), float(s_py0))
        at.scale(float(draw_w) / src_w, float(draw_h) / src_h)
        g2.drawImage(src_img, at, None)

        # Level indicator
        g2.setColor(Color(160, 160, 60))
        g2.setFont(Font("SansSerif", Font.PLAIN, 9))
        g2.drawString(u"L{}".format(level), 4, 12)
        g2.dispose()

        # incomplete renders are not cached: the loader repaints us
        self._render_cache = (key, bi) if complete else None
        return bi

    # -- interaction -------------------------------------------

    def _make_wheel(self):
        panel = self
        class WL(MouseWheelListener):
            def mouseWheelMoved(self, e):
                factor = 0.9 if e.getWheelRotation() > 0 else 1.1
                new_zoom = max(1.0, min(panel.zoom * factor, 200.0))   # never below fit
                if new_zoom == panel.zoom:
                    return
                mx, my = e.getX(), e.getY()
                ix, iy = panel._panel_to_img(mx, my)
                panel.zoom = new_zoom
                bw, bh = orient_buf_dims(panel)
                bx, by = panel_to_buf(panel, mx, my)
                s  = panel._effective_scale()
                panel.cx = ix - (bx - bw / 2.0) / s
                panel.cy = iy - (by - bh / 2.0) / s
                if panel.zoom <= 1.0:            # back at fit: centre the image
                    panel.cx, panel.cy = panel.img_w / 2.0, panel.img_h / 2.0
                panel._render_cache = None
                panel.repaint()
                panel._notify_sync()
        return WL()

    def _make_mouse(self):
        panel = self
        class ML(MouseAdapter):
            def mousePressed(self, e):
                # pan with right/middle drag only (same as Section Cropper)
                if e.getButton() != 1:
                    panel._drag_prev = (e.getX(), e.getY())
            def mouseReleased(self, e):
                panel._drag_prev = None
            def mouseClicked(self, e):
                if e.getClickCount() == 2 and e.getButton() == 1:   # left button
                    toggle_zoom(panel, e.getX(), e.getY())
        return ML()

    def _make_motion(self):
        panel = self
        class MML(MouseMotionAdapter):
            def mouseDragged(self, e):
                if panel._drag_prev is None:
                    return
                dx = e.getX() - panel._drag_prev[0]
                dy = e.getY() - panel._drag_prev[1]
                panel._drag_prev = (e.getX(), e.getY())
                s = panel._effective_scale()
                dx, dy = panel_delta_to_buf(panel, dx, dy)
                panel.cx -= dx / s
                panel.cy -= dy / s
                panel._render_cache = None
                panel.repaint()
                panel._notify_sync()
        return MML()


# ==============================================================
# MergedImagePanel and MergedContrastPanel classes are defined
# in MultichannelImageSetPreviewerPanels.py and injected via execfile (see the
# bottom of this file) to keep the main module below Jython's
# parser buffer limit.
# ==============================================================

# ==============================================================
# ContrastChannelPanel
# ==============================================================

class ContrastChannelPanel(JPanel):

    def __init__(self, channel_name, bit_depth, image_panels):
        JPanel.__init__(self)
        self.channel_name = channel_name
        self.bit_depth    = bit_depth
        self.image_panels = image_panels         # currently VISIBLE panels
        self.all_image_panels = list(image_panels)  # full (unfiltered) list
        self.lut_name     = "Grays"
        self.max_val      = (int(2 ** bit_depth) - 1) if bit_depth < 32 else 65535
        # Optional hook -- MainWindow sets this to invalidate the
        # merged-row panels when contrast/LUT changes.
        self.on_change_cb = None

        self.setBackground(HEADER_BG)
        self.setBorder(EmptyBorder(6, 8, 6, 8))
        self.setLayout(BorderLayout())

        inner = JPanel()
        inner.setBackground(HEADER_BG)
        inner.setLayout(BoxLayout(inner, BoxLayout.Y_AXIS))

        title = JLabel(channel_name)
        title.setFont(Font("SansSerif", Font.BOLD, 13))
        title.setForeground(TEXT_CLR)
        title.setAlignmentX(Component.LEFT_ALIGNMENT)
        inner.add(title)
        inner.add(Box.createVerticalStrut(4))

        self.min_field, self.min_slider = self._make_field_slider(
            "Min", "0", inner, initial=0)
        self.max_field, self.max_slider = self._make_field_slider(
            "Max", str(self.max_val), inner, initial=self.max_val)

        lut_lbl = JLabel("LUT (export)")
        lut_lbl.setFont(Font("SansSerif", Font.PLAIN, 11))
        lut_lbl.setForeground(TEXT_CLR)
        lut_lbl.setAlignmentX(Component.LEFT_ALIGNMENT)
        inner.add(lut_lbl)

        names = [n for n, _ in LUT_PRESETS]
        self.lut_combo = JComboBox(names)
        self.lut_combo.setRenderer(LutSwatchRenderer())
        self.lut_combo.setMaximumSize(Dimension(226, 28))   # = field + slider row
        self.lut_combo.setAlignmentX(Component.LEFT_ALIGNMENT)
        inner.add(self.lut_combo)
        inner.add(Box.createVerticalStrut(4))

        btn_row = JPanel(GridLayout(1, 2, 6, 0))     # same width as the rows above
        btn_row.setBackground(HEADER_BG)
        btn_row.setAlignmentX(Component.LEFT_ALIGNMENT)
        btn_row.setMaximumSize(Dimension(226, 24))

        apply_btn = JButton("Apply")
        auto_btn  = JButton("Auto")
        for b in [apply_btn, auto_btn]:
            b.setFocusPainted(False)
        btn_row.add(apply_btn)
        btn_row.add(auto_btn)
        inner.add(btn_row)

        # ---- Saturation overlay toggles --------------------------------
        inner.add(Box.createVerticalStrut(3))
        sat_row = JPanel()
        sat_row.setLayout(BoxLayout(sat_row, BoxLayout.X_AXIS))
        sat_row.setBackground(HEADER_BG)
        sat_row.setAlignmentX(Component.LEFT_ALIGNMENT)

        sat_lbl = JLabel("Saturation:")
        sat_lbl.setFont(Font("SansSerif", Font.PLAIN, 11))
        sat_lbl.setForeground(TEXT_CLR)
        sat_row.add(sat_lbl)
        sat_row.add(Box.createHorizontalStrut(6))

        # Color swatches drawn as small painted labels next to the checkboxes
        SAT_UNDER_CLR = Color(40, 80, 220)    # blue
        SAT_OVER_CLR  = Color(200, 30, 30)    # red

        def _make_swatch(c):
            sw = JLabel("  ")
            sw.setOpaque(True)
            sw.setBackground(c)
            sw.setPreferredSize(Dimension(12, 12))
            sw.setMaximumSize(Dimension(12, 12))
            sw.setMinimumSize(Dimension(12, 12))
            sw.setBorder(BorderFactory.createLineBorder(BORDER_CLR, 1))
            return sw

        self.sat_under_cb = JCheckBox("Under", True)
        self.sat_over_cb  = JCheckBox("Over",  True)
        for cb in [self.sat_under_cb, self.sat_over_cb]:
            cb.setBackground(HEADER_BG)
            cb.setForeground(TEXT_CLR)
            cb.setFocusPainted(False)
            cb.setFont(Font("SansSerif", Font.PLAIN, 11))

        sat_row.add(_make_swatch(SAT_UNDER_CLR))
        sat_row.add(Box.createHorizontalStrut(3))
        sat_row.add(self.sat_under_cb)
        sat_row.add(Box.createHorizontalStrut(8))
        sat_row.add(_make_swatch(SAT_OVER_CLR))
        sat_row.add(Box.createHorizontalStrut(3))
        sat_row.add(self.sat_over_cb)
        sat_row.add(Box.createHorizontalGlue())

        inner.add(sat_row)
        # ----------------------------------------------------------------

        self.add(inner, BorderLayout.NORTH)

        p = self
        # ActionListener, not mouseClicked: a click with the slightest pointer
        # movement (trackpads, macOS) never fires mouseClicked
        class ApplyML(ActionListener):
            def actionPerformed(self, e):
                p._apply()
        class AutoML(ActionListener):
            def actionPerformed(self, e):
                p._auto()
        class LutAction(ActionListener):
            def actionPerformed(self, e):
                p.lut_name = str(p.lut_combo.getSelectedItem())
                if p.on_change_cb is not None:
                    try: p.on_change_cb()
                    except Exception as ex:
                        IJ.log("[WARN] on_change_cb: " + unicode(ex))
        apply_btn.addActionListener(ApplyML())
        auto_btn.addActionListener(AutoML())
        self.lut_combo.addActionListener(LutAction())

        class SatUnderAction(ActionListener):
            def actionPerformed(self, e):
                on = p.sat_under_cb.isSelected()
                for ip in p.all_image_panels:
                    try: ip.set_show_sat_under(on)
                    except Exception as ex:
                        IJ.log("[WARN] set_show_sat_under: " + unicode(ex))
        class SatOverAction(ActionListener):
            def actionPerformed(self, e):
                on = p.sat_over_cb.isSelected()
                for ip in p.all_image_panels:
                    try: ip.set_show_sat_over(on)
                    except Exception as ex:
                        IJ.log("[WARN] set_show_sat_over: " + unicode(ex))
        self.sat_under_cb.addActionListener(SatUnderAction())
        self.sat_over_cb.addActionListener(SatOverAction())

        # Push initial saturation state to all currently-known image panels
        _u = self.sat_under_cb.isSelected()
        _o = self.sat_over_cb.isSelected()
        for _ip in self.all_image_panels:
            try:
                _ip.set_show_sat_under(_u)
                _ip.set_show_sat_over(_o)
            except Exception:
                pass

    def _make_field_slider(self, label, default_val, parent, initial):
        lbl = JLabel(label)
        lbl.setFont(Font("SansSerif", Font.PLAIN, 11))
        lbl.setForeground(TEXT_CLR)
        lbl.setAlignmentX(Component.LEFT_ALIGNMENT)
        parent.add(lbl)

        row = JPanel()
        row.setLayout(BoxLayout(row, BoxLayout.X_AXIS))
        row.setBackground(HEADER_BG)
        row.setAlignmentX(Component.LEFT_ALIGNMENT)

        field = JTextField(default_val)
        field.setMaximumSize(Dimension(70, 24))
        field.setPreferredSize(Dimension(70, 24))
        row.add(field)
        row.add(Box.createHorizontalStrut(6))

        slider = JSlider(0, self.max_val, initial)
        slider.setMaximumSize(Dimension(150, 24))
        slider.setPreferredSize(Dimension(150, 24))
        slider.setBackground(HEADER_BG)
        row.add(slider)

        parent.add(row)
        parent.add(Box.createVerticalStrut(3))

        panel = self
        class SL(ChangeListener):
            def stateChanged(self, e):
                v = slider.getValue()
                field.setText(str(v))
                if not slider.getValueIsAdjusting():
                    panel._apply()
        slider.addChangeListener(SL())

        def commit_field():
            try:
                v = int(float(field.getText().strip()))
                v = max(0, min(panel.max_val, v))
                slider.setValue(v)   # also triggers _apply via SL
            except Exception:
                pass

        class FA(ActionListener):
            def actionPerformed(self, e):
                commit_field()
                panel._apply()
        field.addActionListener(FA())

        # Commit on focus loss too (tab / clicking elsewhere)
        from java.awt.event import FocusAdapter
        class FL(FocusAdapter):
            def focusLost(self, e):
                commit_field()
                panel._apply()
        field.addFocusListener(FL())

        return field, slider

    def _parse_fields(self):
        try: cmin = float(self.min_field.getText().strip())
        except Exception: cmin = 0.0
        try: cmax = float(self.max_field.getText().strip())
        except Exception: cmax = float(self.max_val)
        if cmin >= cmax:
            cmax = cmin + 1
        return cmin, cmax

    def _apply(self):
        cmin, cmax = self._parse_fields()
        for p in self.all_image_panels:
            p.set_contrast(cmin, cmax)
        if self.on_change_cb is not None:
            try: self.on_change_cb()
            except Exception as ex: IJ.log("[WARN] on_change_cb: " + unicode(ex))

    def _auto(self):
        """Auto-contrast ('saturated 0.35%') from the smallest pyramid level
        of every image of this channel. The histograms are computed on a
        worker thread with its own readers; the result is applied on the
        EDT."""
        paths = []
        for panel in self.all_image_panels:
            p = panel.level_paths[panel.n_levels - 1]
            if os.path.exists(p):
                paths.append(p)
        if not paths:
            return
        cp = self

        class Work(Runnable):
            def run(_s):
                try:
                    rng = cp._auto_range(paths)
                except Exception as e:
                    IJ.log("[WARN] auto-contrast: " + unicode(e))
                    return
                if rng is not None:
                    SwingUtilities.invokeLater(_Do(lambda: cp._set_range(*rng)))
        t = JThread(Work(), "MISP-auto-contrast")
        t.setDaemon(True)
        t.start()

    def _auto_range(self, paths):
        SATURATED = 0.0035   # 0.35% each side
        N_BINS = 1024
        max_val = getattr(self, 'max_val', 65535)
        bins = [0] * N_BINS
        total = 0
        scale = float(N_BINS - 1) / float(max_val)
        for path in paths:
            reader = ImageReader()
            try:
                reader.setId(path)
                w, h = reader.getSizeX(), reader.getSizeY()
                proc = bytes_to_processor(reader.openBytes(0, 0, 0, w, h),
                                          reader.getPixelType(), w, h,
                                          reader.isLittleEndian())
            except Exception as e:
                IJ.log("[WARN] auto-contrast read failed: " + unicode(e))
                continue
            finally:
                reader.close()
            if isinstance(proc, FloatProcessor):
                proc = proc.convertToShortProcessor(False)   # values clipped to 0..65535
            hist = proc.getHistogram()        # Java: 256 or 65536 counts
            for v in range(len(hist)):
                c = hist[v]
                if c:
                    bins[min(N_BINS - 1, int(v * scale))] += c
            total += w * h
        if total == 0:
            return None
        threshold = int(total * SATURATED)
        bin_width = float(max_val) / float(N_BINS - 1)
        acc, lo_bin = 0, 0
        for i in range(N_BINS):
            acc += bins[i]
            if acc > threshold:
                lo_bin = i
                break
        acc, hi_bin = 0, N_BINS - 1
        for i in range(N_BINS - 1, -1, -1):
            acc += bins[i]
            if acc > threshold:
                hi_bin = i
                break
        cmin = int(lo_bin * bin_width)
        cmax = int(hi_bin * bin_width)
        if cmax <= cmin:
            cmax = cmin + 1
        return cmin, cmax

    def _set_range(self, cmin, cmax):
        max_val = getattr(self, 'max_val', 65535)
        self.min_field.setText(str(cmin))
        self.max_field.setText(str(cmax))
        self.min_slider.setValue(max(0, min(max_val, cmin)))
        self.max_slider.setValue(max(0, min(max_val, cmax)))
        self._apply()

    def current_params(self):
        cmin, cmax = self._parse_fields()
        return {'min': cmin, 'max': cmax, 'lut': self.lut_name}


# ==============================================================
# Progress dialog
# ==============================================================

class ProgressDialog(JFrame):
    def __init__(self, title, total, cancellable=False):
        JFrame.__init__(self, title)
        self.setSize(500, 150 if cancellable else 120)
        self.setLocationRelativeTo(None)
        self.setDefaultCloseOperation(JFrame.DO_NOTHING_ON_CLOSE)
        c = self.getContentPane()
        c.setLayout(BoxLayout(c, BoxLayout.Y_AXIS))
        c.setBackground(PANEL_BG)
        c.add(Box.createVerticalStrut(10))
        self.label = JLabel("  Preparing...")
        self.label.setForeground(TEXT_CLR)
        c.add(self.label)
        c.add(Box.createVerticalStrut(8))
        self.bar = JProgressBar(0, total)
        self.bar.setStringPainted(True)
        c.add(self.bar)

        # Cancel flag (polled by worker thread)
        self.cancelled = False

        if cancellable:
            c.add(Box.createVerticalStrut(6))
            btn_row = JPanel()
            btn_row.setBackground(PANEL_BG)
            btn_row.setLayout(BoxLayout(btn_row, BoxLayout.X_AXIS))
            btn_row.add(Box.createHorizontalGlue())
            self.cancel_btn = JButton("Cancel")
            self.cancel_btn.setFocusPainted(False)
            dlg = self
            class CancelA(ActionListener):
                def actionPerformed(_self, e):
                    dlg.cancelled = True
                    dlg.cancel_btn.setEnabled(False)
                    dlg.label.setText("  Cancelling after current file...")
            self.cancel_btn.addActionListener(CancelA())
            btn_row.add(self.cancel_btn)
            btn_row.add(Box.createHorizontalStrut(12))
            c.add(btn_row)

        c.add(Box.createVerticalGlue())
        # time left from the real work (callers: eta.set_total / eta.add)
        from SectionPipelineProject import WorkEta
        self.eta = WorkEta()
        self._text = u"Preparing..."
        dlg = self
        class TickA(ActionListener):
            def actionPerformed(_self, e):
                dlg._show()
        self._timer = SwingTimer(1000, TickA())
        self._timer.start()

    def _show(self):
        if self.cancelled:
            return
        t = self.eta.text()
        self.label.setText(u"  " + self._text + (u"     " + t if t else u""))
        f = self.eta.fraction()
        if f is not None:                    # the bar follows the work, not the item count
            self.bar.setValue(int(round(f * self.bar.getMaximum())))

    def update(self, value, text):
        dlg = self
        class R(Runnable):
            def run(_self):
                if dlg.eta.fraction() is None:
                    dlg.bar.setValue(value)
                dlg._text = text
                dlg._show()
        SwingUtilities.invokeLater(R())

    def dispose(self):
        self._timer.stop()
        JFrame.dispose(self)

    def is_cancelled(self):
        return self.cancelled



def entry_levels_dims(entry):
    """Parse L0_w/L0_h, L1_w/L1_h, ... from entry dict."""
    try:
        n = int(entry.get('pyramid_levels', '1'))
    except Exception:
        n = 1
    dims = []
    for i in range(n):
        lw = int(entry.get(u'L{}_w'.format(i),
                            entry.get('width',  '1')))
        lh = int(entry.get(u'L{}_h'.format(i),
                            entry.get('height', '1')))
        dims.append((lw, lh))
    if not dims:
        dims = [(int(entry.get('width', '1')),
                  int(entry.get('height', '1')))]
    return dims


# ==============================================================
# MainWindow
# ==============================================================

class MainWindow(JFrame):

    def __init__(self, parse_result, folder_path, manifest):
        JFrame.__init__(self, "Multichannel Image Set Previewer")
        self.parse_result = parse_result
        self.folder_path  = folder_path
        self.manifest     = manifest
        self.channels     = parse_result['channels']
        self.sets         = parse_result['sets']
        self.set_ids      = sorted(self.sets.keys())
        self.n_channels   = len(self.channels)
        self.n_sets       = len(self.set_ids)
        self.cells_per_row= DEFAULT_CELLS_ROW

        self.reader_pool = TileReaderPool()
        self.tile_cache  = TileCache(TILE_CACHE_SIZE)

        self.ref_w = 1
        self.ref_h = 1
        bd_max     = 8
        for name, e in manifest.items():
            if name.startswith('__'):
                continue
            try:
                self.ref_w = max(self.ref_w, int(e.get('width',  '1')))
                self.ref_h = max(self.ref_h, int(e.get('height', '1')))
                bd_max     = max(bd_max, int(e.get('bit_depth', '8')))
            except Exception:
                pass
        self.bit_depth = bd_max
        self._project_init()      # project.json, slide orientation, ref

        self.zoom_idx         = ZOOM_DEFAULT_IDX
        self.image_panels_all = []
        self.image_panels     = {}
        self.contrast_panels  = {}
        self._ram_timer       = None
        self.sync_enabled     = True
        self.panels_by_set    = {}   # set_id -> list of ImagePanel
        self.set_checked      = {sid: False for sid in self.set_ids}
        self.show_checked_only= False
        self.header_cells     = {}   # set_id -> header cell JPanel
        self.channel_rows     = {}   # ch -> row JPanel
        self.panel_cells_by_ch= {}   # ch -> {set_id -> cell component}
        self.merged_panels_by_set = {}   # set_id -> MergedImagePanel
        self.n_visible_sets   = self.n_sets   # current # of visible columns

        self._build_ui()
        self._project_after_ui()

        self.setSize(WINDOW_W, WINDOW_H)
        self.setLocationRelativeTo(None)
        self.setDefaultCloseOperation(JFrame.DO_NOTHING_ON_CLOSE)

        win = self
        class WC(WindowAdapter):
            def windowClosing(self, e):
                win._close_window()
        self.addWindowListener(WC())

        # RAM updater: runs every 1000 ms on the EDT
        class RamA(ActionListener):
            def actionPerformed(_self, e):
                win._update_ram()
        self._ram_timer = SwingTimer(1000, RamA())
        self._ram_timer.start()
        self._update_ram()   # initial

        self.setVisible(True)

    def _close_window(self):
        """Close with the unsaved-changes check; True if closed."""
        if not self._confirm_close():
            return False
        self.reader_pool.close_all()
        get_async(self.tile_cache).close()
        _ASYNC.pop(id(self.tile_cache), None)
        if self._ram_timer is not None:
            self._ram_timer.stop()
        self.dispose()
        return True

    # ----------------------------------------------------------

    def _build_ui(self):
        c = self.getContentPane()
        c.setBackground(PANEL_BG)
        c.setLayout(BorderLayout(0, 0))
        c.add(self._build_north(),      BorderLayout.NORTH)
        c.add(self._build_center(),     BorderLayout.CENTER)
        # export options open in their own window (Files group, top right)
        self.export_bar = self._build_export_bar()

    def _build_top_bar(self):
        """(left groups, right groups); _build_north lays them out so the
        left ones wrap onto more rows in a narrow window."""
        left, right = [], []
        win = self

        info = dark_label(u"{}  |  {} ch  |  {} sets  |  {}bit  |  ref {}x{}".format(
            os.path.basename(self.folder_path), self.n_channels, self.n_sets,
            self.bit_depth, self.ref_w, self.ref_h))
        info.setBorder(EmptyBorder(0, 0, 0, 8))
        left.append(info)

        self.cpr_field = JTextField(str(self.cells_per_row), 3)
        def apply_cpr():
            try:
                v = int(win.cpr_field.getText().strip())
                if v > 0:
                    win.cells_per_row = v
                    win._update_cell_sizes()
            except Exception:
                pass
        self.cpr_field.addActionListener(_Action(apply_cpr))
        self.zoom_label = dark_label(u" {}x ".format(
            self._fmt_zoom(ZOOM_STEPS[self.zoom_idx])))
        self.zoom_label.setFont(Font("SansSerif", Font.BOLD, 12))
        self.zoom_label.setPreferredSize(Dimension(44, 24))
        self.zoom_label.setHorizontalAlignment(JLabel.CENTER)
        left.append(dark_group("Layout", [
            dark_label("Cells/row"), self.cpr_field,
            dark_button("Apply", apply_cpr),
            dark_label("   Zoom"),
            dark_button("-", lambda: win._bulk_zoom_step(-1)),
            self.zoom_label,
            dark_button("+", lambda: win._bulk_zoom_step(1))]))

        nav = []
        for (text, dx_fn, tip) in [
            ("|<", lambda: -win.n_visible_sets, "First set"),
            ("<<", lambda: -win.cells_per_row,  "One page left"),
            ("<",  lambda: -1,                  "One set left"),
            (">",  lambda: 1,                   "One set right"),
            (">>", lambda: win.cells_per_row,   "One page right"),
            (">|", lambda: win.n_visible_sets,  "Last set"),
        ]:
            nav.append(dark_button(text, lambda f=dx_fn: win._nav_sets(f()), tip))
        left.append(dark_group("Sets", nav))

        # Sync: interacting with one panel updates all panels of its set
        self.sync_cb_box = dark_check("Sync set", True)
        def sync():
            win.sync_enabled = win.sync_cb_box.isSelected()
        self.sync_cb_box.addActionListener(_Action(sync))
        self.show_checked_cb = dark_check("Show checked only", False)
        def show_checked():
            win.show_checked_only = win.show_checked_cb.isSelected()
            win._apply_set_filter()
        self.show_checked_cb.addActionListener(_Action(show_checked))
        left.append(dark_group("Display", [
            self.sync_cb_box, self.show_checked_cb,
            dark_button("Auto contrast all", lambda: win._auto_contrast_all())]))

        self.ram_bar = JProgressBar(0, 100)
        self.ram_bar.setPreferredSize(Dimension(90, 14))
        self.ram_bar.setStringPainted(False)
        self.ram_label = dark_label("RAM: --")
        self.ram_label.setFont(Font("SansSerif", Font.PLAIN, 11))
        self.ram_label.setPreferredSize(Dimension(150, 18))
        def reload_folder():
            if not win._close_window():          # asks about unsaved changes
                return
            folder = win.folder_path
            class Again(Runnable):               # dialogs + pyramid work off the EDT
                def run(_s):
                    run_plugin(folder)
            JThread(Again(), "MISP-reload").start()
        right.append(dark_group("Memory / folder", [
            self.ram_bar, self.ram_label,
            dark_button("Reload folder", reload_folder)]))
        return left, right

    def _fmt_zoom(self, z):
        if z < 1.0:
            s = u"{:.2f}".format(z)
            return s.rstrip('0').rstrip('.')
        if z == int(z):
            return str(int(z))
        return u"{:.1f}".format(z)

    def _bulk_zoom_step(self, delta):
        new_idx = max(0, min(len(ZOOM_STEPS) - 1, self.zoom_idx + delta))
        if new_idx == self.zoom_idx:
            return
        self.zoom_idx = new_idx
        z = ZOOM_STEPS[new_idx]
        self.zoom_label.setText(u" {}x ".format(self._fmt_zoom(z)))
        for p in self.image_panels_all:
            p.set_zoom(z)
        for mp in self.merged_panels_by_set.values():
            mp.set_zoom(z)

    def _build_center(self):
        container = JPanel(BorderLayout(0, 0))
        container.setBackground(PANEL_BG)

        # Two parts: fixed-height set header row (NORTH) + image
        # rows that stretch (CENTER).  BorderLayout respects the
        # preferred height of the NORTH component.
        self.grid_panel = JPanel(BorderLayout(0, 0))
        self.grid_panel.setBackground(PANEL_BG)

        # Set header row: one cell per column, each with checkbox + label
        self.header_row = JPanel(GridLayout(1, 0, 0, 0))
        self.header_row.setBackground(HEADER_BG)
        self.header_row.setPreferredSize(Dimension(100, 22))

        for idx, set_id in enumerate(self.set_ids):
            cell = JPanel(BorderLayout())
            cell.setBackground(HEADER_BG)
            cell.setBorder(BorderFactory.createMatteBorder(
                0, 1, 1, 0, BORDER_CLR))

            cb = JCheckBox()
            cb.setBackground(HEADER_BG)
            cb.setFocusPainted(False)
            cb.setBorder(EmptyBorder(0, 4, 0, 0))
            cb.setOpaque(True)

            # Pin set-id and cb into closure via factory function
            def make_toggle(sid_p, cb_p, win_p):
                class HeaderToggle(ActionListener):
                    def actionPerformed(_self, e):
                        win_p.set_checked[sid_p] = cb_p.isSelected()
                        if win_p.show_checked_only:
                            win_p._apply_set_filter()
                return HeaderToggle()
            cb.addActionListener(make_toggle(set_id, cb, self))
            cell.add(cb, BorderLayout.WEST)
            self.header_checkboxes[set_id] = cb

            lbl = JLabel(u"Set {}: {}".format(idx + 1, set_id))
            lbl.setForeground(TEXT_CLR)
            lbl.setFont(Font("SansSerif", Font.BOLD, 11))
            lbl.setHorizontalAlignment(JLabel.CENTER)
            cell.add(lbl, BorderLayout.CENTER)
            self.header_labels[set_id] = lbl
            self._attach_header_menu(set_id, lbl)

            self.header_cells[set_id] = cell
            self.header_row.add(cell)
        self.grid_panel.add(self.header_row, BorderLayout.NORTH)

        # Image rows: merged row first, then channel rows
        self.rows_panel = JPanel(GridLayout(self.n_channels + 1, 1, 0, 0))
        self.rows_panel.setBackground(PANEL_BG)
        self.grid_panel.add(self.rows_panel, BorderLayout.CENTER)

        # ----- Build merged row (at top) ---------------------
        self.merged_row = JPanel(GridLayout(1, 0, 0, 0))
        self.merged_row.setBackground(PANEL_BG)
        self.merged_panels_by_set = {}   # set_id -> MergedImagePanel

        win_ref = self
        def make_cb(w):
            def cb(src, cx, cy, zoom):
                w._on_panel_view_change(src, cx, cy, zoom)
            return cb

        for set_id in self.set_ids:
            mp = MergedImagePanel(set_id, self.ref_w, self.ref_h,
                                   self.reader_pool, self.tile_cache)
            mp.sync_cb = make_cb(win_ref)
            mp.putClientProperty("mp_set_id", set_id)
            self.merged_panels_by_set[set_id] = mp
            self.merged_row.add(mp)
        self.rows_panel.add(self.merged_row)

        # ----- Build channel rows ----------------------------
        for ch in self.channels:
            row = JPanel(GridLayout(1, 0, 0, 0))
            row.setBackground(PANEL_BG)
            self.image_panels[ch] = []
            self.channel_rows[ch] = row
            self.panel_cells_by_ch[ch] = {}   # set_id -> cell component

            for set_id in self.set_ids:
                ch_map = self.sets[set_id]
                if ch in ch_map:
                    name  = source_key(self.folder_path, ch_map[ch])
                    entry = self.manifest.get(name, {})
                    try:
                        bd   = int(entry.get('bit_depth', '8'))
                        fmin = float(entry.get('full_min', '0'))
                        fmax = float(entry.get('full_max', '255'))
                        pt   = int(entry.get('pixel_type',
                                             str(FormatTools.UINT8)))
                        le   = entry.get('little_endian', '1') == '1'
                        ldims = entry_levels_dims(entry)
                        n_lv = len(ldims)
                        lpaths = [workspace_level_path(
                                    self.folder_path, name, i)
                                  for i in range(n_lv)]
                    except Exception:
                        bd = 8
                        fmin, fmax = 0.0, 255.0
                        pt = FormatTools.UINT8
                        le = True
                        ldims = [(1, 1)]
                        lpaths = [""]
                    panel = ImagePanel(lpaths, ldims, bd, fmin, fmax,
                                       pt, le,
                                       self.ref_w, self.ref_h,
                                       self.reader_pool, self.tile_cache,
                                       set_id=set_id)
                    panel.sync_cb = make_cb(win_ref)

                    self.image_panels[ch].append(panel)
                    self.image_panels_all.append(panel)
                    self.panels_by_set.setdefault(set_id, []).append(panel)

                    # Register this channel's data with the merged
                    # panel for this set.  Use closures to keep the
                    # contrast/LUT lookups live.
                    def make_contrast_fn(cname):
                        def fn():
                            cp = self.contrast_panels.get(cname)
                            if cp is None:
                                return (0, 255)
                            return cp._parse_fields()
                        return fn
                    def make_lut_fn(cname):
                        def fn():
                            cp = self.contrast_panels.get(cname)
                            if cp is None:
                                return Color.WHITE
                            return LUT_BY_NAME.get(cp.lut_name, Color.WHITE)
                        return fn

                    mp = self.merged_panels_by_set[set_id]
                    mp.add_source(ch, lpaths, ldims, pt, le,
                                  make_contrast_fn(ch),
                                  make_lut_fn(ch))
                else:
                    panel = MissingPanel()
                panel.putClientProperty("mp_set_id", set_id)
                self.panel_cells_by_ch[ch][set_id] = panel
                row.add(panel)

            self.rows_panel.add(row)

        grid_wrap = JPanel(BorderLayout())
        grid_wrap.setBackground(PANEL_BG)
        grid_wrap.add(self.grid_panel, BorderLayout.CENTER)

        self.scroll_pane = JScrollPane(grid_wrap)
        self.scroll_pane.setBackground(PANEL_BG)
        self.scroll_pane.getViewport().setBackground(PANEL_BG)
        self.scroll_pane.setHorizontalScrollBarPolicy(
            ScrollPaneConstants.HORIZONTAL_SCROLLBAR_AS_NEEDED)
        self.scroll_pane.setVerticalScrollBarPolicy(
            ScrollPaneConstants.VERTICAL_SCROLLBAR_NEVER)
        self.scroll_pane.getHorizontalScrollBar().setUnitIncrement(32)

        # Contrast column uses a small spacer at the top to align
        # with the set-header row, then n_channels cells to match
        # the image rows.
        self.contrast_column = JPanel(BorderLayout())
        self.contrast_column.setBackground(PANEL_BG)

        # Top spacer matches header row height (updated in _update_cell_sizes)
        self.contrast_header_spacer = JPanel()
        self.contrast_header_spacer.setBackground(HEADER_BG)
        self.contrast_header_spacer.setPreferredSize(
            Dimension(CONTRAST_PANEL_W, 0))
        self.contrast_column.add(self.contrast_header_spacer,
                                 BorderLayout.NORTH)

        contrast_inner = JPanel(GridLayout(self.n_channels + 1, 1, 0, 0))
        contrast_inner.setBackground(PANEL_BG)
        self.contrast_column.add(contrast_inner, BorderLayout.CENTER)

        # Merged placeholder at top (matches the merged image row)
        merged_cp = MergedContrastPanel()
        contrast_inner.add(merged_cp)

        # Hook: when ANY channel's contrast/LUT changes, refresh all
        # merged panels.
        win_ref = self
        def refresh_merged():
            for mp in win_ref.merged_panels_by_set.values():
                mp.invalidate_render()

        for ch in self.channels:
            cp = ContrastChannelPanel(
                ch, self.bit_depth, self.image_panels[ch])
            cp.on_change_cb = refresh_merged
            self.contrast_panels[ch] = cp
            contrast_inner.add(cp)

        container.add(self.scroll_pane,     BorderLayout.CENTER)
        container.add(self.contrast_column, BorderLayout.EAST)

        win = self
        class CL(ComponentAdapter):
            def componentResized(self, e):
                win._update_cell_sizes()
        self.scroll_pane.addComponentListener(CL())
        self.addComponentListener(CL())

        return container

    def _build_export_bar(self):
        """Bottom bar: sectioned output options.

        - Single Channel section: enable + 2x2 (Grayscale/Colored x
          Original/8-bit) checkboxes
        - Merged section: enable + radio (8-bit / Original depth)
        - Which sets: Checked only / All
        - Formats: TIF / JPG / PNG (any combination)
        - Output path + Browse + Merge & Export
        """
        bar = JPanel()
        bar.setBackground(Color(50, 50, 50))
        bar.setBorder(EmptyBorder(8, 12, 8, 12))
        bar.setLayout(BoxLayout(bar, BoxLayout.Y_AXIS))
        lf = Font("SansSerif", Font.PLAIN, 12)
        bf = Font("SansSerif", Font.BOLD, 12)

        # ============================================================
        # Top row: Single Channel + Merged + Which sets
        # ============================================================
        top = JPanel()
        top.setBackground(Color(50, 50, 50))
        top.setLayout(BoxLayout(top, BoxLayout.X_AXIS))
        top.setAlignmentX(Component.LEFT_ALIGNMENT)

        # ---- Single Channel section -------------------------------
        self.sc_enable_cb = JCheckBox("Single Channel", False)
        self.sc_enable_cb.setFont(bf)
        self.sc_enable_cb.setForeground(TEXT_CLR)
        self.sc_enable_cb.setBackground(Color(50, 50, 50))

        sc_box = JPanel()
        sc_box.setBackground(Color(50, 50, 50))
        sc_box.setBorder(BorderFactory.createTitledBorder(
            BorderFactory.createLineBorder(BORDER_CLR, 1),
            " "))   # space reserved, actual header is put as NORTH

        sc_wrap = JPanel(BorderLayout(4, 0))
        sc_wrap.setBackground(Color(50, 50, 50))
        sc_wrap.setBorder(BorderFactory.createLineBorder(BORDER_CLR, 1))

        sc_header = JPanel()
        sc_header.setBackground(Color(60, 60, 60))
        sc_header.setLayout(BoxLayout(sc_header, BoxLayout.X_AXIS))
        sc_header.setBorder(EmptyBorder(2, 6, 2, 6))
        sc_header.add(self.sc_enable_cb)
        sc_wrap.add(sc_header, BorderLayout.NORTH)

        # 2x2 grid of checkboxes
        sc_grid_panel = JPanel(GridLayout(3, 3, 4, 2))
        sc_grid_panel.setBackground(Color(50, 50, 50))
        sc_grid_panel.setBorder(EmptyBorder(4, 6, 4, 6))

        # Corner
        corner = JLabel("")
        sc_grid_panel.add(corner)
        # Col headers
        hd1 = JLabel("Grayscale")
        hd2 = JLabel("Colored")
        for h in [hd1, hd2]:
            h.setForeground(TEXT_CLR); h.setFont(lf)
            h.setHorizontalAlignment(JLabel.CENTER)
            sc_grid_panel.add(h)
        # Row: Original
        rl1 = JLabel("Original")
        rl1.setForeground(TEXT_CLR); rl1.setFont(lf)
        sc_grid_panel.add(rl1)
        self.sc_orig_gray  = JCheckBox("", False)
        self.sc_orig_color = JCheckBox("", False)
        sc_grid_panel.add(self.sc_orig_gray)
        sc_grid_panel.add(self.sc_orig_color)
        # Row: 8-bit
        rl2 = JLabel("8-bit")
        rl2.setForeground(TEXT_CLR); rl2.setFont(lf)
        sc_grid_panel.add(rl2)
        self.sc_8bit_gray  = JCheckBox("", False)
        self.sc_8bit_color = JCheckBox("", True)   # default
        sc_grid_panel.add(self.sc_8bit_gray)
        sc_grid_panel.add(self.sc_8bit_color)

        for cb in [self.sc_orig_gray, self.sc_orig_color,
                   self.sc_8bit_gray, self.sc_8bit_color]:
            cb.setBackground(Color(50, 50, 50))
            cb.setHorizontalAlignment(JCheckBox.CENTER)
            cb.setFocusPainted(False)

        sc_wrap.add(sc_grid_panel, BorderLayout.CENTER)
        top.add(sc_wrap)
        top.add(Box.createHorizontalStrut(8))

        # ---- Merged section ---------------------------------------
        self.mg_enable_cb = JCheckBox("Merged", True)
        self.mg_enable_cb.setFont(bf)
        self.mg_enable_cb.setForeground(TEXT_CLR)
        self.mg_enable_cb.setBackground(Color(50, 50, 50))

        mg_wrap = JPanel(BorderLayout(4, 0))
        mg_wrap.setBackground(Color(50, 50, 50))
        mg_wrap.setBorder(BorderFactory.createLineBorder(BORDER_CLR, 1))

        mg_header = JPanel()
        mg_header.setBackground(Color(60, 60, 60))
        mg_header.setLayout(BoxLayout(mg_header, BoxLayout.X_AXIS))
        mg_header.setBorder(EmptyBorder(2, 6, 2, 6))
        mg_header.add(self.mg_enable_cb)
        mg_wrap.add(mg_header, BorderLayout.NORTH)

        from javax.swing import JRadioButton, ButtonGroup
        mg_body = JPanel()
        mg_body.setBackground(Color(50, 50, 50))
        mg_body.setLayout(BoxLayout(mg_body, BoxLayout.Y_AXIS))
        mg_body.setBorder(EmptyBorder(4, 6, 4, 6))

        self.mg_cb_8bit = JCheckBox("8-bit", True)
        self.mg_cb_orig = JCheckBox("Original depth", False)
        for rb in [self.mg_cb_8bit, self.mg_cb_orig]:
            rb.setForeground(TEXT_CLR)
            rb.setBackground(Color(50, 50, 50))
            rb.setFont(lf)
            rb.setFocusPainted(False)
            mg_body.add(rb)
        mg_body.add(Box.createVerticalStrut(2))
        mg_note = JLabel("(color, additive)")
        mg_note.setForeground(Color(160, 160, 160))
        mg_note.setFont(Font("SansSerif", Font.ITALIC, 10))
        mg_body.add(mg_note)

        mg_wrap.add(mg_body, BorderLayout.CENTER)
        top.add(mg_wrap)
        top.add(Box.createHorizontalStrut(8))

        # ---- Which sets section -----------------------------------
        ws_wrap = JPanel(BorderLayout(4, 0))
        ws_wrap.setBackground(Color(50, 50, 50))
        ws_wrap.setBorder(BorderFactory.createLineBorder(BORDER_CLR, 1))
        ws_header = JPanel()
        ws_header.setBackground(Color(60, 60, 60))
        ws_header.setLayout(BoxLayout(ws_header, BoxLayout.X_AXIS))
        ws_header.setBorder(EmptyBorder(2, 6, 2, 6))
        ws_lbl = JLabel("Which sets")
        ws_lbl.setFont(bf); ws_lbl.setForeground(TEXT_CLR)
        ws_header.add(ws_lbl)
        ws_wrap.add(ws_header, BorderLayout.NORTH)

        ws_body = JPanel()
        ws_body.setBackground(Color(50, 50, 50))
        ws_body.setLayout(BoxLayout(ws_body, BoxLayout.Y_AXIS))
        ws_body.setBorder(EmptyBorder(4, 6, 4, 6))
        self.ws_rb_checked = JRadioButton("Checked only", True)
        self.ws_rb_all     = JRadioButton("All", False)
        for rb in [self.ws_rb_checked, self.ws_rb_all]:
            rb.setForeground(TEXT_CLR)
            rb.setBackground(Color(50, 50, 50))
            rb.setFont(lf)
            rb.setFocusPainted(False)
            ws_body.add(rb)
        ws_group = ButtonGroup()
        ws_group.add(self.ws_rb_checked)
        ws_group.add(self.ws_rb_all)
        ws_wrap.add(ws_body, BorderLayout.CENTER)
        top.add(ws_wrap)
        top.add(Box.createHorizontalStrut(8))

        # ---- Format section ---------------------------------------
        fmt_wrap = JPanel(BorderLayout(4, 0))
        fmt_wrap.setBackground(Color(50, 50, 50))
        fmt_wrap.setBorder(BorderFactory.createLineBorder(BORDER_CLR, 1))
        fmt_header = JPanel()
        fmt_header.setBackground(Color(60, 60, 60))
        fmt_header.setLayout(BoxLayout(fmt_header, BoxLayout.X_AXIS))
        fmt_header.setBorder(EmptyBorder(2, 6, 2, 6))
        fmt_lbl = JLabel("Format")
        fmt_lbl.setFont(bf); fmt_lbl.setForeground(TEXT_CLR)
        fmt_header.add(fmt_lbl)
        fmt_wrap.add(fmt_header, BorderLayout.NORTH)

        fmt_body = JPanel()
        fmt_body.setBackground(Color(50, 50, 50))
        fmt_body.setLayout(BoxLayout(fmt_body, BoxLayout.Y_AXIS))
        fmt_body.setBorder(EmptyBorder(4, 6, 4, 6))
        self.fmt_tif = JCheckBox("TIF",  True)
        self.fmt_jpg = JCheckBox("JPG", False)
        self.fmt_png = JCheckBox("PNG", False)
        for cb in [self.fmt_tif, self.fmt_jpg, self.fmt_png]:
            cb.setForeground(TEXT_CLR)
            cb.setBackground(Color(50, 50, 50))
            cb.setFont(lf)
            cb.setFocusPainted(False)
            fmt_body.add(cb)

        fmt_body.add(Box.createVerticalStrut(2))
        fmt_note = JLabel("(JPG: 8-bit only;")
        fmt_note.setForeground(Color(180, 180, 130))
        fmt_note.setFont(Font("SansSerif", Font.ITALIC, 10))
        fmt_body.add(fmt_note)
        fmt_note2 = JLabel(" orig+color: TIF only)")
        fmt_note2.setForeground(Color(180, 180, 130))
        fmt_note2.setFont(Font("SansSerif", Font.ITALIC, 10))
        fmt_body.add(fmt_note2)

        fmt_wrap.add(fmt_body, BorderLayout.CENTER)
        top.add(fmt_wrap)

        top.add(Box.createHorizontalGlue())
        bar.add(top)

        bar.add(Box.createVerticalStrut(6))

        # ============================================================
        # Bottom row: Output path + Browse + Merge
        # ============================================================
        btm = JPanel()
        btm.setBackground(Color(50, 50, 50))
        btm.setLayout(BoxLayout(btm, BoxLayout.X_AXIS))
        btm.setAlignmentX(Component.LEFT_ALIGNMENT)

        path_lbl = JLabel("Output: ")
        path_lbl.setForeground(TEXT_CLR); path_lbl.setFont(lf)
        btm.add(path_lbl)

        self.path_field = JTextField(
            os.path.join(self.folder_path, "merged_output"), 25)
        self.path_field.setMaximumSize(Dimension(99999, 26))
        self.path_field.setPreferredSize(Dimension(200, 26))
        btm.add(self.path_field)

        browse_btn = JButton("Browse")
        browse_btn.setFocusPainted(False)
        pf  = self.path_field
        win = self
        class BrowseML(ActionListener):
            def actionPerformed(self, e):
                chooser = JFileChooser()
                chooser.setFileSelectionMode(
                    JFileChooser.DIRECTORIES_ONLY)
                if chooser.showOpenDialog(win) == \
                        JFileChooser.APPROVE_OPTION:
                    pf.setText(chooser.getSelectedFile().getAbsolutePath())
        browse_btn.addActionListener(BrowseML())
        btm.add(browse_btn)

        btm.add(Box.createHorizontalStrut(8))
        self.flat_cb = JCheckBox("Ignore subfolders", False)
        self.flat_cb.setToolTipText("<html>Off: each type folder keeps the image's subfolder "
                                    "below the loaded folder.<br>On: all files of a type in one "
                                    "folder (a repeated name gets its subfolder in front).</html>")
        self.flat_cb.setForeground(TEXT_CLR)
        self.flat_cb.setBackground(Color(50, 50, 50))
        self.flat_cb.setFont(lf)
        self.flat_cb.setFocusPainted(False)
        btm.add(self.flat_cb)

        btm.add(Box.createHorizontalStrut(12))

        merge_btn = JButton("Export")
        merge_btn.setFocusPainted(False)
        class MergeML(ActionListener):
            def actionPerformed(self, e):
                win._on_merge()
        merge_btn.addActionListener(MergeML())
        btm.add(merge_btn)
        btm.add(Box.createHorizontalStrut(4))
        close_btn = JButton("Close")
        close_btn.setFocusPainted(False)
        class CloseML(ActionListener):
            def actionPerformed(self, e):
                if getattr(win, "export_dialog", None) is not None:
                    win.export_dialog.setVisible(False)
        close_btn.addActionListener(CloseML())
        btm.add(close_btn)

        bar.add(btm)

        # ============================================================
        # Enable/disable interactions
        # ============================================================
        def refresh_enable():
            sc_on = self.sc_enable_cb.isSelected()
            for cb in [self.sc_orig_gray, self.sc_orig_color,
                       self.sc_8bit_gray, self.sc_8bit_color]:
                cb.setEnabled(sc_on)
            mg_on = self.mg_enable_cb.isSelected()
            for rb in [self.mg_cb_8bit, self.mg_cb_orig]:
                rb.setEnabled(mg_on)
        class EnableA(ActionListener):
            def actionPerformed(_self, e):
                refresh_enable()
        for cb in [self.sc_enable_cb, self.mg_enable_cb]:
            cb.addActionListener(EnableA())
        refresh_enable()

        return bar

    # ----------------------------------------------------------

    def _update_cell_sizes(self):
        vp = self.scroll_pane.getViewport()
        vw = vp.getWidth()
        vh = vp.getHeight()
        if vw <= 0 or vh <= 0:
            return

        n_row_cells = max(self.n_visible_sets, 1)
        cpr         = max(1, self.cells_per_row)

        if n_row_cells <= cpr:
            cell_w = max(MIN_CELL_W, vw // n_row_cells)
            grid_w = vw
        else:
            cell_w = max(MIN_CELL_W, vw // cpr)
            grid_w = cell_w * n_row_cells

        HEADER_H = 22
        img_area_h = max(1, vh - HEADER_H)
        # +1 for merged row
        n_img_rows = self.n_channels + 1
        cell_h = max(MIN_CELL_H, img_area_h // max(1, n_img_rows))
        grid_h = HEADER_H + cell_h * n_img_rows

        self.grid_panel.setPreferredSize(Dimension(grid_w, grid_h))
        self.grid_panel.revalidate()

        # Header spacer matches header row height; contrast inner
        # fills the remaining grid area.
        self.contrast_header_spacer.setPreferredSize(
            Dimension(CONTRAST_PANEL_W, HEADER_H))
        self.contrast_column.setPreferredSize(
            Dimension(CONTRAST_PANEL_W, grid_h))
        self.contrast_column.revalidate()
    # ----------------------------------------------------------

    def _apply_set_filter(self):
        """Apply visibility based on current mode:
           show_checked_only=False -> all sets visible (original order)
           show_checked_only=True  -> only checked sets visible (original order)
        Horizontal scroll position is preserved.
        """
        sb = self.scroll_pane.getHorizontalScrollBar()
        saved_val = sb.getValue()

        if self.show_checked_only:
            visible = [sid for sid in self.set_ids
                       if self.set_checked.get(sid, False)]
        else:
            visible = list(self.set_ids)
        n_visible = max(1, len(visible))
        self.n_visible_sets = len(visible)

        # Header row: use persistent header_cells map
        self.header_row.removeAll()
        self.header_row.setLayout(GridLayout(1, n_visible, 0, 0))
        for sid in visible:
            cell = self.header_cells.get(sid)
            if cell is not None:
                self.header_row.add(cell)

        # Merged row: same visibility rules
        self.merged_row.removeAll()
        self.merged_row.setLayout(GridLayout(1, n_visible, 0, 0))
        for sid in visible:
            mp = self.merged_panels_by_set.get(sid)
            if mp is not None:
                self.merged_row.add(mp)

        # Channel rows: use persistent panel_cells_by_ch map
        # (built during _build_center, never mutated)
        for ch in self.channels:
            row = self.channel_rows[ch]
            cells_map = self.panel_cells_by_ch[ch]   # set_id -> component
            row.removeAll()
            row.setLayout(GridLayout(1, n_visible, 0, 0))
            for sid in visible:
                c = cells_map.get(sid)
                if c is not None:
                    row.add(c)

            # Update image_panels[ch] to reflect visible ImagePanels
            # (skips MissingPanels). Contrast Apply/Auto operate on
            # all_image_panels (not this filtered list) so hidden
            # panels also receive updates.
            new_list = []
            for sid in visible:
                c = cells_map.get(sid)
                if isinstance(c, ImagePanel):
                    new_list.append(c)
            self.image_panels[ch] = new_list

        # Update contrast panels' VISIBLE list (only for display /
        # future reference; Apply/Auto iterate all_image_panels).
        for ch in self.channels:
            cp = self.contrast_panels.get(ch)
            if cp is not None:
                cp.image_panels = self.image_panels[ch]

        # Force a redraw of every image panel
        for p in self.image_panels_all:
            p._render_cache = None
            p.repaint()

        self.header_row.revalidate(); self.header_row.repaint()
        for ch in self.channels:
            r = self.channel_rows[ch]
            r.revalidate(); r.repaint()
        self.grid_panel.revalidate(); self.grid_panel.repaint()
        self._update_cell_sizes()

        SwingUtilities.invokeLater(_SetScroll(sb, saved_val))

    def _nav_sets(self, dx_sets):
        """Scroll horizontally by dx_sets columns worth of cells.

        dx_sets > 0 -> scroll right (toward higher index)
        dx_sets < 0 -> scroll left
        Values with |dx_sets| >= n_visible_sets jump to the far end.
        """
        if dx_sets == 0 or self.n_visible_sets == 0:
            return

        sb   = self.scroll_pane.getHorizontalScrollBar()
        vp   = self.scroll_pane.getViewport()
        grid_w = self.grid_panel.getPreferredSize().width
        if grid_w <= 0:
            return
        cell_w = grid_w / float(self.n_visible_sets)

        # Jump-to-end shortcuts
        if dx_sets <= -self.n_visible_sets:
            sb.setValue(sb.getMinimum())
            return
        if dx_sets >=  self.n_visible_sets:
            sb.setValue(sb.getMaximum() - vp.getWidth())
            return

        cur = sb.getValue()
        new_val = int(round(cur + dx_sets * cell_w))
        # Clamp
        lo = sb.getMinimum()
        hi = max(lo, sb.getMaximum() - vp.getWidth())
        if new_val < lo: new_val = lo
        if new_val > hi: new_val = hi
        sb.setValue(new_val)

    def _on_panel_view_change(self, src, cx, cy, zoom):
        """Called when a panel's view changes (wheel/drag).
        If sync is enabled, propagate to other panels in the same set
        (single-channel panels AND the merged panel for that set).
        """
        if not self.sync_enabled:
            return
        sid = src.set_id
        if sid is None:
            return
        # Single-channel panels in this set
        group = list(self.panels_by_set.get(sid, []))
        # Merged panel for this set
        mp = self.merged_panels_by_set.get(sid)
        if mp is not None:
            group.append(mp)
        for p in group:
            if p is src:
                continue
            p.apply_view(cx, cy, zoom)

    def _auto_contrast_all(self):
        """Auto contrast on every channel; each computes its histogram on a
        worker thread and applies the result on the EDT."""
        for cp in list(self.contrast_panels.values()):
            try:
                cp._auto()
            except Exception as ex:
                IJ.log("[WARN] auto_contrast_all: " + unicode(ex))

    def _update_ram(self):
        """Refresh RAM usage bar + label (JVM heap)."""
        from java.lang import Runtime as JRuntime
        rt = JRuntime.getRuntime()
        max_m  = rt.maxMemory()
        tot_m  = rt.totalMemory()
        free_m = rt.freeMemory()
        used_m = tot_m - free_m

        pct = int(100.0 * used_m / max_m) if max_m > 0 else 0

        def fmt(bytes_):
            GB = 1024.0 * 1024.0 * 1024.0
            MB = 1024.0 * 1024.0
            if bytes_ >= GB:
                return u"{:.1f}G".format(bytes_ / GB)
            return u"{:.0f}M".format(bytes_ / MB)

        self.ram_bar.setValue(pct)
        self.ram_label.setText(u"RAM: {}%  {} / {}".format(
            pct, fmt(used_m), fmt(max_m)))
    # Export methods are attached from
    # MultichannelImageSetPreviewerExport.py (see bottom of this file).

# ==============================================================
# Entry point
# ==============================================================

def run_plugin(folder=None):
    if not PARSER_LOADED:
        IJ.error("FileNameParser.py not found.\n"
                 "Place it in the same folder as this script.")
        return

    if folder is None:          # not started from the Section Pipeline launcher
        chooser = JFileChooser()
        chooser.setFileSelectionMode(JFileChooser.DIRECTORIES_ONLY)
        chooser.setDialogTitle("Select image folder")
        if chooser.showOpenDialog(None) != JFileChooser.APPROVE_OPTION:
            IJ.log("Cancelled.")
            return
        folder = chooser.getSelectedFile().getAbsolutePath()
    IJ.log("Loading: " + folder)

    files = select_images_dialog(folder)
    if not files:
        IJ.log("Cancelled.")
        return
    result = parse_files(files, allow_no_channel=True)
    if result['status'] == 'AMBIGUOUS':
        cands   = result['ambiguous_candidates']
        options = []
        for c in cands:
            vals = ", ".join(str(v) for v in sorted(c['unique_vals']))
            options.append(u"Position {}: [{}]".format(c['pos'], vals))
        choice = JOptionPane.showInputDialog(
            None, "Multiple channel position candidates.\nSelect one:",
            "Select Channel Position",
            JOptionPane.QUESTION_MESSAGE,
            None, options, options[0])
        if choice is None:
            return
        sel_pos = cands[options.index(choice)]['pos']
        result  = parse_files(files, forced_channel_pos=sel_pos, allow_no_channel=True)

    if result['status'] != 'OK':
        IJ.error("Parse failed:\n" + "\n".join(result['errors']))
        return
    if result['errors']:
        IJ.log("[Warnings]\n" + "\n".join(result['errors']))
    # files holding several channels -> one channel each
    result = expand_components(result, folder, IJ.log)
    if result.get('no_channel_token') and not result.get('split_files'):
        IJ.log("[Info] No channel token in the file names: each file is "
               "shown as its own set with one channel ('img').")

    total = sum(len(ch_map) for ch_map in result['sets'].values())
    progress = ProgressDialog("Preparing workspace", total)
    progress.setVisible(True)

    holder = [None]
    class Bg(Runnable):
        def run(self):
            try:
                holder[0] = prepare_workspace(folder, result, progress, fill_l1=True)
            except Exception as e:
                IJ.log("[ERROR] prepare_workspace: " + unicode(e))
                import traceback
                IJ.log(traceback.format_exc())
            finally:
                class Closer(Runnable):
                    def run(_self):
                        # closing the progress hands the focus back to the
                        # window active before (the launcher) a moment later:
                        # bring the viewer forward after that
                        w = None
                        if holder[0] is not None:
                            w = MainWindow(result, folder, holder[0])
                        progress.dispose()
                        if w is not None:
                            for ms in (150, 600):
                                class Front(ActionListener):
                                    def actionPerformed(_s, e):
                                        if w.isDisplayable():
                                            w.toFront(); w.requestFocus()
                                t = SwingTimer(ms, Front())
                                t.setRepeats(False)
                                t.start()
                SwingUtilities.invokeLater(Closer())
    t = JThread(Bg())
    t.setDaemon(True)
    t.start()


# ==============================================================
# Load companion modules (Panels, Export) and attach methods.
#
# The code is split across several files because Jython's parser
# (BufferedReader with a fixed readAheadLimit) fails to parse
# single files beyond ~100KB.  execfile injects symbols into
# the current globals so everything shares the same namespace.
# ==============================================================
def _locate_module(filename):
    """Locate a companion module file.

    Strategy (in order):
      1. IJ.getDirectory("plugins") + filename                 (plugins root)
      2. os.path.dirname(__file__) + filename                  (script's own dir, if __file__ exists)
      3. IJ.getDirectory("imagej") + "plugins" + filename      (alt plugins root)
      4. Recursive walk of plugins/ looking for filename       (handles arbitrary sub-folders)

    Step 4 is the fallback that works when the plugin is invoked from a
    Fiji menu (where __file__ is NOT defined in the script namespace),
    and the companion files live in plugins/<sub>/<sub>/...
    """
    candidates = []
    plugins_dir = None
    try:
        plugins_dir = IJ.getDirectory("plugins")
        if plugins_dir:
            candidates.append(os.path.join(plugins_dir, filename))
    except Exception:
        pass
    try:
        if '__file__' in dir() or '__file__' in globals():
            f = globals().get('__file__', None)
            if f:
                here = os.path.dirname(os.path.abspath(f))
                candidates.append(os.path.join(here, filename))
    except Exception:
        pass
    try:
        imagej = IJ.getDirectory("imagej")
        if imagej:
            candidates.append(os.path.join(imagej, "plugins", filename))
    except Exception:
        pass
    # Direct hits first
    for c in candidates:
        if c and os.path.exists(c):
            return c, candidates
    # Recursive walk fallback
    try:
        if plugins_dir and os.path.isdir(plugins_dir):
            for root, dirs, files in os.walk(plugins_dir):
                # Skip hidden / heavy folders to keep search fast
                dirs[:] = [d for d in dirs if not d.startswith('.')
                           and d.lower() not in ('jars', 'java', 'lib',
                                                 'macros', 'luts', 'fonts',
                                                 'images', 'samples')]
                if filename in files:
                    found = os.path.join(root, filename)
                    candidates.append(found + "  (via recursive walk)")
                    return found, candidates
    except Exception:
        pass
    return None, candidates


def _load_companion_module(filename):
    path, tried = _locate_module(filename)
    if path is None:
        IJ.error("{} not found.\nPlace it next to MultichannelImageSetPreviewer_.py "
                 u"in the Fiji plugins folder.\n\nTried:\n{}".format(
                     filename, "\n".join(tried)))
        return False
    IJ.log("Loading " + filename + ": " + path)
    execfile(path, globals())
    return True


# Panels must load BEFORE MainWindow is defined -- but MainWindow is
# already defined above in this file (uses MergedImagePanel), so
# technically Panels just needs to be loaded before MainWindow is
# INSTANTIATED.  run_plugin() below triggers instantiation, so
# loading here (before run_plugin call) is correct.

if not _load_companion_module("MultichannelImageSetPreviewerPanels.py"):
    raise RuntimeError("Failed to load panels module.")

if not _load_companion_module("MultichannelImageSetPreviewerWorkspace.py"):
    raise RuntimeError("Failed to load workspace module.")

if not _load_companion_module("MultichannelImageSetPreviewerProject.py"):
    raise RuntimeError("Failed to load project module.")

if not _load_companion_module("MultichannelImageSetPreviewerExport.py"):
    raise RuntimeError("Failed to load export module.")

register_export_methods(MainWindow)
register_project_methods(MainWindow)


run_plugin(globals().get("LAUNCH_FOLDER"))
