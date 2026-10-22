# ============================================================
# SectionOrienterWindow.py  (stage 3: Section Orienter)
#
#   +--------------------------------------------------------+
#   | Validated | one cell per section of the current sample |
#   | Modified  | (filled = yes, current cell outlined)      |
#   | info: name, i / n in sample, overall, tags             |
#   |                                                        |
#   |            current image, as large as possible,        |
#   |            shown with its flip / rotation applied      |
#   |                                                        |
#   | tools: sample, preview, rotate, keys, save, apply      |
#   +--------------------------------------------------------+
#
# Default keys (editable with "Keys...", stored in Fiji prefs):
#   Left / Right = previous / next image, Up = toggle validated,
#   Down = flip left-right, R / Shift+R = rotate 90 CW / CCW,
#   Ctrl+Z = undo, Ctrl+S = save
# Click a cell = go there, Shift+click = select a range (actions then
# apply to the whole range).
# ============================================================

import os, math, time, threading, traceback
from jarray import zeros

from java.awt       import (BorderLayout, FlowLayout, GridLayout, Dimension,
                            Color, Font, BasicStroke, RenderingHints,
                            AlphaComposite, Insets)
from java.awt.event import (ActionListener, MouseAdapter, WindowAdapter,
                            KeyEvent, KeyAdapter, ComponentAdapter)
from java.awt.geom  import AffineTransform
from java.awt.image import BufferedImage, LookupOp, ByteLookupTable
from java.lang      import Runnable, Runtime, System
from javax.swing.event import ChangeListener
from javax.swing    import (JFrame, JPanel, JLabel, JButton, JCheckBox,
                            JComboBox, JTextField, JScrollPane, JOptionPane,
                            JDialog, JTable, JFileChooser, BorderFactory,
                            KeyStroke, AbstractAction, JComponent,
                            SwingUtilities, ListSelectionModel, Box, JSlider,
                            SwingConstants, Timer, BoxLayout)
from javax.swing.plaf.basic import BasicArrowButton

from ij import IJ, Prefs

import SectionPipelineProject as spp
import SectionOrienterCore as oc
import SectionCropperExport as cex      # ProgressDialog, write_csv, _ROModel


BG       = Color(28, 28, 28)
C_EMPTY  = Color(62, 62, 62)
C_VALID  = Color(60, 175, 95)
C_MOD    = Color(235, 145, 40)
C_CUR    = Color(255, 255, 255)
C_SELR   = Color(90, 150, 255)
SAMPLE_PX   = 3000     # in-memory width of a sample's enclosing box
MEM_CAP_FRAC = 0.75    # heap use above this: stop preloading every sample
LOAD_THREADS = 4       # images decoded in parallel (~0.4 s per big JPG each)

ACTIONS = [  # id, label, default key
    ('prev',     "Previous image",     "LEFT"),
    ('next',     "Next image",         "RIGHT"),
    ('prev_sample', "Previous sample",  "PAGE_UP"),
    ('next_sample', "Next sample",      "PAGE_DOWN"),
    ('validate', "Toggle validated",   "UP"),
    ('flip',     "Flip left-right",    "DOWN"),
    ('rot_cw',   "Rotate 90 CW",       "R"),
    ('rot_ccw',  "Rotate 90 CCW",      "shift R"),
    ('ang_ccw',  "Rotate 1 degree CCW", "A"),
    ('ang_cw',   "Rotate 1 degree CW",  "S"),
    ('undo',     "Undo",               "ctrl Z"),
    ('save',     "Save",               "ctrl S"),
]
PREF = "section_orienter.key."


def parse_ks(s):
    """"R" / "shift R" -> KeyStroke for the key PRESS (a bare letter would
    otherwise parse as a typed character and miss the unshifted key)."""
    if not s:
        return None
    parts = s.split()
    if not any(p in ("pressed", "released", "typed") for p in parts):
        parts.insert(len(parts) - 1, "pressed")
    return KeyStroke.getKeyStroke(" ".join(parts))


def key_text(ks_str):
    ks = parse_ks(ks_str)
    if ks is None:
        return "-"
    mods = KeyEvent.getModifiersExText(ks.getModifiers())
    k = KeyEvent.getKeyText(ks.getKeyCode())
    return (mods + "+" + k) if mods else k


def ks_string(ks):
    s = ks.toString().replace("pressed ", "")
    return s


class _Act(ActionListener):
    def __init__(self, fn): self.fn = fn
    def actionPerformed(self, e):
        try: self.fn()
        except Exception as ex:
            IJ.log("[Orienter] %s" % ex); IJ.log(traceback.format_exc())


class _KeyAct(AbstractAction):
    def __init__(self, fn):
        AbstractAction.__init__(self); self.fn = fn
    def actionPerformed(self, e):
        try: self.fn()
        except Exception as ex:
            IJ.log("[Orienter] %s" % ex); IJ.log(traceback.format_exc())


class _Run(Runnable):
    def __init__(self, fn): self.fn = fn
    def run(self):
        try: self.fn()
        except Exception as ex:
            IJ.log("[Orienter] %s" % ex); IJ.log(traceback.format_exc())


def on_edt(fn):
    SwingUtilities.invokeLater(_Run(fn))


def _btn(text, fn, tip=None):
    b = JButton(text)
    b.setMargin(Insets(2, 7, 2, 7))
    b.setFocusable(False)
    if tip: b.setToolTipText(tip)
    b.addActionListener(_Act(fn))
    return b


def _group(title, comps):
    """Titled box around a row of controls."""
    p = JPanel(FlowLayout(FlowLayout.LEFT, 3, 0))
    b = BorderFactory.createTitledBorder(title)
    b.setTitleFont(Font("SansSerif", Font.PLAIN, 11))
    p.setBorder(b)
    for c in comps:
        p.add(c)
    return p


def draw_oriented(g2, bi, tags, x, y, w, h, alpha=1.0):
    """Fit bi into the box, flip first then rotate clockwise."""
    iw, ih = bi.getWidth(), bi.getHeight()
    a = math.radians(oc.norm_rot(tags.get('rot', 0)))
    rw = abs(iw * math.cos(a)) + abs(ih * math.sin(a))
    rh = abs(iw * math.sin(a)) + abs(ih * math.cos(a))
    s = min(float(w) / rw, float(h) / rh)
    at = AffineTransform()
    at.translate(x + w / 2.0, y + h / 2.0)
    at.rotate(a)
    at.scale(-s if tags.get('flip_h') else s, s)
    at.translate(-iw / 2.0, -ih / 2.0)
    if alpha < 1.0:
        old = g2.getComposite()
        g2.setComposite(AlphaComposite.getInstance(AlphaComposite.SRC_OVER, alpha))
        g2.drawImage(bi, at, None)
        g2.setComposite(old)
    else:
        g2.drawImage(bi, at, None)


