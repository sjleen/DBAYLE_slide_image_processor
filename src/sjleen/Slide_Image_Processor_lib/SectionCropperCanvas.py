# ============================================================
# SectionCropperCanvas.py  (stage 2: Section Cropper)
#
# Slide view (pyramid, stage-1 orientation, exposure + LUT composite)
# with polygon section overlay and editing tools.
#
# Coordinates: everything the canvas stores is in slide VIEW L0 px.
#   screen = (view - c) * s + panel_center
#
# Mouse
#   wheel            zoom at cursor
#   right/mid drag   pan
#   Edit   click = select (Shift/Ctrl toggles), drag = move selected,
#          drag on empty = box select, drag a vertex of the single
#          selected section = move vertex, double-click edge = add
#          vertex, Alt+click vertex = delete vertex (hand-drawn sections
#          only; detected sections cannot be dragged)
#   Draw   click = add point, double-click / Enter / click first point
#          = close, Backspace = undo point, Esc = cancel
#   Wand   click inside tissue = add section from the detection mask
#   Split  click points of a cut line, double-click / Enter = cut;
#          both parts are re-detected (seeded growth through tissue)
#   Order  click sections in the wanted order, Enter / Esc = done
# Keys: Delete, Ctrl+A, F = fit
# ============================================================

import math, threading
from collections import OrderedDict

from java.awt       import (Color, BasicStroke, Font, RenderingHints,
                            AlphaComposite, Cursor)
from java.awt.event import (MouseAdapter, MouseMotionAdapter,
                            MouseWheelListener, KeyAdapter, KeyEvent,
                            InputEvent, MouseEvent)
from java.awt.geom  import AffineTransform, Path2D, Line2D
from java.awt.image import IndexColorModel
from jarray         import zeros
from javax.swing    import JPanel

from ij import IJ

import SectionPipelineProject as spp
import SectionCropperCore as core


BG        = Color(30, 30, 30)
C_AUTO    = Color(29, 158, 117)
C_EDITED  = Color(55, 138, 221)
C_SEL     = Color(240, 120, 60)
C_EXCL    = Color(140, 140, 140)
C_PAD     = Color(240, 200, 80)
C_TEMP    = Color(255, 230, 90)
HANDLE    = 7          # vertex hit radius, screen px
TILE        = 512        # background-loaded tile size (level px)
RAW_TILES   = 400        # cached raw tiles (all channels)
RGB_TILES   = 200        # cached composited tiles
OVERVIEW_PX = 2048       # overview long side, loaded once per slide
MODES     = ('edit', 'draw', 'wand', 'split', 'order')

STROKE    = BasicStroke(1.6)
STROKE_SEL = BasicStroke(2.6)
STROKE_PAD = BasicStroke(1.0, BasicStroke.CAP_BUTT, BasicStroke.JOIN_MITER,
                         10.0, [5.0, 4.0], 0.0)
STROKE_EXCL = BasicStroke(1.4, BasicStroke.CAP_BUTT, BasicStroke.JOIN_MITER,
                          10.0, [3.0, 4.0], 0.0)


def _mask_cm():
    r = zeros(256, 'b'); g = zeros(256, 'b'); b = zeros(256, 'b'); a = zeros(256, 'b')
    r[255] = -1; g[255] = -56; b[255] = 0; a[255] = 70   # (255, 200, 0, 70)
    return IndexColorModel(8, 256, r, g, b, a)


