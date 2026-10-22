# ============================================================
# MultichannelImageSetPreviewerPanels.py
#
# Large panel classes extracted from MultichannelImageSetPreviewer_.py to keep
# each file below Jython's parser buffer limit (~100KB).
#
# Loaded from MultichannelImageSetPreviewer_.py via execfile() so that symbols
# defined here (MergedImagePanel, MergedContrastPanel) are injected
# into the main globals namespace.
#
# Imports are re-stated here in case this file is ever loaded
# in isolation -- they are no-ops when loaded via execfile into
# globals that already have them.
# ============================================================

import os, math
from jarray import zeros

from java.awt  import (BorderLayout, Dimension, Color, Font, Component)
from java.awt.geom  import AffineTransform
from java.awt.image import BufferedImage
from java.awt.event import (MouseAdapter, MouseMotionAdapter,
                             MouseWheelListener)

from javax.swing        import (JPanel, JLabel, BoxLayout, Box,
                                 BorderFactory)
from javax.swing.border import EmptyBorder

from ij         import IJ
from ij.process import (ByteProcessor, ShortProcessor, FloatProcessor)

from loci.formats import FormatTools


# ==============================================================
# MergedImagePanel  --  composite view of multiple channels
#
# Behaves like an ImagePanel for interaction (pan / zoom / sync),
# but its render combines all available channel tiles for the same
# set using per-channel contrast + LUT color, additive blending.
# ==============================================================