# ==============================================================
# Status strip: one cell per section of the current sample
# ==============================================================

class Strip(JPanel):
    LABEL_W, ROW_H, GAP, NUM_H = 78, 22, 3, 13

    def __init__(self, win):
        JPanel.__init__(self)
        self.win = win
        self.setBackground(BG)
        self.setPreferredSize(Dimension(100, 2 * self.ROW_H + self.GAP + self.NUM_H + 12))
        strip = self
        class M(MouseAdapter):
            def mousePressed(_s, e):
                i = strip.cell_at(e.getX())
                if i is not None:
                    strip.win.click_cell(i, e.isShiftDown())
        self.addMouseListener(M())

    def _geom(self):
        idx = self.win.sample_idx()
        n = max(1, len(idx))
        x0 = self.LABEL_W
        cw = float(self.getWidth() - x0 - 8) / n
        return idx, x0, cw

    def cell_at(self, x):
        idx, x0, cw = self._geom()
        if x < x0 or cw <= 0:
            return None
        k = int((x - x0) / cw)
        return idx[k] if 0 <= k < len(idx) else None

    def paintComponent(self, g):
        super(Strip, self).paintComponent(g)
        g2 = g.create()
        g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON)
        win = self.win
        idx, x0, cw = self._geom()
        y1 = 6
        y2 = y1 + self.ROW_H + self.GAP
        g2.setFont(Font("SansSerif", Font.BOLD, 12))
        g2.setColor(Color(200, 200, 200))
        g2.drawString("Validated", 8, y1 + 16)
        g2.drawString("Modified", 8, y2 + 16)
        sel = win.selection_range()
        small = Font("SansSerif", Font.PLAIN, 10)
        for k, i in enumerate(idx):
            it = win.items[i]
            x = int(x0 + k * cw)
            w = max(1, int(x0 + (k + 1) * cw) - x - (1 if cw >= 4 else 0))
            g2.setColor(C_VALID if it.validated else C_EMPTY)
            g2.fillRect(x, y1, w, self.ROW_H)
            g2.setColor(C_MOD if it.modified() else C_EMPTY)
            g2.fillRect(x, y2, w, self.ROW_H)
            if sel and i in sel:
                g2.setColor(C_SELR)
                g2.fillRect(x, y2 + self.ROW_H + 2, w, 3)
            if cw >= 18:
                g2.setFont(small)
                g2.setColor(Color(150, 150, 150))
                g2.drawString(str(k + 1), x + 2, y2 + self.ROW_H + 13)
        if win.cur in idx:
            k = idx.index(win.cur)
            x = int(x0 + k * cw)
            w = max(3, int(x0 + (k + 1) * cw) - x)
            g2.setColor(C_CUR)
            g2.setStroke(BasicStroke(2.5))
            g2.drawRect(x - 1, y1 - 2, w + 1, 2 * self.ROW_H + self.GAP + 3)
        g2.dispose()


# ==============================================================
# Main image: one zoom for the whole sample (the box enclosing every
# image of the sample fits the view), each image top-center aligned
# ==============================================================

def draw_top_center(g2, bi, tags, s, view_w, top, alpha=1.0):
    iw, ih = bi.getWidth(), bi.getHeight()
    rw, rh = oc.oriented_size(iw, ih, tags)
    at = AffineTransform()
    at.translate(view_w / 2.0, top + rh * s / 2.0)
    at.rotate(math.radians(oc.norm_rot(tags.get('rot', 0))))
    at.scale(-s if tags.get('flip_h') else s, s)
    at.translate(-iw / 2.0, -ih / 2.0)
    if alpha < 1.0:
        old = g2.getComposite()
        g2.setComposite(AlphaComposite.getInstance(AlphaComposite.SRC_OVER, alpha))
        g2.drawImage(bi, at, None)
        g2.setComposite(old)
    else:
        g2.drawImage(bi, at, None)