class SlideCanvas(JPanel):

    def __init__(self, win):
        JPanel.__init__(self)
        self.win  = win
        self.src  = None
        self.pool = core.ReaderPool()       # EDT-only
        self.cx = self.cy = 0.0
        self.s  = 0.1
        self.mode = 'edit'
        self.selected = []
        self.show_mask = False
        self.show_pad  = True
        self._pad      = {}       # sec_id -> (key, padded poly)
        self._mask_img = None     # (mask object id, image)
        self._drag     = None     # dict(kind=..., ...)
        self._temp     = []       # draw points
        self._split_p1 = None
        self._hover    = None
        self.setBackground(BG)
        self.setFocusable(True)
        self._install_listeners()
        self._start_loader()

    # ---------------------------------------------------------- state
    def set_slide(self, src):
        self.src = src
        self._reset_tiles()
        self._pad = {}
        self._mask_img = None
        self.selected = []
        self._temp = []
        self._split_p1 = None
        self.fit()

    def fit(self):
        if self.src is None:
            return
        pw, ph = max(1, self.getWidth()), max(1, self.getHeight())
        self.s  = 0.95 * min(float(pw) / self.src.vw, float(ph) / self.src.vh)
        self.cx = self.src.vw / 2.0
        self.cy = self.src.vh / 2.0
        self.repaint()

    def invalidate_image(self):
        self.repaint()          # tile / overview keys carry channels + exposure

    def invalidate_overlay(self, sec_ids=None):
        if sec_ids is None:
            self._pad = {}
        else:
            for i in sec_ids:
                self._pad.pop(i, None)
        self.repaint()

    def set_mode(self, mode):
        self.mode = mode
        self._temp = []
        self._split_p1 = None
        self.setCursor(Cursor.getPredefinedCursor(
            Cursor.DEFAULT_CURSOR if mode == 'edit' else Cursor.CROSSHAIR_CURSOR))
        self.win.on_mode_changed(mode)
        self.repaint()

    def set_selection(self, ids, notify=True):
        self.selected = list(ids)
        if notify:
            self.win.on_canvas_selection(self.selected)
        self.repaint()

    # ---------------------------------------------------------- coords
    def to_screen(self, x, y):
        return ((x - self.cx) * self.s + self.getWidth() / 2.0,
                (y - self.cy) * self.s + self.getHeight() / 2.0)

    def to_view(self, px, py):
        return ((px - self.getWidth() / 2.0) / self.s + self.cx,
                (py - self.getHeight() / 2.0) / self.s + self.cy)

    def _view_tx(self):
        at = AffineTransform()
        at.translate(self.getWidth() / 2.0, self.getHeight() / 2.0)
        at.scale(self.s, self.s)
        at.translate(-self.cx, -self.cy)
        return at

    # ---------------------------------------------------------- image
    # Never reads files on the paint thread:
    #   - an overview (whole slide, long side ~OVERVIEW_PX) is loaded once
    #     per slide and always drawn first
    #   - finer levels come as 512 px tiles from a background thread and
    #     are drawn on top as they arrive (the view stays responsive; it
    #     sharpens), cached raw (per channel) and composited (per channel
    #     set + exposure); the ring of tiles around the view is prefetched
    def _start_loader(self):
        self._lock = threading.Condition()
        self._want = []
        self._gen = 0
        self._raw = OrderedDict()      # (ch, L, tx, ty) -> processor
        self._rgb = OrderedDict()      # (L, tx, ty, chans, ev) -> BufferedImage
        self._failed = set()
        self._ov = None                # (L, {ch: processor})
        self._ov_img = None            # (key, BufferedImage)
        self._ov_req = None
        self._ov_job = False
        self._stop = False
        t = threading.Thread(target=self._loader)
        t.setDaemon(True)
        t.start()

    def _reset_tiles(self):
        """New slide: drop cached tiles; the overview is read by the tile
        thread (first job), never on the EDT."""
        with self._lock:
            self._gen += 1
            self._raw.clear(); self._rgb.clear(); self._failed.clear()
            self._want = []
            self._ov, self._ov_img = None, None
            self._ov_req = None            # composite wanted for (chans, ev)
            self._ov_job = self.src is not None
            self._lock.notifyAll()

    def stop_loader(self):
        """Window closed: end the tile thread (it closes its readers)."""
        with self._lock:
            self._stop = True
            self._lock.notifyAll()

    def _loader(self):
        pool = core.ReaderPool()
        try:
            self._load_tiles(pool)
        finally:
            pool.close()

    def _next_job(self):
        """Under the lock: ('ov', None) | ('comp', key) | ('tile', key) | None."""
        if self._ov_job:
            self._ov_job = False
            return ('ov', None)
        if self._ov is not None and self._ov_req is not None:
            k, self._ov_req = self._ov_req, None
            if self._ov_img is None or self._ov_img[0] != k:
                return ('comp', k)
        for k in self._want:
            if k not in self._raw and k not in self._failed:
                return ('tile', k)
        return None

    def _load_tiles(self, pool):
        while True:
            with self._lock:
                job = None
                while job is None and not self._stop:
                    job = self._next_job()
                    if job is None:
                        self._lock.wait()
                if self._stop:
                    return
                gen, src = self._gen, self.src
                ov = self._ov
            kind, key = job
            if kind == 'ov':
                L = src.level_for_target(OVERVIEW_PX)
                raw = {}
                for ch in src.channels():
                    try:
                        raw[ch] = src.read_full(pool, ch, L)
                    except Exception as e:
                        IJ.log("[Cropper] overview %s: %s" % (ch, e))
                with self._lock:
                    if gen == self._gen:
                        self._ov = (L, raw)
                self.repaint()
                continue
            if kind == 'comp':
                chans, ev = key
                try:
                    cp = core.composite(ov[1], self.win.proj, list(chans))
                    img = cp.getBufferedImage() if cp else None
                except Exception as e:
                    IJ.log("[Cropper] overview composite: %s" % e)
                    img = None
                with self._lock:
                    if gen == self._gen:
                        self._ov_img = (key, img)
                self.repaint()
                continue
            ch, L, tx, ty = key
            try:
                lvw, lvh = src.level_view_dims(ch, L)
                rect = (tx * TILE, ty * TILE,
                        min(TILE, lvw - tx * TILE), min(TILE, lvh - ty * TILE))
                ip = src.read_view(pool, ch, L, rect)
            except Exception as e:
                IJ.log("[Cropper] tile %s L%d (%d,%d): %s" % (ch, L, tx, ty, e))
                ip = None
            with self._lock:
                if gen != self._gen:
                    continue
                if ip is None:
                    self._failed.add(key)
                else:
                    self._raw[key] = ip
                    while len(self._raw) > RAW_TILES:
                        self._raw.popitem(last=False)
            self.repaint()                      # thread-safe, coalesced

    def _paint_image(self, g2, pw, ph):
        src = self.src
        chans = tuple(self.win.visible_channels())
        if not chans:
            return
        if self._ov is None:
            g2.setColor(Color(150, 150, 150))
            g2.drawString("Loading slide...", 12, 20)
            return
        ev = self.win.exposure_version
        Lo, ovraw = self._ov
        key = (chans, ev)
        if self._ov_img is None or self._ov_img[0] != key:
            with self._lock:               # composite on the tile thread
                self._ov_req = key
                self._lock.notifyAll()
        if self._ov_img is not None and self._ov_img[1] is not None:
            # (the previous composite until the new one is ready)
            at = self._view_tx()
            fo = src.level_scale(Lo)
            at.scale(1.0 / fo, 1.0 / fo)
            g2.drawImage(self._ov_img[1], at, None)
        L = src.pick_level(self.s)
        if L >= Lo:
            return                              # overview is fine enough
        with self._lock:
            raw = dict(self._raw)
        want, ring = [], []
        self._draw_level(g2, L, pw, ph, chans, ev, raw, want, ring)
        with self._lock:
            self._want = want + ring
            self._lock.notifyAll()

    def _draw_level(self, g2, L, pw, ph, chans, ev, raw, want, ring):
        """Draw the loaded tiles of level L that cover the view; missing
        visible tiles go to `want`, the ring around the view to `ring`
        (None = no ring)."""
        src = self.src
        f = src.level_scale(L)
        lvw, lvh = src.level_view_dims(chans[0], L)
        ox = self.cx - pw / (2.0 * self.s)
        oy = self.cy - ph / (2.0 * self.s)
        tx0 = max(0, int(math.floor(ox * f / TILE)))
        ty0 = max(0, int(math.floor(oy * f / TILE)))
        tx1 = min((lvw - 1) // TILE, int(math.floor((ox + pw / self.s) * f / TILE)))
        ty1 = min((lvh - 1) // TILE, int(math.floor((oy + ph / self.s) * f / TILE)))
        m = 1 if ring is not None else 0
        for ty in range(max(0, ty0 - m), min((lvh - 1) // TILE, ty1 + m) + 1):
            for tx in range(max(0, tx0 - m), min((lvw - 1) // TILE, tx1 + m) + 1):
                visible = tx0 <= tx <= tx1 and ty0 <= ty <= ty1
                rk = (L, tx, ty, chans, ev)
                img = self._rgb.get(rk)
                if img is None:
                    ks = [(ch, L, tx, ty) for ch in chans]
                    missing = [k for k in ks if k not in raw]
                    if missing:
                        (want if visible else ring).extend(missing)
                        continue
                    if not visible:
                        continue
                    cp = core.composite(dict((k[0], raw[k]) for k in ks),
                                        self.win.proj, list(chans))
                    if cp is None:
                        continue
                    img = cp.getBufferedImage()
                    self._rgb[rk] = img
                    while len(self._rgb) > RGB_TILES:
                        self._rgb.popitem(last=False)
                if visible:
                    at = self._view_tx()
                    at.translate(tx * TILE / f, ty * TILE / f)
                    at.scale(1.0 / f, 1.0 / f)
                    g2.drawImage(img, at, None)

    # ---------------------------------------------------------- paint
    def paintComponent(self, g):
        super(SlideCanvas, self).paintComponent(g)
        if self.src is None:
            g.setColor(Color(150, 150, 150))
            g.drawString("No slide", 20, 30)
            return
        g2 = g.create()
        g2.setRenderingHint(RenderingHints.KEY_INTERPOLATION,
                            RenderingHints.VALUE_INTERPOLATION_BILINEAR)
        g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                            RenderingHints.VALUE_ANTIALIAS_ON)
        pw, ph = self.getWidth(), self.getHeight()
        try:
            self._paint_image(g2, pw, ph)
        except Exception as e:
            IJ.log("[Cropper] render: %s" % e)
        if self.show_mask:
            self._paint_mask(g2)
        self._paint_sections(g2)
        self._paint_temp(g2)
        g2.dispose()

    def _paint_mask(self, g2):
        m = self.win.get_mask()
        if m is None:
            return
        mask, f = m
        if self._mask_img is None or self._mask_img[0] is not mask:
            d = mask.duplicate()
            d.setColorModel(_mask_cm())
            self._mask_img = (mask, d.createImage())
        at = self._view_tx()
        at.scale(1.0 / f, 1.0 / f)
        g2.drawImage(self._mask_img[1], at, None)

    def _screen_path(self, poly):
        path = Path2D.Double()
        x, y = self.to_screen(poly[0][0], poly[0][1])
        path.moveTo(x, y)
        for p in poly[1:]:
            x, y = self.to_screen(p[0], p[1])
            path.lineTo(x, y)
        path.closePath()
        return path

    def _padded(self, sec_id):
        proj = self.win.proj
        s = proj['sections'][sec_id]
        pad = spp.section_padding(proj, sec_id)
        key = (len(s['polygon']), tuple(s['polygon'][0]), tuple(s['polygon'][-1]),
               spp.poly_area(s['polygon']), pad, spp.padding_mode(proj),
               self._sig)
        c = self._pad.get(sec_id)
        if c is None or c[0] != key:
            try:
                pp = spp.padded_shape(proj, sec_id, max_dim=400)
            except Exception:
                pp = s['polygon']
            c = (key, pp)
            self._pad[sec_id] = c
        return c[1]

    def _paint_sections(self, g2):
        proj = self.win.proj
        self._sig = spp.slide_signature(proj, self.src.sid)
        ids = spp.slide_section_ids(proj, self.src.sid)
        nums = self.win.numbers
        sel = set(self.selected)
        font = Font("SansSerif", Font.BOLD, 14)
        g2.setFont(font)
        for sec_id in ids:
            s = proj['sections'][sec_id]
            poly = s['polygon']
            if len(poly) < 3:
                continue
            excl = s.get('excluded')
            if self.show_pad and not excl:
                g2.setColor(C_PAD); g2.setStroke(STROKE_PAD)
                g2.draw(self._screen_path(self._padded(sec_id)))
            if sec_id in sel:
                g2.setColor(C_SEL); g2.setStroke(STROKE_SEL)
            elif excl:
                g2.setColor(C_EXCL); g2.setStroke(STROKE_EXCL)
            else:
                g2.setColor(C_AUTO if s.get('origin') == 'auto' else C_EDITED)
                g2.setStroke(STROKE)
            g2.draw(self._screen_path(poly))
            cx, cy = self.to_screen(*spp.poly_centroid(poly))
            label = "x" if excl else nums.get(sec_id, "?")
            if self.mode == 'order' and sec_id in self.win.order_clicked:
                label = "%s (%d)" % (label, self.win.order_clicked.index(sec_id) + 1)
            fm = g2.getFontMetrics()
            tw = fm.stringWidth(label)
            g2.setColor(Color(0, 0, 0, 170))
            g2.fillRoundRect(int(cx - tw / 2 - 4), int(cy - 11), tw + 8, 18, 6, 6)
            g2.setColor(Color.WHITE)
            g2.drawString(label, int(cx - tw / 2), int(cy + 4))
        if self.mode == 'edit' and len(self.selected) == 1 \
                and self._drawn(self.selected[0]):
            g2.setColor(C_SEL)
            for x, y in proj['sections'][self.selected[0]]['polygon']:
                sx, sy = self.to_screen(x, y)
                g2.fillRect(int(sx) - 3, int(sy) - 3, 7, 7)

    def _paint_temp(self, g2):
        g2.setColor(C_TEMP)
        g2.setStroke(BasicStroke(1.5))
        d = self._drag
        if d is not None and d['kind'] == 'box':
            x0, y0 = d['p0']; x1, y1 = d['p1']
            g2.drawRect(int(min(x0, x1)), int(min(y0, y1)),
                        int(abs(x1 - x0)), int(abs(y1 - y0)))
        if self.mode in ('draw', 'split') and self._temp:
            pts = [self.to_screen(x, y) for x, y in self._temp]
            for i in range(1, len(pts)):
                g2.draw(Line2D.Double(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
            if self._hover is not None:
                g2.draw(Line2D.Double(pts[-1][0], pts[-1][1], self._hover[0], self._hover[1]))
            for x, y in pts:
                g2.fillRect(int(x) - 2, int(y) - 2, 5, 5)

    # ---------------------------------------------------------- hit tests
    def _hit_section(self, vx, vy):
        proj = self.win.proj
        ids = spp.slide_section_ids(proj, self.src.sid)
        for sec_id in reversed(ids):
            if core.point_in_poly(proj['sections'][sec_id]['polygon'], vx, vy):
                return sec_id
        return None

    def _hit_vertex(self, sec_id, px, py):
        poly = self.win.proj['sections'][sec_id]['polygon']
        for i, (x, y) in enumerate(poly):
            sx, sy = self.to_screen(x, y)
            if abs(sx - px) <= HANDLE and abs(sy - py) <= HANDLE:
                return i
        return None

    def _hit_edge(self, sec_id, px, py):
        poly = self.win.proj['sections'][sec_id]['polygon']
        n = len(poly)
        for i in range(n):
            a = self.to_screen(*poly[i]); b = self.to_screen(*poly[(i + 1) % n])
            if Line2D.ptSegDist(a[0], a[1], b[0], b[1], px, py) <= HANDLE:
                return i
        return None

    # ---------------------------------------------------------- input
    def _install_listeners(self):
        cv = self

        class Wheel(MouseWheelListener):
            def mouseWheelMoved(_s, e):
                if cv.src is None:
                    return
                k = 1.0 / 1.2 if e.getWheelRotation() > 0 else 1.2
                vx, vy = cv.to_view(e.getX(), e.getY())
                cv.s = max(1e-4, min(cv.s * k, 8.0))
                cv.cx = vx - (e.getX() - cv.getWidth() / 2.0) / cv.s
                cv.cy = vy - (e.getY() - cv.getHeight() / 2.0) / cv.s
                cv.repaint()

        class Mouse(MouseAdapter):
            def mousePressed(_s, e):
                cv.requestFocusInWindow()
                if cv.src is not None:
                    cv._press(e)

            def mouseReleased(_s, e):
                if cv.src is not None:
                    cv._release(e)

            def mouseClicked(_s, e):
                if cv.src is not None and e.getClickCount() == 2:
                    cv._double(e)

        class Motion(MouseMotionAdapter):
            def mouseDragged(_s, e):
                if cv.src is not None:
                    cv._dragged(e)

            def mouseMoved(_s, e):
                cv._hover = (e.getX(), e.getY())
                if cv.src is not None:
                    vx, vy = cv.to_view(e.getX(), e.getY())
                    cv.win.show_coords(vx, vy)
                if cv.mode in ('draw', 'split'):
                    cv.repaint()

        class Keys(KeyAdapter):
            def keyPressed(_s, e):
                cv._key(e)

        self.addMouseWheelListener(Wheel())
        self.addMouseListener(Mouse())
        self.addMouseMotionListener(Motion())
        self.addKeyListener(Keys())

    def _press(self, e):
        px, py = e.getX(), e.getY()
        vx, vy = self.to_view(px, py)
        if e.getButton() != MouseEvent.BUTTON1:
            self._drag = {'kind': 'pan', 'last': (px, py)}
            return
        m = self.mode
        if m == 'draw':
            if len(self._temp) >= 3:
                fx, fy = self.to_screen(*self._temp[0])
                if abs(fx - px) <= HANDLE and abs(fy - py) <= HANDLE:
                    self._finish_draw()
                    return
            self._temp.append([vx, vy])
            self.repaint()
            return
        if m == 'wand':
            self.win.wand_add(vx, vy)
            return
        if m == 'split':
            self._temp.append([vx, vy])
            self.repaint()
            return
        if m == 'order':
            hit = self._hit_section(vx, vy)
            if hit is not None:
                self.win.order_click(hit)
            return
        # edit: only sections drawn by hand can be dragged / reshaped;
        # detected ones are select-only
        multi = e.isShiftDown() or e.isControlDown()
        if len(self.selected) == 1 and not multi and self._drawn(self.selected[0]):
            vi = self._hit_vertex(self.selected[0], px, py)
            if vi is not None:
                if e.isAltDown():
                    self.win.delete_vertex(self.selected[0], vi)
                    return
                self._drag = {'kind': 'vertex', 'id': self.selected[0],
                              'vi': vi, 'started': False}
                return
        hit = self._hit_section(vx, vy)
        if hit is not None:
            if multi:
                sel = list(self.selected)
                if hit in sel: sel.remove(hit)
                else: sel.append(hit)
                self.set_selection(sel, notify=True)
                return
            if hit not in self.selected:
                self.set_selection([hit], notify=True)
            if all(self._drawn(i) for i in self.selected):
                self._drag = {'kind': 'move', 'last': (vx, vy), 'started': False}
            return
        if not multi:
            self.set_selection([], notify=True)
        self._drag = {'kind': 'box', 'p0': (px, py), 'p1': (px, py), 'add': multi}

    def _dragged(self, e):
        d = self._drag
        if d is None:
            return
        px, py = e.getX(), e.getY()
        if d['kind'] == 'pan':
            lx, ly = d['last']
            self.cx -= (px - lx) / self.s
            self.cy -= (py - ly) / self.s
            d['last'] = (px, py)
            self.repaint()
            return
        vx, vy = self.to_view(px, py)
        secs = self.win.proj['sections']
        if d['kind'] == 'vertex':
            if not d['started']:
                self.win.begin_edit(); d['started'] = True
            secs[d['id']]['polygon'][d['vi']] = [vx, vy]
            self.repaint()
        elif d['kind'] == 'move':
            if not d['started']:
                self.win.begin_edit(); d['started'] = True
            lx, ly = d['last']
            dx, dy = vx - lx, vy - ly
            for i in self.selected:
                secs[i]['polygon'] = [[x + dx, y + dy] for x, y in secs[i]['polygon']]
            d['last'] = (vx, vy)
            self.repaint()
        elif d['kind'] == 'box':
            d['p1'] = (px, py)
            self.repaint()

    def _release(self, e):
        d, self._drag = self._drag, None
        if d is None:
            return
        if d['kind'] in ('vertex', 'move') and d.get('started'):
            ids = [d['id']] if d['kind'] == 'vertex' else list(self.selected)
            self.win.geometry_edited(ids)
        elif d['kind'] == 'box':
            (x0, y0), (x1, y1) = d['p0'], d['p1']
            if abs(x1 - x0) < 3 and abs(y1 - y0) < 3:
                self.repaint()
                return
            a = self.to_view(min(x0, x1), min(y0, y1))
            b = self.to_view(max(x0, x1), max(y0, y1))
            proj = self.win.proj
            hits = []
            for sec_id in spp.slide_section_ids(proj, self.src.sid):
                cx, cy = spp.poly_centroid(proj['sections'][sec_id]['polygon'])
                if a[0] <= cx <= b[0] and a[1] <= cy <= b[1]:
                    hits.append(sec_id)
            sel = list(self.selected) if d['add'] else []
            for h in hits:
                if h not in sel: sel.append(h)
            self.set_selection(sel, notify=True)

    def _double(self, e):
        if self.mode == 'draw':
            if self._temp and len(self._temp) > 3:
                self._temp.pop()          # the 2nd click of the double
            self._finish_draw()
            return
        if self.mode == 'split':
            if self._temp and len(self._temp) > 2:
                self._temp.pop()
            self._finish_split()
            return
        if self.mode == 'edit' and len(self.selected) == 1 and self._drawn(self.selected[0]):
            ei = self._hit_edge(self.selected[0], e.getX(), e.getY())
            if ei is not None and self._hit_vertex(self.selected[0], e.getX(), e.getY()) is None:
                vx, vy = self.to_view(e.getX(), e.getY())
                self.win.insert_vertex(self.selected[0], ei + 1, [vx, vy])

    def _finish_draw(self):
        pts, self._temp = self._temp, []
        if len(pts) >= 3:
            self.win.add_drawn(pts)
        self.repaint()

    def _finish_split(self):
        pts, self._temp = self._temp, []
        if len(pts) >= 2:
            self.win.split_poly(pts)
        self.repaint()

    def _drawn(self, sec_id):
        s = self.win.proj['sections'].get(sec_id)
        return s is not None and s.get('origin') == 'drawn'

    def _key(self, e):
        k = e.getKeyCode()
        ctrl = e.isControlDown()
        if k == KeyEvent.VK_ESCAPE:
            if self.mode == 'order':
                self.win.order_done()
            self.set_mode('edit')
        elif k == KeyEvent.VK_ENTER:
            if self.mode == 'draw':
                self._finish_draw()
            elif self.mode == 'split':
                self._finish_split()
            elif self.mode == 'order':
                self.win.order_done()
                self.set_mode('edit')
        elif k in (KeyEvent.VK_DELETE, KeyEvent.VK_BACK_SPACE):
            if self.mode in ('draw', 'split'):
                if self._temp: self._temp.pop()
                self.repaint()
            elif self.selected:
                self.win.delete_selected()
        elif ctrl and k == KeyEvent.VK_A:
            self.set_selection(spp.slide_section_ids(self.win.proj, self.src.sid),
                               notify=True)
        elif k == KeyEvent.VK_F and not ctrl:
            self.fit()