class MergedImagePanel(JPanel):

    def __init__(self, set_id, ref_w, ref_h,
                 reader_pool, tile_cache):
        JPanel.__init__(self)
        self.set_id        = set_id
        self.ref_w         = ref_w
        self.ref_h         = ref_h
        self.reader_pool   = reader_pool
        self.tile_cache    = tile_cache

        # Channel sources: list of dicts populated by MainWindow.
        # Each entry: {
        #   'ch'          : channel name,
        #   'level_paths' : [...], 'level_dims': [(w,h), ...],
        #   'pixel_type'  : ..., 'little_endian': ...,
        #   'contrast_fn' : fn() -> (min, max),
        #   'lut_fn'      : fn() -> Color,
        # }
        self.sources       = []

        # Reference for coord transforms (use max of channel sources)
        self.img_w         = ref_w
        self.img_h         = ref_h

        self.cx   = ref_w / 2.0
        self.cy   = ref_h / 2.0
        self.zoom = 1.0

        self._drag_prev    = None
        self._render_cache = None
        self.sync_cb       = None
        self.view_rot      = 0       # stage-1 slide orientation
        self.view_flip     = False

        self.setBackground(CELL_BG)
        self.setBorder(BorderFactory.createLineBorder(BORDER_CLR, 1))

        self.addMouseWheelListener(self._make_wheel())
        self.addMouseListener(self._make_mouse())
        self.addMouseMotionListener(self._make_motion())

    def add_source(self, ch, level_paths, level_dims,
                   pixel_type, little_endian,
                   contrast_fn, lut_fn):
        get_async(self.tile_cache).register(self, level_paths)
        self.sources.append({
            'ch'          : ch,
            'level_paths' : level_paths,
            'level_dims'  : level_dims,
            'n_levels'    : len(level_paths),
            'missing'     : unusable_levels(level_paths),
            'pixel_type'  : pixel_type,
            'little_endian': little_endian,
            'contrast_fn' : contrast_fn,
            'lut_fn'      : lut_fn,
        })
        # Use the largest channel for img bounds (for pan range).
        # Source-pixel space: ref_w/ref_h may be view-space (rotated).
        w0, h0 = level_dims[0]
        if len(self.sources) == 1:
            self.img_w, self.img_h = w0, h0
        elif w0 > self.img_w or h0 > self.img_h:
            self.img_w = w0
            self.img_h = h0
        self.cx = self.img_w / 2.0
        self.cy = self.img_h / 2.0

    def set_zoom(self, z):
        self.zoom = z
        self.cx = self.img_w / 2.0
        self.cy = self.img_h / 2.0
        self._render_cache = None
        self.repaint()

    def apply_view(self, cx, cy, zoom):
        self.cx, self.cy, self.zoom = cx, cy, zoom
        self._render_cache = None
        self.repaint()

    def set_orientation(self, rot, flip):
        self.view_rot  = rot
        self.view_flip = bool(flip)
        self._render_cache = None
        self.repaint()

    def invalidate_render(self):
        """Called by contrast/LUT change to force recomposite."""
        self._render_cache = None
        self.repaint()

    def _notify_sync(self):
        if self.sync_cb is not None:
            try:
                self.sync_cb(self, self.cx, self.cy, self.zoom)
            except Exception as e:
                IJ.log("[WARN] sync_cb failed: " + unicode(e))

    # -- coord transforms (same as ImagePanel) -----------------

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

    def _pick_level_for(self, src):
        eff = self._effective_scale()
        if eff <= 0:
            return 0
        try:
            target = int(math.ceil(math.log(1.0 / eff, 2)))
        except Exception:
            target = 0
        target = max(0, min(src['n_levels'] - 1, target))
        while target > 0 and target in src['missing']:
            target -= 1
        return target

    # -- rendering --------------------------------------------

    def paintComponent(self, g):
        super(MergedImagePanel, self).paintComponent(g)
        pw = self.getWidth()
        ph = self.getHeight()
        if pw <= 0 or ph <= 0:
            return
        bw, bh = orient_buf_dims(self)
        bi = self._get_render(bw, bh)
        if bi is None:
            g.setColor(Color(80, 80, 80))
            g.setFont(Font("SansSerif", Font.PLAIN, 11))
            g.drawString("No channels", 10, ph // 2)
            return
        g.drawImage(bi, orient_transform(self, bw, bh), None)

    def _get_render(self, pw, ph):
        if not self.sources:
            return None

        # Build cache key incl. current contrast/lut of every source
        key_parts = [pw, ph,
                     round(self.cx, 2), round(self.cy, 2),
                     round(self.zoom, 4)]
        for src in self.sources:
            cmin, cmax = src['contrast_fn']()
            lut = src['lut_fn']()
            key_parts.append((src['ch'], cmin, cmax, lut.getRGB()))
        key = tuple(key_parts)
        if self._render_cache and self._render_cache[0] == key:
            return self._render_cache[1]

        # Additive merge into R/G/B 8-bit images:
        #   for each channel: gray * (color_r, color_g, color_b) // 255
        #   added with saturation at 255 (Java ops, see add_tinted).
        acc = new_rgb_acc(pw, ph)

        eff = self._effective_scale()
        if eff <= 0:
            # Empty
            bi = BufferedImage(pw, ph, BufferedImage.TYPE_INT_ARGB)
            g2 = bi.createGraphics()
            g2.setColor(CELL_BG); g2.fillRect(0, 0, pw, ph); g2.dispose()
            self._render_cache = (key, bi)
            return bi

        complete = True
        for src in self.sources:
            try:
                if not self._accumulate_channel(src, pw, ph, eff, acc):
                    complete = False
            except Exception as ex:
                IJ.log(u"[WARN] merged accumulate ch {}: {}".format(
                    src.get('ch'), ex))

        bi = rgb_from(acc).getBufferedImage()
        # incomplete renders are not cached: the tile loader repaints us
        self._render_cache = (key, bi) if complete else None
        return bi

    def _accumulate_channel(self, src, pw, ph, eff, acc):
        """Read source region, contrast->8bit, and additively add
        (gray * color // 255) into the [r, g, b] accumulators.
        """
        level        = self._pick_level_for(src)
        lw, lh       = src['level_dims'][level]
        level_path   = src['level_paths'][level]
        scale_factor = float(lw) / src['level_dims'][0][0]
        eff_L        = eff / scale_factor

        cx_L = self.cx * scale_factor
        cy_L = self.cy * scale_factor
        half_iw_L = (pw / 2.0) / eff_L
        half_ih_L = (ph / 2.0) / eff_L

        ix0 = max(0, int(math.floor(cx_L - half_iw_L)))
        iy0 = max(0, int(math.floor(cy_L - half_ih_L)))
        ix1 = min(lw, int(math.ceil(cx_L + half_iw_L)))
        iy1 = min(lh, int(math.ceil(cy_L + half_ih_L)))
        if ix1 <= ix0 or iy1 <= iy0:
            return True

        rw = ix1 - ix0
        rh = iy1 - iy0
        pt = src['pixel_type']
        le = src['little_endian']

        if pt == FormatTools.UINT8 or pt == FormatTools.INT8:
            region_proc = ByteProcessor(rw, rh)
        elif pt == FormatTools.UINT16 or pt == FormatTools.INT16:
            region_proc = ShortProcessor(rw, rh)
        else:
            region_proc = FloatProcessor(rw, rh)

        # overview first, then loaded tiles; missing ones load in the
        # background (AsyncTiles) and repaint this panel when ready
        loader = get_async(self.tile_cache)
        complete = True
        try:
            ov = loader.overview(src['level_paths'][-1], self.reader_pool)
            fill_from_overview(region_proc, ov, src['level_dims'], level, ix0, iy0, rw, rh)
        except Exception as e:
            IJ.log(u"[WARN] overview {}: {}".format(src['level_paths'][-1], e))
        tile_x0 = (ix0 // TILE_SIZE) * TILE_SIZE
        tile_y0 = (iy0 // TILE_SIZE) * TILE_SIZE
        ty = tile_y0
        while ty < iy1:
            tx = tile_x0
            while tx < ix1:
                tile_w = min(TILE_SIZE, lw - tx)
                tile_h = min(TILE_SIZE, lh - ty)
                tile_proc = loader.get(level_path, tx, ty, tile_w, tile_h, pt, le)
                if tile_proc is None:
                    complete = False
                else:
                    region_proc.insert(tile_proc, tx - ix0, ty - iy0)
                tx += TILE_SIZE
            ty += TILE_SIZE

        cmin, cmax = src['contrast_fn']()
        # 8-bit sources too (convertToByte ignores the range of an 8-bit image)
        proc8 = scaled8(region_proc, cmin, cmax)

        # Resize to the panel-space region size using Java2D (bilinear)
        draw_w = max(1, int(round(rw * eff_L)))
        draw_h = max(1, int(round(rh * eff_L)))
        src_bi = proc8.getBufferedImage()

        # Create an intermediate grayscale buffer at draw_w x draw_h
        # via Graphics2D scale.  (TYPE_BYTE_GRAY so we can read
        # 8-bit values back cheaply.)
        scaled = BufferedImage(draw_w, draw_h,
                               BufferedImage.TYPE_BYTE_GRAY)
        gs = scaled.createGraphics()
        at = AffineTransform()
        at.scale(float(draw_w) / src_bi.getWidth(),
                 float(draw_h) / src_bi.getHeight())
        gs.drawImage(src_bi, at, None)
        gs.dispose()

        # Panel-space placement offsets
        s_px0 = pw / 2.0 + (ix0 - cx_L) * eff_L
        s_py0 = ph / 2.0 + (iy0 - cy_L) * eff_L
        px0 = int(round(s_px0))
        py0 = int(round(s_py0))

        # Tint by the channel color and add at the panel offset
        # (copyBits clips to the panel).
        p8s = ByteProcessor(draw_w, draw_h,
                            scaled.getRaster().getDataBuffer().getData(), None)
        add_tinted(acc, p8s, src['lut_fn'](), px0, py0)
        return complete

    # -- interaction (same style as ImagePanel) ----------------

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
# MergedContrastPanel  --  placeholder cell for the merged row
# ==============================================================

class MergedContrastPanel(JPanel):
    def __init__(self):
        JPanel.__init__(self)
        self.setBackground(HEADER_BG)
        self.setBorder(EmptyBorder(6, 8, 6, 8))
        self.setLayout(BorderLayout())

        inner = JPanel()
        inner.setBackground(HEADER_BG)
        inner.setLayout(BoxLayout(inner, BoxLayout.Y_AXIS))

        title = JLabel("Merged")
        title.setFont(Font("SansSerif", Font.BOLD, 13))
        title.setForeground(TEXT_CLR)
        title.setAlignmentX(Component.LEFT_ALIGNMENT)
        inner.add(title)
        inner.add(Box.createVerticalStrut(4))

        hint = JLabel("(preview)")
        hint.setFont(Font("SansSerif", Font.ITALIC, 11))
        hint.setForeground(Color(160, 160, 160))
        hint.setAlignmentX(Component.LEFT_ALIGNMENT)
        inner.add(hint)

        self.add(inner, BorderLayout.NORTH)