class ImageView(JPanel):
    M = 8

    def __init__(self, win):
        JPanel.__init__(self)
        self.win = win
        self.setBackground(BG)
        self.setFocusable(True)

    def paintComponent(self, g):
        super(ImageView, self).paintComponent(g)
        win = self.win
        if not win.items:
            return
        g2 = g.create()
        g2.setRenderingHint(RenderingHints.KEY_INTERPOLATION,
                            RenderingHints.VALUE_INTERPOLATION_BILINEAR)
        g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON)
        w, h = self.getWidth(), self.getHeight()
        m = self.M
        it = win.items[win.cur]
        bi = win.cache_get(it.key)
        s = win.display_scale(w - 2 * m, h - 2 * m)
        if not bi or s is None:
            g2.setColor(Color(170, 170, 170))
            g2.setFont(Font("SansSerif", Font.PLAIN, 16))
            g2.drawString("cannot open this file" if bi is False else "loading...",
                          w // 2 - 40, h // 2)
        else:
            lut = win.display_lut()
            if lut is not None:
                # global brightness: draw into a view-size image, then one
                # lookup table for every channel
                off = BufferedImage(w, h, BufferedImage.TYPE_INT_RGB)
                target = off.createGraphics()
                target.setColor(BG); target.fillRect(0, 0, w, h)
                target.setRenderingHint(RenderingHints.KEY_INTERPOLATION,
                                        RenderingHints.VALUE_INTERPOLATION_BILINEAR)
            else:
                target = g2
            draw_top_center(target, bi, it.tags, win.bi_scale(it, bi, s), w, m)
            if win.ghost.isSelected():
                idx = win.sample_idx()
                k = idx.index(win.cur)
                if k > 0:
                    p = win.items[idx[k - 1]]
                    pb = win.cache_get(p.key)
                    if pb:
                        draw_top_center(target, pb, p.tags, win.bi_scale(p, pb, s), w, m, 0.35)
            if lut is not None:
                target.dispose()
                g2.drawImage(LookupOp(lut, None).filter(off, None), 0, 0, None)
        g2.setFont(Font("SansSerif", Font.BOLD, 15))
        x = 14
        if it.tags.get('flip_h'):
            x = self._badge(g2, "FLIPPED", C_MOD, x)
        r = oc.norm_rot(it.tags.get('rot', 0))
        if r:
            x = self._badge(g2, "R%g" % r, C_MOD, x)
        if it.validated:
            self._badge(g2, "VALIDATED", C_VALID, x)
        g2.dispose()

    def _badge(self, g2, text, col, x):
        fm = g2.getFontMetrics()
        tw = fm.stringWidth(text)
        g2.setColor(col)
        g2.fillRoundRect(x, 12, tw + 14, 24, 8, 8)
        g2.setColor(Color.BLACK)
        g2.drawString(text, x + 7, 30)
        return x + tw + 22


# ==============================================================
# Window
# ==============================================================

class OrienterWindow(JFrame):

    def __init__(self, items, base, mode, root=None, proj=None):
        JFrame.__init__(self, "Section Orienter - " + os.path.basename(base))
        # keep each sample contiguous (first-appearance order), series
        # order inside a sample -> Left/Right walks sample by sample
        first = {}
        for k, it in enumerate(items):
            first.setdefault(it.sample, k)
        items = sorted(items, key=lambda it: first[it.sample])
        self._sync_box = False
        self.items, self.base, self.mode = items, base, mode
        self.root, self.proj = root, proj
        self.cur = 0
        self.anchor = None
        self.sel = None              # (a, b) global index range or None
        self.history = []
        self._angle_run = False    # consecutive 1-degree steps share one undo
        self.dirty = False
        self.kinds = oc.preview_kinds(items, base)
        self.samples = oc.samples_in_order(items)
        self._by_sample = {}
        for i, it in enumerate(items):
            self._by_sample.setdefault(it.sample, []).append(i)
        self._cache, self._meta = {}, {}
        self._sample_of = dict((it.key, it.sample) for it in items)
        self._lock = threading.Condition()
        self._gen = 0
        self._alive = True
        self.keys = dict((a, Prefs.get(PREF + a, d)) for a, _l, d in ACTIONS)
        self._build()
        self._bind_keys()
        self.setDefaultCloseOperation(JFrame.DO_NOTHING_ON_CLOSE)
        win = self
        class WC(WindowAdapter):
            def windowClosing(_s, e): win._close()
        self.addWindowListener(WC())
        self.setSize(1400, 1000)
        self.setLocationRelativeTo(None)
        self._busy = set()           # keys / samples being loaded by a worker
        self._sprog = None           # [sample, progress dialog, {key: bytes left}]
        self._sp_once = False
        for k in range(max(1, min(LOAD_THREADS, Runtime.getRuntime().availableProcessors()))):
            t = threading.Thread(target=self._loader)
            t.setDaemon(True)
            t.start()
        self.setVisible(True)
        self.goto(0)

    # ---------------------------------------------------------- layout
    def _build(self):
        c = self.getContentPane()
        c.setLayout(BorderLayout())
        top = JPanel(BorderLayout())
        top.setBackground(BG)
        self.strip = Strip(self)
        top.add(self.strip, BorderLayout.NORTH)
        self.info = JLabel(" ")
        self.info.setForeground(Color(235, 235, 235))
        self.info.setFont(Font("SansSerif", Font.PLAIN, 15))
        self.info.setBorder(BorderFactory.createEmptyBorder(4, 10, 6, 10))
        self.counts = JLabel(" ")
        self.counts.setForeground(Color(170, 170, 170))
        self.counts.setFont(Font("SansSerif", Font.PLAIN, 13))
        self.counts.setBorder(BorderFactory.createEmptyBorder(4, 10, 6, 12))
        row = JPanel(BorderLayout())
        row.setBackground(BG)
        row.add(self.info, BorderLayout.CENTER)
        row.add(self.counts, BorderLayout.EAST)
        top.add(row, BorderLayout.SOUTH)
        self.view = ImageView(self)
        tools = self._build_tools()          # needs self.view (ghost repaint)
        north = JPanel(BorderLayout())
        north.add(self._build_topbar(), BorderLayout.NORTH)
        north.add(top, BorderLayout.CENTER)
        c.add(north, BorderLayout.NORTH)
        self.view.setFocusable(True)
        view = self.view
        class _TakeFocus(MouseAdapter):
            def mousePressed(_s, e):
                view.requestFocusInWindow()   # leave the angle field
        self.view.addMouseListener(_TakeFocus())
        self.strip.addMouseListener(_TakeFocus())
        c.add(self.view, BorderLayout.CENTER)
        c.add(tools, BorderLayout.SOUTH)

    def _build_topbar(self):
        """Show (left); Project + Files (right), placed and worded as in the
        other two tools."""
        bar = JPanel(BorderLayout())
        left = JPanel(FlowLayout(FlowLayout.LEFT, 4, 2))
        left.add(_group("Show", [JLabel("Sample"), self.sample_box,
                                 JLabel(" Preview"), self.kind_box]))
        bar.add(left, BorderLayout.CENTER)
        right = JPanel(FlowLayout(FlowLayout.RIGHT, 4, 2))
        proj = self.mode == "project"
        self.save_lbl = JLabel(spp.saved_text(
            (self.proj or {}).get("modified") if proj else False))
        self.save_lbl.setForeground(Color(90, 90, 90))
        right.add(_group("Memory", cex.memory_widgets()))
        right.add(_group("Project" if proj else "Tags (no project)", [
            self.save_lbl,
            _btn("Save to project" if proj else "Save tags", lambda: self.do("save"),
                 "Ctrl+S")]))
        right.add(_group("Files", [_btn("Export files...", self.open_apply,
                                        "Write the re-oriented section files")]))
        bar.add(right, BorderLayout.EAST)
        return bar

    def _build_tools(self):
        win = self
        p = JPanel(BorderLayout())
        p.setBorder(BorderFactory.createEmptyBorder(2, 4, 0, 4))
        row1 = JPanel(FlowLayout(FlowLayout.LEFT, 4, 2))

        self.sample_box = JComboBox(self.samples)
        self.sample_box.setFocusable(False)
        self.sample_box.addActionListener(_Act(self._jump_sample))
        self.kind_box = JComboBox(self.kinds or ["-"])
        self.kind_box.setFocusable(False)
        self.kind_box.addActionListener(_Act(self._reset_cache))

        # angle: field + 1-degree up / down buttons (hold to repeat)
        self.angle_field = JTextField("0", 4)
        self.angle_field.setHorizontalAlignment(JTextField.RIGHT)
        self.angle_field.setToolTipText("Rotation in degrees, clockwise; Enter applies")
        self.angle_field.addActionListener(_Act(self.angle_typed))
        arrows = JPanel(GridLayout(2, 1, 0, 0))
        for direction, step in ((SwingConstants.NORTH, 1), (SwingConstants.SOUTH, -1)):
            b = BasicArrowButton(direction)
            b.setFocusable(False)
            b.setPreferredSize(Dimension(16, 11))
            b.setToolTipText("%+d degree (hold to repeat)" % step)
            self._repeat(b, lambda s=step: self.angle_step(s))
            arrows.add(b)
        angle = JPanel(BorderLayout(1, 0))
        angle.add(self.angle_field, BorderLayout.CENTER)
        angle.add(arrows, BorderLayout.EAST)
        row1.add(_group("Orientation (selected)", [
            _btn("Flip", lambda: self.do('flip')),
            _btn("90 CCW", lambda: self.do('rot_ccw')),
            _btn("90 CW", lambda: self.do('rot_cw')),
            JLabel(" Angle"), angle, JLabel("deg "),
            _btn("Reset", self.reset, "Clear flip / rotation"),
            _btn("Undo", lambda: self.do('undo'))]))

        self.ghost = JCheckBox("Ghost previous", False)
        self.ghost.setFocusable(False)
        self.ghost.addActionListener(_Act(self.view.repaint))
        row1.add(_group("Check", [
            _btn("Validate sample", self.validate_sample,
                 "Mark every section of this sample validated"),
            self.ghost]))
        p.add(row1, BorderLayout.CENTER)


        # second row: global display brightness + key hint
        row2 = JPanel(BorderLayout())
        self.black = JSlider(0, 254, 0)
        self.white = JSlider(1, 255, 255)
        class BL(ChangeListener):
            def stateChanged(_s, e):
                win.view.repaint()
        for sl in (self.black, self.white):
            sl.setFocusable(False)   # arrow keys stay with navigation
            sl.setPreferredSize(Dimension(150, 22))
            sl.addChangeListener(BL())
        bright = JPanel(FlowLayout(FlowLayout.LEFT, 4, 0))
        bright.add(_group("Brightness (all images, screen only)", [
            JLabel("black"), self.black, JLabel("white"), self.white,
            _btn("Auto", self.auto_brightness, "Stretch to the current image (0.35% clipped)"),
            _btn("Reset", self.reset_brightness)]))
        row2.add(bright, BorderLayout.WEST)
        self.hint = JLabel(" ")
        self.hint.setForeground(Color(110, 110, 110))
        self.hint.setBorder(BorderFactory.createEmptyBorder(0, 8, 0, 8))
        row2.add(self.hint, BorderLayout.CENTER)
        keys = JPanel(FlowLayout(FlowLayout.RIGHT, 4, 0))
        keys.add(_btn("Keys...", self.edit_keys, "Change the keyboard shortcuts"))
        row2.add(keys, BorderLayout.EAST)
        p.add(row2, BorderLayout.SOUTH)
        return p

    def _repeat(self, button, fn):
        """Run fn on press, then repeatedly while the button is held."""
        t = Timer(60, _Act(fn))
        t.setInitialDelay(400)
        class M(MouseAdapter):
            def mousePressed(_s, e):
                fn(); t.restart()
            def mouseReleased(_s, e):
                t.stop()
            def mouseExited(_s, e):
                t.stop()
        button.addMouseListener(M())

    def _sync_angle(self):
        if self.items and not self.angle_field.isFocusOwner():
            r = oc.norm_rot(self.items[self.cur].tags.get('rot', 0))
            self.angle_field.setText("%g" % r)

    def angle_step(self, d):
        """+/- 1 degree on every target; one undo step per run of clicks."""
        if not self.items:
            return
        if not self._angle_run:
            self._push()
            self._angle_run = True
        for t in self.targets():
            t.tags['rot'] = oc.norm_rot(t.tags.get('rot', 0) + d)
        self._changed()

    def angle_typed(self):
        try:
            a = float(self.angle_field.getText().strip())
        except ValueError:
            self._sync_angle()
            return
        self._push()
        for t in self.targets():
            t.tags['rot'] = oc.norm_rot(a)
        self._changed()
        self.view.requestFocusInWindow()   # arrow keys back to navigation

    # ---------------------------------------------------------- keys
    def _bind_keys(self):
        rp = self.getRootPane()
        im = rp.getInputMap(JComponent.WHEN_IN_FOCUSED_WINDOW)
        am = rp.getActionMap()
        im.clear()
        for a, _l, _d in ACTIONS:
            ks = parse_ks(self.keys.get(a))
            if ks is not None:
                im.put(ks, a)
                am.put(a, _KeyAct(lambda a=a: self.do(a)))
        shown = ('prev', 'next', 'validate', 'flip', 'ang_ccw', 'ang_cw', 'prev_sample', 'next_sample')
        self.hint.setText("   ".join("%s: %s" % (key_text(self.keys.get(a)), l)
                                     for a, l, _d in ACTIONS if a in shown))

    def edit_keys(self):
        dlg = JDialog(self, "Key bindings", True)
        dlg.setSize(460, 420)
        dlg.setLocationRelativeTo(self)
        model = cex._ROModel(["Action", "Key"], 0)
        new = dict(self.keys)
        def fill():
            model.setRowCount(0)
            for a, l, _d in ACTIONS:
                model.addRow([l, key_text(new.get(a))])
        fill()
        t = JTable(model)
        t.setSelectionMode(ListSelectionModel.SINGLE_SELECTION)
        dlg.getContentPane().add(JScrollPane(t), BorderLayout.CENTER)
        msg = JLabel("  Select an action, press Change, then press the new key.")
        def change():
            r = t.getSelectedRow()
            if r < 0:
                return
            a = ACTIONS[r][0]
            cap = JDialog(dlg, "Press a key", True)
            cap.setSize(300, 110)
            cap.setLocationRelativeTo(dlg)
            lbl = JLabel("  Press the new key for: " + ACTIONS[r][1])
            cap.getContentPane().add(lbl)
            class K(KeyAdapter):
                def keyPressed(_s, e):
                    if e.getKeyCode() in (KeyEvent.VK_SHIFT, KeyEvent.VK_CONTROL,
                                          KeyEvent.VK_ALT, KeyEvent.VK_META):
                        return
                    s = ks_string(KeyStroke.getKeyStrokeForEvent(e))
                    for b, _l, _d in ACTIONS:      # a key does one thing
                        if b != a and new.get(b) == s:
                            new[b] = ""
                    new[a] = s
                    cap.dispose()
            cap.setFocusable(True)
            cap.addKeyListener(K())
            cap.setVisible(True)
            fill()
            t.setRowSelectionInterval(r, r)
        def defaults():
            for a, _l, d in ACTIONS:
                new[a] = d
            fill()
        def ok():
            self.keys = new
            for a, _l, _d in ACTIONS:
                Prefs.set(PREF + a, new.get(a) or "")
            Prefs.savePreferences()
            self._bind_keys()
            dlg.dispose()
        row = JPanel(FlowLayout(FlowLayout.RIGHT))
        for label, fn in [("Change...", change), ("Defaults", defaults),
                          ("OK", ok), ("Cancel", dlg.dispose)]:
            row.add(_btn(label, fn))
        south = JPanel(BorderLayout())
        south.add(msg, BorderLayout.NORTH)
        south.add(row, BorderLayout.SOUTH)
        dlg.getContentPane().add(south, BorderLayout.SOUTH)
        dlg.setVisible(True)

    # ---------------------------------------------------------- previews
    # Whole samples are held in memory: when a sample is opened, the
    # header sizes of its files give the enclosing box; every image is
    # loaded reduced by an integer factor so that box is <= SAMPLE_PX
    # wide (fast decoder subsampling). Every sample is preloaded: the
    # current one first (nearest images first), then the following ones,
    # then the earlier ones. If memory runs short, only the current and
    # the next sample are kept from then on.
    def _reset_cache(self):
        with self._lock:
            self._gen += 1
            self._cache, self._meta = {}, {}
            self._lock.notifyAll()
        self.refresh()
        if self.items:                      # another preview type: the sample loads again
            self._close_sample_progress()
            self._sample_progress()

    def cache_get(self, key):
        return self._cache.get(key)

    def _want(self):
        with self._lock:
            self._lock.notifyAll()

    def _kind(self):
        return unicode(self.kind_box.getSelectedItem()) if self.kinds else None

    def _file(self, it, kind):
        return oc.preview_file(it, self.base, kind) if kind else it.files[0]

    def _keep_samples(self):
        s = self.items[self.cur].sample
        k = self.samples.index(s)
        order = [s] + self.samples[k + 1:] + self.samples[:k][::-1]
        if not getattr(self, '_mem_capped', False):
            rt = Runtime.getRuntime()
            used = lambda: rt.totalMemory() - rt.freeMemory()
            if used() > MEM_CAP_FRAC * rt.maxMemory():
                System.gc()                   # count live images only, not garbage
            if used() > MEM_CAP_FRAC * rt.maxMemory():
                self._mem_capped = True
                IJ.log("[Orienter] memory is getting full: keeping only the current "
                       "and the next sample in memory")
        return order[:2] if getattr(self, '_mem_capped', False) else order

    def display_scale(self, bw, bh):
        """Screen px per ORIGINAL px, the same for every image of the
        sample: the box enclosing all (oriented) images fits the view."""
        it = self.items[self.cur]
        meta = self._meta.get(it.sample)
        if meta is None or bw <= 0 or bh <= 0:
            return None
        ew = eh = 1.0
        for i in self._by_sample[it.sample]:
            x = self.items[i]
            w, h = meta['dims'].get(x.key, (1, 1))
            ow, oh = oc.oriented_size(w, h, x.tags)
            ew, eh = max(ew, ow), max(eh, oh)
        return min(bw / ew, bh / eh)

    def bi_scale(self, it, bi, S):
        """Screen px per cached-image px for this item."""
        meta = self._meta.get(it.sample)
        w0 = meta['dims'].get(it.key, (bi.getWidth(), 1))[0] if meta else bi.getWidth()
        return S * float(w0) / max(1, bi.getWidth())

    # ---------------------------------------------------------- brightness
    def display_lut(self):
        """Global display range (black / white points on 0-255), applied
        to every image on screen only. None = identity."""
        b, w = self.black.getValue(), self.white.getValue()
        if b <= 0 and w >= 255:
            return None
        if w <= b:
            w = b + 1
        key = (b, w)
        if getattr(self, '_lut_key', None) != key:
            t = zeros(256, 'b')
            for v in range(256):
                o = int(round((v - b) * 255.0 / (w - b)))
                o = 0 if o < 0 else (255 if o > 255 else o)
                t[v] = o - 256 if o > 127 else o
            self._lut, self._lut_key = ByteLookupTable(0, t), key
        return self._lut

    def auto_brightness(self):
        bi = self.cache_get(self.items[self.cur].key)
        if not bi:
            return
        from ij import ImagePlus
        ip = ImagePlus("", bi).getProcessor().convertToByte(False)
        h = ip.getHistogram()
        total = float(sum(h))
        lo_t, hi_t, run, lo, hi = total * 0.0035, total * 0.9965, 0, 0, 255
        for v in range(256):
            run += h[v]
            if run <= lo_t: lo = v
            if run <= hi_t: hi = v
        self.black.setValue(lo)
        self.white.setValue(max(lo + 1, hi))

    def reset_brightness(self):
        self.black.setValue(0)
        self.white.setValue(255)

    def loaded_in_sample(self):
        idx = self.sample_idx()
        return sum(1 for i in idx if self.items[i].key in self._cache), len(idx)

    def _loader(self):
        while self._alive:
            with self._lock:
                gen, kind = self._gen, self._kind()
                keep = self._keep_samples()
                for key in list(self._cache):
                    if self._sample_of[key] not in keep:
                        del self._cache[key]
                for s in list(self._meta):
                    if s not in keep:
                        del self._meta[s]
                job = None
                for s in keep:
                    if s not in self._meta:
                        if ('meta', s) in self._busy:
                            continue          # another worker reads its sizes
                        job = ('meta', s)
                        break
                    idx = self._by_sample[s]
                    if s == keep[0]:          # nearest first, ahead before behind
                        cur = self.cur
                        idx = sorted(idx, key=lambda i: abs(i - cur) + (0.5 if i < cur else 0))
                    miss = [i for i in idx if self.items[i].key not in self._cache
                            and self.items[i].key not in self._busy]
                    if miss:
                        job = ('img', miss[0])
                        break
                if job is None:
                    self._lock.wait(1.0)
                    continue
                self._busy.add(('meta', job[1]) if job[0] == 'meta' else self.items[job[1]].key)
            try:
                self._run_job(job, gen, kind)
            finally:
                with self._lock:
                    self._busy.discard(('meta', job[1]) if job[0] == 'meta'
                                       else self.items[job[1]].key)
                    self._lock.notifyAll()

    def _run_job(self, job, gen, kind):
            if job[0] == 'meta':
                s = job[1]
                dims = {}
                for i in self._by_sample[s]:
                    it = self.items[i]
                    try:
                        dims[it.key] = oc.image_dims(self._file(it, kind))
                    except Exception as e:
                        IJ.log("[Orienter] size of %s: %s" % (it.label, e))
                        dims[it.key] = (1, 1)
                envw = max([w for w, h in dims.values()] or [1])
                n = max(1, int(math.ceil(envw / float(SAMPLE_PX))))
                with self._lock:
                    if gen == self._gen:
                        self._meta[s] = {'dims': dims, 'n': n}
                on_edt(self.refresh)
                return
            i = job[1]
            it = self.items[i]
            meta = self._meta.get(it.sample)
            if meta is None:
                return
            try:
                bi = oc.load_fast(self._file(it, kind), meta['n'])
            except Exception as e:
                IJ.log("[Orienter] load %s: %s" % (it.label, e))
                bi = False
            with self._lock:
                if gen != self._gen:
                    return
                self._cache[it.key] = bi
            sp = self._sprog
            if sp and it.sample == sp[0]:
                sp[1].eta.add(sp[2].pop(it.key, 0))
                on_edt(self._sample_progress_step)
            if it.sample == self.items[self.cur].sample:
                n, tot = self.loaded_in_sample()
                if abs(i - self.cur) <= 1 or n % 5 == 0 or n == tot:
                    on_edt(self.refresh)

    # ---------------------------------------------------------- sample loading progress
    def _sample_progress(self):
        """Opening a sample that is not in memory yet: a progress window
        (count, bar, time left from the file sizes) until all its images are
        loaded, so the sample then shows complete."""
        s = self.items[self.cur].sample
        n, tot = self.loaded_in_sample()
        sp = self._sprog
        if sp and sp[0] == s:
            return
        self._close_sample_progress()
        if n >= tot:
            return
        kind = self._kind()
        weights = {}
        for i in self._by_sample[s]:
            it = self.items[i]
            if it.key not in self._cache:
                try:
                    weights[it.key] = os.path.getsize(self._file(it, kind))
                except OSError:
                    weights[it.key] = 1
        prog = cex.ProgressDialog(self, u"Loading sample %s" % s, tot)
        prog.eta.set_total(sum(weights.values()))
        self._sprog = [s, prog, weights]
        prog.update(n, u"Loading images %d / %d" % (n, tot))
        prog.setVisible(True)

    def _sample_progress_step(self):
        sp = self._sprog
        if not sp:
            return
        s, prog = sp[0], sp[1]
        if prog.cancelled or self.items[self.cur].sample != s:
            self._close_sample_progress()            # loading goes on in the background
            return
        n, tot = self.loaded_in_sample()
        if n >= tot:
            self._close_sample_progress()
            self.refresh()
        else:
            prog.update(n, u"Loading images %d / %d" % (n, tot))

    def _close_sample_progress(self):
        sp, self._sprog = self._sprog, None
        if sp:
            sp[1].dispose()

    # ---------------------------------------------------------- navigation
    def sample_idx(self):
        if not self.items:
            return []
        return self._by_sample[self.items[self.cur].sample]

    def selection_range(self):
        if self.sel is None:
            return None
        a, b = self.sel
        return set(range(a, b + 1))

    def goto(self, i, keep_sel=False):
        if not (0 <= i < len(self.items)):
            return
        old_sample = self.items[self.cur].sample if self.items else None
        self.cur = i
        self._angle_run = False
        if not keep_sel:
            self.sel, self.anchor = None, None
        s = self.items[i].sample
        if s != old_sample or self.sample_box.getSelectedItem() != s:
            self._sync_box = True
            self.sample_box.setSelectedItem(s)
            self._sync_box = False
        self._want()
        self.refresh()
        if s != old_sample or not self._sp_once:
            self._sp_once = True
            self._sample_progress()

    def _jump_sample(self):
        if getattr(self, '_sync_box', False):
            return
        s = self.sample_box.getSelectedItem()
        if s in self._by_sample and self.items[self.cur].sample != s:
            self.goto(self._by_sample[s][0])

    def click_cell(self, i, shift):
        if shift:
            a = self.anchor if self.anchor is not None else self.cur
            self.sel = (min(a, i), max(a, i))
            self.anchor = a
            self.goto(i, keep_sel=True)
        else:
            self.anchor = i
            self.goto(i)
        self.view.requestFocusInWindow()

    def targets(self):
        r = self.selection_range()
        if r and len(r) > 1:
            return [self.items[i] for i in sorted(r)]
        return [self.items[self.cur]] if self.items else []

    def refresh(self):
        if not self.items:
            return
        it = self.items[self.cur]
        idx = self.sample_idx()
        k = idx.index(self.cur) + 1
        f = oc.preview_file(it, self.base, unicode(self.kind_box.getSelectedItem())) \
            if self.kinds else it.files[0]
        parts = []
        if it.tags.get('flip_h'):
            parts.append("flipped")
        r0 = oc.norm_rot(it.tags.get('rot', 0))
        if r0:
            parts.append("rotated %g&deg;" % r0)
        tags = ", ".join(parts) or "no change"
        r = self.selection_range()
        selt = ("   |   %d selected" % len(r)) if r and len(r) > 1 else ""
        flash = getattr(self, '_flash', None)
        if flash:
            selt += " &nbsp;&nbsp;<span style='color:#ffc83c'>%s</span>" % flash
            self._flash = None
        self.info.setText(
            "<html><b>%s</b> &nbsp;&nbsp; sample <b>%s</b> &nbsp; %d / %d &nbsp;"
            "<span style='color:#999999'>(overall %d / %d)</span> &nbsp;&nbsp; %s &nbsp;&nbsp; %s%s</html>" % (
                os.path.splitext(os.path.basename(f))[0], it.sample, k, len(idx),
                self.cur + 1, len(self.items),
                "<span style='color:#eb9128'>%s</span>" % tags if it.modified() else tags,
                "<span style='color:#3caf5f'>validated</span>" if it.validated
                else "<span style='color:#999999'>not validated</span>", selt))
        sv = sum(1 for i in idx if self.items[i].validated)
        sm = sum(1 for i in idx if self.items[i].modified())
        tv = sum(1 for x in self.items if x.validated)
        nl, nt = self.loaded_in_sample()
        load = ("loading %d/%d   |   " % (nl, nt)) if nl < nt else ""
        self.counts.setText("%sthis sample: validated %d/%d, modified %d   |   all: validated %d/%d" % (
            load, sv, len(idx), sm, tv, len(self.items)))
        self._sync_angle()
        self.strip.repaint()
        self.view.repaint()

    # ---------------------------------------------------------- actions
    def do(self, a):
        if a in ('prev', 'next'):
            # stays inside the sample; Page Up / Down change sample
            idx = self.sample_idx()
            k = idx.index(self.cur) + (1 if a == 'next' else -1)
            if 0 <= k < len(idx):
                self.goto(idx[k])
            else:
                self._flash = ("last" if a == 'next' else "first") + \
                    " section  (%s / %s: other sample)" % (
                        key_text(self.keys.get('prev_sample')),
                        key_text(self.keys.get('next_sample')))
                self.refresh()
        elif a in ('prev_sample', 'next_sample'):
            k = self.samples.index(self.items[self.cur].sample) + \
                (1 if a == 'next_sample' else -1)
            if 0 <= k < len(self.samples):
                self.goto(self._by_sample[self.samples[k]][0])
        elif a == 'validate':
            ts = self.targets()
            v = not all(t.validated for t in ts)
            self._push()
            for t in ts:
                t.validated = v
            self._changed()
        elif a == 'flip':
            self._push()
            for t in self.targets():
                t.tags['flip_h'] = not t.tags.get('flip_h')
            self._changed()
        elif a in ('ang_cw', 'ang_ccw'):            # same as the 1-degree arrow buttons
            self.angle_step(1 if a == 'ang_cw' else -1)
        elif a in ('rot_cw', 'rot_ccw'):
            d = 90 if a == 'rot_cw' else -90
            self._push()
            for t in self.targets():
                t.tags['rot'] = oc.norm_rot(t.tags.get('rot', 0) + d)
            self._changed()
        elif a == 'undo':
            self.undo()
        elif a == 'save':
            self.save()

    def reset(self):
        self._push()
        for t in self.targets():
            t.tags = dict(oc.IDENTITY)
        self._changed()

    def validate_sample(self):
        self._push()
        for i in self.sample_idx():
            self.items[i].validated = True
        self._changed()

    def _push(self):
        self._angle_run = False
        self.history.append([(dict(it.tags), it.validated) for it in self.items])
        del self.history[:-200]

    def undo(self):
        if not self.history:
            return
        self._angle_run = False
        snap = self.history.pop()
        for it, (t, v) in zip(self.items, snap):
            it.tags, it.validated = dict(t), v
        self._changed()

    def _changed(self):
        self.dirty = True
        self.save_lbl.setText(spp.UNSAVED_TEXT)
        self.save_lbl.setForeground(Color(200, 120, 0))
        self.refresh()

    # ---------------------------------------------------------- save / apply
    def save(self):
        try:
            if self.mode == 'project':
                self.proj = oc.save_tags_project(self.root, self.items)
                where = spp.project_path(self.root)
            else:
                oc.save_tags_folder(self.base, self.items)
                where = os.path.join(self.base, oc.TAGS_FILE)
        except Exception as e:
            IJ.log(traceback.format_exc())
            JOptionPane.showMessageDialog(self, "Save failed: %s" % e)
            return False
        self.dirty = False
        self.save_lbl.setText(spp.saved_text())
        self.save_lbl.setForeground(Color(40, 120, 60))
        IJ.log("[Orienter] saved to " + where)
        return True

    def open_apply(self):
        nv = sum(1 for it in self.items if not it.validated)
        if nv and JOptionPane.showConfirmDialog(
                self, "%d of %d section(s) are not validated yet. Continue?" % (nv, len(self.items)),
                "Export files", JOptionPane.OK_CANCEL_OPTION) != JOptionPane.OK_OPTION:
            return
        groups = sorted(oc.item_groups(self.items, self.base))
        # in place only for section folders (never a project root or a plain image folder)
        root = getattr(self, 'root', None)
        root = os.path.abspath(root).lower() if root else None
        can_inplace = all(os.path.exists(os.path.join(b, "sections.csv")) and
                          b.lower() != root for b in groups)
        dlg = JDialog(self, "Export oriented files", False)
        c = dlg.getContentPane()
        c.setLayout(BorderLayout())
        new_cb = JCheckBox("Write all files to a new folder (section files kept)", True)
        new_cb.setEnabled(can_inplace)
        dest = JLabel(" ")
        info = JLabel(" ")
        info.setForeground(Color(200, 60, 60))
        def refresh(e=None):
            outs = u", ".join(os.path.basename(oc.oriented_dir(b)) for b in groups)
            if new_cb.isSelected():
                dest.setText(u"<html>New folder beside the section folder: %s<br>"
                             u"every image is written (re-oriented or copied)</html>" % outs)
            else:
                dest.setText(u"<html>The section folder is renamed to %s and only the "
                             u"re-oriented images are rewritten.<br>Unturned images: "
                             u"export again from step 2.</html>" % outs)
            info.setText(" ")
        new_cb.addActionListener(_Act(refresh))
        refresh()
        body = JPanel()
        body.setLayout(BoxLayout(body, BoxLayout.Y_AXIS))
        body.setBorder(BorderFactory.createEmptyBorder(4, 8, 4, 8))
        r2 = JPanel(FlowLayout(FlowLayout.LEFT)); r2.add(new_cb)
        r2d = JPanel(FlowLayout(FlowLayout.LEFT)); r2d.add(dest)
        body.add(cex._tgroup("Destination", r2, r2d))
        c.add(body, BorderLayout.CENTER)
        def plan():
            new = new_cb.isSelected()
            if not new:
                taken = [oc.oriented_dir(b) for b in groups if os.path.exists(oc.oriented_dir(b))]
                if taken:
                    info.setText(u"%s already exists: rename or delete it first"
                                 % os.path.basename(taken[0]))
                    return None, None
            return new, oc.plan_apply(self.items, self.base, new)
        def preview():
            new, jobs = plan()
            if jobs is not None:
                self._job_table(jobs, new, dlg)
        def run():
            new, jobs = plan()
            if jobs is not None:
                dlg.dispose()
                self._run_apply(jobs, new)
        r3 = JPanel(FlowLayout(FlowLayout.RIGHT))
        r3.add(_btn("Preview job list", preview)); r3.add(_btn("Export", run))
        r3.add(_btn("Close", dlg.dispose))
        south = JPanel(BorderLayout())
        south.setBorder(BorderFactory.createEmptyBorder(0, 12, 6, 6))
        south.add(info, BorderLayout.CENTER)
        south.add(r3, BorderLayout.EAST)
        c.add(south, BorderLayout.SOUTH)
        dlg.pack()
        dlg.setLocationRelativeTo(self)
        dlg.setVisible(True)

    def _job_table(self, jobs, new, parent):
        cols = ['label', 'action', 'tags', 'file', 'status']
        model = cex._ROModel(cols, 0)
        for j in jobs:
            model.addRow([j[k] for k in cols])
        t = JTable(model)
        t.setAutoCreateRowSorter(True)
        for i, w in enumerate([140, 70, 80, 520, 130]):
            t.getColumnModel().getColumn(i).setPreferredWidth(w)
        d = JDialog(parent, "Job list - %d file(s)" % len(jobs), False)
        d.setSize(1000, 600)
        d.setLocationRelativeTo(parent)
        d.getContentPane().add(JScrollPane(t), BorderLayout.CENTER)
        nt = sum(1 for j in jobs if j['action'] != 'copy')
        bottom = JPanel(BorderLayout())
        bottom.add(JLabel("  transform %d   copy %d   overwrite %d" % (
            nt, len(jobs) - nt, sum(1 for j in jobs if j['status'] != 'ok'))), BorderLayout.CENTER)
        def save_csv():
            ch = JFileChooser(self.base)
            if ch.showSaveDialog(d) == JFileChooser.APPROVE_OPTION:
                cex.write_csv(ch.getSelectedFile().getAbsolutePath(), jobs, cols)
        row = JPanel(FlowLayout(FlowLayout.RIGHT))
        row.add(_btn("Save CSV...", save_csv))
        row.add(_btn("Apply these", lambda: (d.dispose(), parent.dispose(), self._run_apply(jobs, new))))
        row.add(_btn("Close", d.dispose))
        bottom.add(row, BorderLayout.EAST)
        d.getContentPane().add(bottom, BorderLayout.SOUTH)
        d.setVisible(True)

    def _run_apply(self, jobs, new):
        groups = oc.item_groups(self.items, self.base)
        outs = [oc.oriented_dir(b) for b in sorted(groups)]
        # ask only when something existing is replaced or renamed
        msg = None
        if new:
            nover = sum(1 for j in jobs if j['status'] != 'ok')
            if nover:
                msg = u"%d existing file(s) in\n%s\nwill be overwritten." % (nover, u"\n".join(outs))
        else:
            msg = u"Rename\n%s\nand rewrite %d re-oriented file(s) there?" % (
                u"\n".join(u"%s -> %s" % (os.path.basename(b), os.path.basename(oc.oriented_dir(b)))
                           for b in sorted(groups)), len(jobs))
        if msg and JOptionPane.showConfirmDialog(self, msg, "Apply",
                                                 JOptionPane.OK_CANCEL_OPTION) != JOptionPane.OK_OPTION:
            return
        if not self.save():
            return
        if not new:
            try:
                oc.rename_for_apply(self.items, self.base)
            except Exception as e:
                JOptionPane.showMessageDialog(self, u"Could not rename the section folder "
                                              u"(a file in it may be open):\n%s" % e,
                                              "Apply", JOptionPane.ERROR_MESSAGE)
                return
        fill = float((self.proj or {}).get('settings', {}).get('fill_value', 0) or 0)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        prog = cex.ProgressDialog(self, "Applying orientation", len(jobs))
        win = self
        def work():
            t0 = time.time()
            done, errors = oc.run_apply(jobs, fill, prog)
            cancelled = prog.cancelled
            try:
                for b in sorted(groups):
                    out = oc.oriented_dir(b)
                    js = [j for j in jobs if j['group'] == b]
                    oc.write_apply_records(out, groups[b], js, sum(1 for j in js if j.get('done')),
                                           errors, stamp, b, cancelled, in_place=not new)
                    if win.mode == 'project':
                        rec = {'stamp': stamp, 'stage': 3, 'folder': spp.rel_path(win.root, out),
                               'source': spp.rel_path(win.root, b), 'files': done,
                               'cancelled': cancelled, 'in_place': not new}
                        spp.update_project(win.root,
                                           lambda p, rec=rec: p.setdefault('exports', []).append(rec))
            except Exception as e:
                errors.append("records: %s" % e)
                IJ.log(traceback.format_exc())
            def fin():
                prog.dispose()
                JOptionPane.showMessageDialog(win, u"%s: %d file(s) in %.0f s%s\n%s%s" % (
                    "Cancelled" if cancelled else "Done", done, time.time() - t0,
                    ("\n%d error(s), see Log" % len(errors)) if errors else "", u"\n".join(outs),
                    u"" if new else u"\n\nThe section folder was renamed: the Orienter "
                                    u"closes (open the oriented folder again if needed)."))
                for e in errors[:50]:
                    IJ.log("[Orienter] " + e)
                if not new:            # its items still point at the old folder
                    win.dirty = False
                    win.dispose()
            on_edt(fin)
        t = threading.Thread(target=work)
        t.setDaemon(True)
        t.start()
        prog.setVisible(True)

    def _close(self):
        if self.dirty:
            r = JOptionPane.showConfirmDialog(self, "Save orientation / validation before closing?",
                                              "Unsaved changes", JOptionPane.YES_NO_CANCEL_OPTION)
            if r in (JOptionPane.CANCEL_OPTION, JOptionPane.CLOSED_OPTION):
                return
            if r == JOptionPane.YES_OPTION and not self.save():
                return
        self._alive = False
        with self._lock:
            self._lock.notifyAll()
        self.dispose()
