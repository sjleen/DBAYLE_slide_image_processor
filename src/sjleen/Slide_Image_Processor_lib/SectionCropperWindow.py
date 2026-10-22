# ============================================================
# SectionCropperWindow.py  (stage 2: Section Cropper)
#
# Main window: slide list (left), slide canvas (center), detection
# settings + section list (right). Owns the in-memory project; the
# canvas edits proj['sections'] through the callbacks below.
#
# Stage-2-owned project fields (written on Save):
#   sections, next_section_id,
#   slides[*].order / sample / excluded / reviewed / roi_orient,
#   settings.detect / padding_frac / fill_value / filename_template
# ============================================================

import os, re, copy, math, threading, traceback
import jarray

from java.awt       import (BorderLayout, FlowLayout, GridLayout, Dimension,
                            GridBagLayout, GridBagConstraints,
                            Color, Font, Insets, BasicStroke, RenderingHints)
from java.awt.event import (ActionListener, WindowAdapter, KeyEvent,
                            InputEvent, FocusAdapter)
from java.lang      import Runnable, Object
from javax.swing    import (JFrame, JPanel, JLabel, JButton, JToggleButton,
                            ButtonGroup, JCheckBox, JComboBox, JTextField,
                            JTable, JScrollPane, JSplitPane, JOptionPane,
                            BorderFactory, Box, BoxLayout, ListSelectionModel,
                            SwingUtilities, KeyStroke, AbstractAction,
                            JComponent)
from javax.swing.table import DefaultTableModel
from javax.swing.event import ListSelectionListener

from ij import IJ

import SectionPipelineProject as spp
import SectionCropperCore as core
import SectionCropperCanvas as canvas_mod
import SectionCropperExport as export_mod
import SectionRename as srn


UNDO_MAX = 60
TOKEN_SPLIT = re.compile(r'[_\-\s]+')


# ==============================================================
# small Swing helpers
# ==============================================================

class _Act(ActionListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, e):
        try:
            self.fn()
        except Exception as ex:
            IJ.log("[Cropper] " + unicode(ex))
            IJ.log(traceback.format_exc())


class _KeyAct(AbstractAction):
    def __init__(self, fn):
        AbstractAction.__init__(self)
        self.fn = fn

    def actionPerformed(self, e):
        try:
            self.fn()
        except Exception as ex:
            IJ.log("[Cropper] " + unicode(ex))


class _Run(Runnable):
    def __init__(self, fn):
        self.fn = fn

    def run(self):
        try:
            self.fn()
        except Exception as ex:
            IJ.log("[Cropper] " + unicode(ex))
            IJ.log(traceback.format_exc())


def on_edt(fn):
    SwingUtilities.invokeLater(_Run(fn))


class _ROModel(DefaultTableModel):
    def isCellEditable(self, r, c):
        return False


def _btn(text, fn, tip=None):
    b = JButton(text)
    b.setMargin(Insets(2, 6, 2, 6))
    b.setFocusable(False)
    if tip:
        b.setToolTipText(tip)
    b.addActionListener(_Act(fn))
    return b


def _row(*comps):
    p = JPanel(FlowLayout(FlowLayout.LEFT, 4, 2))
    for c in comps:
        p.add(c)
    return p


def _group(title, comps, cols=None):
    """Titled box: a row of controls (cols=None) or a button grid."""
    if cols:
        p = JPanel(GridLayout(0, cols, 3, 3))
    else:
        p = JPanel(FlowLayout(FlowLayout.LEFT, 3, 0))
    b = BorderFactory.createTitledBorder(title)
    b.setTitleFont(Font("SansSerif", Font.PLAIN, 11))
    p.setBorder(b)
    for c in comps:
        p.add(c)
    return p


class _Form(object):
    """Titled box on a 5-column grid so labels and fields line up:
        item | sub-label | field | sub-label | field
    e.g. "Size | min [ ] | max [ ]". Single fields in columns 2 and 4 all
    get the same width; wider items (combos, option rows) span to the
    common right edge."""
    COLS = 5
    FIELDS = (2, 4)
    FIELD_W = 90

    def __init__(self, title):
        self.panel = JPanel(GridBagLayout())
        b = BorderFactory.createTitledBorder(title)
        b.setTitleFont(Font("SansSerif", Font.PLAIN, 11))
        self.panel.setBorder(b)
        self.y = 0
        self.colw = [0] * self.COLS    # widest single-column cell per column

    def row(self, *cells):
        """cells: (col, component) pairs in column order; each spans up to
        the next cell (the last one to the end of the row)."""
        cells = list(cells)
        for k, (col, comp) in enumerate(cells):
            end = cells[k + 1][0] if k + 1 < len(cells) else self.COLS
            if isinstance(comp, JTextField):     # text fields never stretch over columns
                end = col + 1
            single_field = (col in self.FIELDS and end - col == 1
                            and not isinstance(comp, JLabel))
            if col == 4 and not isinstance(comp, JLabel):
                single_field = True
            if single_field:
                h = comp.getPreferredSize().height
                comp.setPreferredSize(Dimension(self.FIELD_W, h))
                comp.setMinimumSize(Dimension(self.FIELD_W, h))
            c = GridBagConstraints()
            c.gridx, c.gridy, c.gridwidth = col, self.y, end - col
            c.insets = Insets(2, 3, 2, 3)
            c.anchor = GridBagConstraints.WEST
            if isinstance(comp, JLabel) and col > 0:
                c.fill = GridBagConstraints.NONE
            elif col > 0 or end - col > 1:
                c.fill = GridBagConstraints.HORIZONTAL
                # only the single fields take extra width, equally
                c.weightx = 1.0 if single_field else 0.0
            if end - col == 1:
                self.colw[col] = max(self.colw[col], comp.getPreferredSize().width + 6)
            self.panel.add(comp, c)
        self.y += 1
        return self


def _align_forms(*forms):
    """Give several _Form boxes the same column widths (same panel width
    -> their columns line up across boxes)."""
    w = [max(f.colw[i] for f in forms) for i in range(_Form.COLS)]
    for f in forms:
        f.panel.getLayout().columnWidths = jarray.array(w, "i")


def _hbox(*comps):
    """Components side by side with no extra gaps (checkbox lists)."""
    p = JPanel(FlowLayout(FlowLayout.LEFT, 0, 0))
    for c in comps:
        p.add(c)
    return p


def _fill_row(*comps):
    """Buttons sharing the full width equally."""
    p = JPanel(GridLayout(1, len(comps), 4, 0))
    for c in comps:
        p.add(c)
    return p


def _stack(*panels):
    p = JPanel()
    p.setLayout(BoxLayout(p, BoxLayout.Y_AXIS))
    for x in panels:
        p.add(x)
    return p


def _num(field, default, cast=float):
    try:
        t = field.getText().strip()
        return cast(t) if t != "" else default
    except Exception:
        return default


# ==============================================================
# Main window
# ==============================================================

class Histogram(JPanel):
    """Distribution of size or circularity over all slides: every object
    of a slide's last detection (kept blue, rejected gray), otherwise its
    sections; ticks = current slide, orange line = selected section(s),
    red lines = detection min / max."""
    BINS = 24

    def __init__(self, win, kind):
        JPanel.__init__(self)
        self.win, self.kind = win, kind
        self.setPreferredSize(Dimension(330, 60))
        self.setBackground(Color(250, 250, 250))

    def _limit(self, field):
        t = field.getText().strip()
        try:
            return float(t) if t else None
        except ValueError:
            return None

    def paintComponent(self, g):
        super(Histogram, self).paintComponent(g)
        win = self.win
        if not hasattr(win, 'det_cmin'):
            return
        g2 = g.create()
        g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON)
        w, h = self.getWidth(), self.getHeight()
        k = 0 if self.kind == 'size' else 1
        m = win.metrics()
        secs = win.proj['sections']
        # spread: every object of the last detection of a slide (kept or
        # rejected, so the limits can be chosen from it); slides not detected
        # in this session contribute their sections
        d = win.proj['settings']['detect']
        allv, rejv = [], []
        done = set()
        for sid, c in win.cands.items():
            if c['unit'] != d.get('size_unit') or (k == 1 and c['con'] != d.get('circ_on')):
                continue
            done.add(sid)
            for item in c['items']:
                v = item[k]
                if v is not None:
                    (allv if item[2] else rejv).append(v)
        allv += [m[i][k] for i in m if m[i][k] is not None and not secs[i].get('excluded')
                 and secs[i]['slide'] not in done]
        cur = [m[i][k] for i in spp.slide_section_ids(win.proj, win.sid)
               if i in m and m[i][k] is not None] if win.sid else []
        sel = [m[i][k] for i in win.canvas.selected if i in m and m[i][k] is not None]
        if self.kind == 'size':
            unit = str(win.det_unit.getSelectedItem())
            title = "Size (%s)" % unit
            lo, hi = 0.0, (100.0 if unit.startswith('%') else max(allv + rejv or [1.0]) * 1.05)
            lims = (self._limit(win.det_smin), self._limit(win.det_smax))
        else:
            title = "Circularity (on %s)" % win.det_con.getSelectedItem()
            lo, hi = 0.0, 1.0
            lims = (self._limit(win.det_cmin), self._limit(win.det_cmax))
        L, R, T, B = 12, 14, 14, 18
        pw, ph = w - L - R, h - T - B
        X = lambda v: L + int(pw * (min(max(v, lo), hi) - lo) / float(hi - lo or 1))
        def binned(vals):
            cs = [0] * self.BINS
            for v in vals:
                b = int((v - lo) / float(hi - lo or 1) * self.BINS)
                cs[max(0, min(self.BINS - 1, b))] += 1
            return cs
        counts, rcounts = binned(allv), binned(rejv)
        # scale to the kept objects (hundreds of rejected specks would flatten
        # them); taller gray bars are clipped
        cmax = float(max(counts) * 2 if any(counts) else max(rcounts or [1]) or 1)
        bw = pw / float(self.BINS)
        for b, (c, r) in enumerate(zip(counts, rcounts)):
            ch = int(ph * c / cmax)
            rh = min(ph - ch, int(ph * r / cmax))
            if c:
                g2.setColor(Color(120, 150, 200))          # kept
                g2.fillRect(int(L + b * bw), T + ph - ch, max(1, int(bw) - 1), ch)
            if r:
                g2.setColor(Color(200, 200, 200))          # rejected by the limits
                g2.fillRect(int(L + b * bw), T + ph - ch - rh, max(1, int(bw) - 1), rh)
        g2.setColor(Color(150, 150, 150))
        g2.drawLine(L, T + ph, L + pw, T + ph)
        # x axis: 5 major intervals (labelled), one minor tick between
        g2.setFont(Font("SansSerif", Font.PLAIN, 10))
        fm = g2.getFontMetrics()
        for k in range(11):
            v = lo + (hi - lo) * k / 10.0
            x = X(v)
            if k % 2 == 0:
                g2.drawLine(x, T + ph, x, T + ph + 4)
                s = "%.3g" % v
                g2.drawString(s, x - fm.stringWidth(s) // 2, h - 2)
            else:
                g2.drawLine(x, T + ph, x, T + ph + 2)
        g2.setColor(Color(40, 40, 40))
        for v in cur:                       # this slide: ticks above the axis
            x = X(v)
            g2.drawLine(x, T + ph - 6, x, T + ph - 1)
        g2.setColor(Color(240, 120, 60))
        g2.setStroke(BasicStroke(2.0))
        for v in sel:
            x = X(v)
            g2.drawLine(x, T, x, T + ph + 8)
        g2.setColor(Color(210, 40, 40))
        g2.setStroke(BasicStroke(1.2))
        for v in lims:
            if v is not None and lo <= v <= hi:
                x = X(v)
                g2.drawLine(x, T - 2, x, T + ph)
        g2.setColor(Color(60, 60, 60))
        g2.setFont(Font("SansSerif", Font.PLAIN, 10))
        g2.drawString("%s   n=%d" % (title, len(allv)) +
                      ("  (+%d rejected, gray)" % len(rejv) if rejv else ""), L, 11)
        g2.dispose()


class CropperWindow(JFrame):

    def __init__(self, root, proj, manifest, ws):
        JFrame.__init__(self, "Section Cropper - " + os.path.basename(root))
        self.root, self.proj, self.manifest, self.ws = root, proj, manifest, ws
        st = proj['settings']
        st['detect'] = core.normalize_detect(st.get('detect'))
        self.sources = {}
        self.masks   = {}
        self.combs   = {}          # sid -> (detection image, f, threshold)
        self.pxp     = {}          # sid -> detection params with lengths in detection px
        self.raws    = {}          # sid -> (signal mask before closing, f)
        self.cands   = {}          # sid -> objects of its last detection (histograms)
        self._fcache = {}
        self.undo    = {}
        self.redo    = {}
        self.sid     = None
        self.numbers = {}
        self.order_clicked = []
        self.exposure_version = 0
        self.dirty   = False
        self._sync_guard = False
        self._reconcile_orientation()
        self._build()
        self._renumber()
        self._refresh_slides()
        ids = self._slide_ids_all()
        if ids:
            self.show_slide(ids[0])
        self.setDefaultCloseOperation(JFrame.DO_NOTHING_ON_CLOSE)
        win = self
        class WC(WindowAdapter):
            def windowClosing(_s, e):
                win._close()
        self.addWindowListener(WC())
        self.setSize(1600, 950)
        self.setLocationRelativeTo(None)
        self.setVisible(True)
        SwingUtilities.invokeLater(_Run(self.canvas.fit))

    # ---------------------------------------------------------- build
    def _build(self):
        c = self.getContentPane()
        c.setLayout(BorderLayout())
        c.add(self._build_toolbar(), BorderLayout.NORTH)
        self.canvas = canvas_mod.SlideCanvas(self)
        center = JSplitPane(JSplitPane.HORIZONTAL_SPLIT,
                            self._build_slide_panel(), self.canvas)
        center.setDividerLocation(330)
        main = JSplitPane(JSplitPane.HORIZONTAL_SPLIT, center,
                          self._build_right_panel())
        main.setResizeWeight(1.0)
        main.setDividerLocation(1155)
        c.add(main, BorderLayout.CENTER)
        self.status = JLabel(" ")
        self.coords = JLabel(" ")
        sb = JPanel(BorderLayout())
        sb.setBorder(BorderFactory.createEmptyBorder(2, 8, 2, 8))
        sb.add(self.status, BorderLayout.CENTER)
        sb.add(self.coords, BorderLayout.EAST)
        c.add(sb, BorderLayout.SOUTH)
        self._install_keys()

    def _build_toolbar(self):
        bar = JPanel(FlowLayout(FlowLayout.LEFT, 6, 2))
        self.mode_buttons = {}
        grp = ButtonGroup()
        tools = []
        for mode, label, tip in [
                ('edit', "Edit", "Select / move / reshape (Esc)"),
                ('draw', "Draw", "Click points, double-click or Enter to close"),
                ('wand', "Wand add", "Click inside tissue to add a section"),
                ('split', "Split", "Click points of a cut line, double-click = cut"),
                ('order', "Click order", "Click sections in order, Enter = done")]:
            b = JToggleButton(label, mode == 'edit')
            b.setToolTipText(tip)
            b.setFocusable(False)
            b.addActionListener(_Act(lambda m=mode: self.canvas.set_mode(m)))
            grp.add(b)
            tools.append(b)
            self.mode_buttons[mode] = b
        sep = JLabel(" | ")
        sep.setForeground(Color(150, 150, 150))
        bar.add(_group("Tool", tools + [sep,
                    _btn("Merge", self.merge_selected, "Merge the selected sections into one"),
                    _btn("Delete", self.delete_selected, "Delete the selected sections (Delete key)")]))
        bar.add(_group("History", [_btn("Undo", self.do_undo, "Ctrl+Z"),
                                   _btn("Redo", self.do_redo, "Ctrl+Y")]))
        bar.add(_group("Slide", [_btn("<", lambda: self.step_slide(-1), "Previous slide (Page Up)"),
                                 _btn(">", lambda: self.step_slide(1), "Next slide (Page Down)"),
                                 _btn("Fit", lambda: self.canvas.fit(), "Fit to window (F)")]))
        view = []
        self.chan_boxes = {}
        for ch in self._all_channels():
            cb = JCheckBox(ch, True)
            cb.setFocusable(False)
            cb.addActionListener(_Act(self.canvas_image_changed))
            self.chan_boxes[ch] = cb
            view.append(cb)
        self.mask_box = JCheckBox("Mask", False)
        self.mask_box.setFocusable(False)
        self.mask_box.setToolTipText("Overlay the detection mask")
        self.mask_box.addActionListener(_Act(self._toggle_mask))
        self.pad_box = JCheckBox("Padding", True)
        self.pad_box.setFocusable(False)
        self.pad_box.setToolTipText("Show the padded crop outline (dashed)")
        self.pad_box.addActionListener(_Act(self._toggle_pad))
        bar.add(_group("View", view + [self.mask_box, self.pad_box]))
        # same place and wording in all three tools: top right,
        # "Project" = save state + Save to project, "Files" = Export files...
        self.save_label = JLabel(spp.saved_text(
            self.proj.get("modified") if os.path.exists(spp.project_path(self.root)) else False))
        self.save_label.setForeground(Color(90, 90, 90))
        right = JPanel(FlowLayout(FlowLayout.RIGHT, 6, 2))
        right.add(_group("Memory", export_mod.memory_widgets()))
        right.add(_group("Project", [self.save_label,
                                     _btn("Save to project", self.save, "Ctrl+S")]))
        right.add(_group("Files", [_btn("Export files...", self.open_export,
                                        "Write section image files (preview the job list first)"),
                                   _btn("Update names...", self.update_names,
                                        "Rename exported files after sample names or the order "
                                        "changed (no new crops)")]))
        outer = JPanel(BorderLayout())
        outer.add(bar, BorderLayout.CENTER)
        outer.add(right, BorderLayout.EAST)
        return outer

    def _build_slide_panel(self):
        p = JPanel(BorderLayout())
        p.setBorder(BorderFactory.createTitledBorder("Slides"))
        self.slide_model = _ROModel(["#", "Slide", "Sample", "Sec", "Status", "Orient"], 0)
        t = JTable(self.slide_model)
        t.setSelectionMode(ListSelectionModel.MULTIPLE_INTERVAL_SELECTION)
        cm = t.getColumnModel()
        for i, w in enumerate([28, 120, 70, 34, 60, 46]):
            cm.getColumn(i).setPreferredWidth(w)
        win = self
        class SL(ListSelectionListener):
            def valueChanged(_s, e):
                if e.getValueIsAdjusting() or win._sync_guard:
                    return
                r = t.getSelectionModel().getLeadSelectionIndex()
                if 0 <= r < t.getRowCount() and t.isRowSelected(r):
                    sid = win._slide_row_ids[r]
                    if sid != win.sid:
                        win.show_slide(sid, from_table=True)
        t.getSelectionModel().addListSelectionListener(SL())
        self.slide_table = t
        p.add(JScrollPane(t), BorderLayout.CENTER)
        p.add(_stack(
            _group("Order (selected slides)", [
                _btn("Move up", lambda: self.move_slides(-1)),
                _btn("Move down", lambda: self.move_slides(1)),
                _btn("Select all", lambda: self.slide_table.selectAll())], 3),
            _group("Sample (selected slides)", [
                _btn("Set...", self.set_slide_sample, "Same sample for all selected"),
                _btn("From name...", self.sample_from_name,
                     "Take filename tokens of each selected slide")], 2),
            _group("Status (selected slides)", [
                _btn("Reviewed on/off", self.toggle_reviewed),
                _btn("Exclude on/off", self.toggle_slide_excluded)], 2)),
            BorderLayout.SOUTH)
        p.setPreferredSize(Dimension(330, 400))
        return p

    def _build_right_panel(self):
        p = JPanel(BorderLayout())
        p.setPreferredSize(Dimension(435, 400))
        det, pad = self._build_detect_panel(), self._build_pad_panel()
        _align_forms(self._det_form, self._pad_form)
        self._update_px_row()
        p.add(_stack(det, pad),
              BorderLayout.NORTH)
        p.add(self._build_section_panel(), BorderLayout.CENTER)
        return p

    def _build_detect_panel(self):
        d = core.normalize_detect(self.proj['settings']['detect'])
        f = _Form("Detection")
        sub = lambda t: JLabel(t)
        self.det_chan = {}
        chans = []
        for ch in self._all_channels():
            cb = JCheckBox(ch, d.get('channels') is None or ch in d['channels'])
            self.det_chan[ch] = cb
            chans.append(cb)
        f.row((0, JLabel("Channels")), (1, _hbox(*chans)))
        self.det_signal = JComboBox(["texture", "intensity"])
        self.det_signal.setSelectedIndex(core.SIGNALS.index(d.get('signal', core.SIGNALS[0])))
        self.det_signal.setToolTipText("<html>texture: local graininess of the raw pixels (nuclei, cells); "
                                       "uneven tiles of a scan do not matter.<br>intensity: brightness "
                                       "after the step-1 exposure. With all channels checked, texture "
                                       "uses the channels that show tissue over a large area.</html>")
        self.det_tissue = JComboBox(["islands", "threshold"])
        self.det_tissue.setSelectedIndex(core.TISSUE_MODES.index(d.get('tissue', core.TISSUE_MODES[0])))
        self.det_tissue.setToolTipText("<html>islands: the smooth background reaching the image border is "
                                       "found; everything it does not reach is tissue (dim or patchy<br>"
                                       "tissue stays whole). threshold: the above-threshold area with "
                                       "holes filled.</html>")
        f.row((0, JLabel("Signal")), (1, sub("use")), (2, self.det_signal),
              (3, sub("tissue")), (4, self.det_tissue))
        self.det_method = JComboBox(core.THRESHOLD_METHODS)
        self.det_method.setSelectedItem(d['method'])
        self.det_manual = JTextField("" if d.get('manual') in (None, "") else str(d['manual']), 4)
        self.det_manual.setToolTipText("Fixed threshold value; empty = use the method")
        self.det_invert = JCheckBox("Invert", bool(d.get('invert')))
        self.det_invert.setToolTipText("Intensity signal only: tissue darker than background (bright-field)")
        f.row((0, JLabel("Threshold")), (1, sub("method")), (2, self.det_method),
              (3, sub("manual")), (4, self.det_manual))
        # blur / close / depth: real length (default; from the pixel size) or detection px
        UM = unichr(181) + u"m"                  # micrometre sign; the source stays ASCII
        self.det_len = JComboBox([UM, "px"])
        self.det_len.setSelectedIndex(0 if d.get('len_unit') == 'um' else 1)
        self.det_len.setToolTipText(u"<html>" + UM + u": lengths in the tissue, the same whatever the "
                                    u"objective or the scanned area<br>(pixel size from the project "
                                    u"setting, else the image file). px: pixels of the detection "
                                    u"image.</html>")
        self._len_idx = self.det_len.getSelectedIndex()
        self.det_len.addActionListener(_Act(self._switch_len_unit))
        f.row((0, JLabel("Lengths")), (1, sub("unit")), (2, self.det_len))
        # pixel size: shown only in um mode when the image files carry none
        cur = self.proj['settings'].get('pixel_size_um')
        self.det_px = JTextField("" if not cur else "%g" % float(cur), 4)
        self.det_px.setToolTipText(u"Pixel size of the full-resolution images, " + UM +
                                   u" per pixel (square pixels). Also used as export calibration.")
        self.det_px.addActionListener(_Act(self._set_pixel_size))
        win = self
        class PxFL(FocusAdapter):
            def focusLost(_s, e):
                win._set_pixel_size()
        self.det_px.addFocusListener(PxFL())
        self._px_row = [sub("pixel size"), self.det_px, sub(UM + "/px (square)")]
        f.row((1, self._px_row[0]), (2, self._px_row[1]), (3, self._px_row[2]))
        self.det_blur = JTextField("%g" % float(d['blur']), 4)
        self.det_close = JTextField("%g" % float(d['close']), 4)
        self.det_blur.setToolTipText("Gaussian blur sigma before thresholding")
        self.det_close.setToolTipText("Morphological closing radius after thresholding (seals tissue edges)")
        f.row((0, JLabel("Smoothing")), (1, sub("blur")), (2, self.det_blur),
              (3, sub("close")), (4, self.det_close))
        self.det_sep = JTextField("" if d.get('separate') in (None, "") else ("%g" % float(d['separate'])), 4)
        self.det_sep.setToolTipText("<html>Split sections that touch: cut where the shape narrows by at least "
                                    "this much (Lengths unit)<br>(a section's own lobes are not cut). "
                                    "0 or empty = off.</html>")
        self.det_border = JCheckBox("Skip border objects", bool(d.get('border', True)))
        self.det_border.setToolTipText("Drop objects touching the image border (slide label, slide edge)")
        sz = d.get('size_split')
        self.det_mult = JTextField("" if sz in (None, "") else "%g" % float(sz), 4)
        self.det_mult.setToolTipText("<html>Sections pressed together along long edges: a blob this many "
                                     "times larger than the typical section<br>on the slide is split "
                                     "into that many sections (by size). 0 = off. Needs split depth.</html>")
        f.row((0, JLabel("Touching")), (1, sub("depth")), (2, self.det_sep),
              (3, sub("size x")), (4, self.det_mult))
        f.row((1, _hbox(self.det_border, self.det_invert)))
        # ROI filters: size in a chosen unit, circularity (0..1)
        fmt = lambda v: "" if v in (None, "") else ("%g" % float(v))
        self.det_smin = JTextField(fmt(d.get('size_min')), 4)
        self.det_smax = JTextField(fmt(d.get('size_max')), 4)
        self.det_smin.setToolTipText("Empty = no lower limit")
        self.det_smax.setToolTipText("Empty = no upper limit (e.g. to drop sections stuck together)")
        self.det_unit = JComboBox([lbl for _k, lbl in core.SIZE_UNITS])
        self.det_unit.setSelectedIndex([k for k, _l in core.SIZE_UNITS].index(d.get('size_unit', 'pct')))
        self.det_unit.addActionListener(_Act(self._unit_changed))
        self._unit_prev = d.get('size_unit', 'pct')
        f.row((0, JLabel("Size")), (1, sub("min")), (2, self.det_smin),
              (3, sub("max")), (4, self.det_smax))
        f.row((1, sub("unit")), (2, self.det_unit))
        self.det_cmin = JTextField(fmt(d.get('circ_min')), 4)
        self.det_cmax = JTextField(fmt(d.get('circ_max')), 4)
        for x in (self.det_cmin, self.det_cmax):
            x.setToolTipText("Circularity = 4*pi*area/perimeter^2 (1 = circle), as in Analyze Particles")
        self.det_con = JComboBox(["section + padding", "section"])
        self.det_con.setSelectedIndex(0 if d.get('circ_on', 'padded') == 'padded' else 1)
        self.det_con.setToolTipText("Measure circularity on the detected outline, or on the "
                                    "outline grown by the default padding (rounder)")
        f.row((0, JLabel("Circularity")), (1, sub("min")), (2, self.det_cmin),
              (3, sub("max")), (4, self.det_cmax))
        f.row((1, sub("on")), (2, self.det_con))
        self.det_fill = JTextField(fmt(d.get('fill_min')), 4)
        self.det_fill.setToolTipText("Minimum share of the ROI that is real (above-threshold) signal; "
                                     "drops big ROIs made of scattered specks. Empty = off")
        f.row((0, JLabel("Signal fill")), (1, sub("min")), (2, self.det_fill),
              (3, sub("% of ROI")))
        # limits / measure choice -> table + histogram lines follow
        def limits_changed():
            self.detect_params()
            self._refresh_sections()
        self.det_con.addActionListener(_Act(limits_changed))
        for x in (self.det_smin, self.det_smax, self.det_cmin, self.det_cmax):
            x.addActionListener(_Act(limits_changed))
            class LF(FocusAdapter):
                def focusLost(_s, e):
                    limits_changed()
            x.addFocusListener(LF())
        self.det_outline = JComboBox(["Smooth", "Hull"])
        self.det_outline.setSelectedItem(d['outline'])
        self.det_simplify = JTextField(str(d['simplify']), 4)
        self.det_simplify.setToolTipText("Polygon simplification tolerance (px)")
        self.det_rtl = JCheckBox("Right to left", bool(d.get('right_to_left')))
        self.det_rtl.setToolTipText("Auto order runs right to left within each row")
        f.row((0, JLabel("Outline")), (1, sub("shape")), (2, self.det_outline),
              (3, sub("simplify")), (4, self.det_simplify))
        f.row((0, _fill_row(_btn("Detect this slide", self.detect_current),
                            _btn("Detect selected slides", self.detect_selected,
                                 "All slides selected in the slide list"))))
        self.det_info = JLabel(" ")
        f.row((0, self.det_info))
        self._det_form = f
        return f.panel

    def _build_pad_panel(self):
        st = self.proj['settings']
        self.pad_frac = JTextField(str(st['padding_frac'] * 100), 4)
        self.fill_val = JTextField(str(st['fill_value']), 4)
        self.pad_frac.setToolTipText("% of each section's bounding-box diagonal "
                                     "(sections without their own padding)")
        self.fill_val.setToolTipText("Pixel value written outside the padded polygon "
                                     "when export 'Fill outside' is on")
        win = self
        for x in (self.pad_frac, self.fill_val):
            x.addActionListener(_Act(self._apply_pad_settings))
            class FL(FocusAdapter):
                def focusLost(_s, e):
                    win._apply_pad_settings()
            x.addFocusListener(FL())
        self.pad_mode = JComboBox(["Convex (no concave parts)", "Follow outline"])
        self.pad_mode.setSelectedIndex(0 if spp.padding_mode(self.proj) == 'hull' else 1)
        self.pad_mode.setToolTipText("Shape of the padded crop outline")
        self.pad_mode.addActionListener(_Act(self._apply_pad_settings))
        self.pad_overlap = JCheckBox("Allow overlap", bool(st.get('padding_overlap')))
        self.pad_overlap.setToolTipText("Off: touching sections split the gap (each padding "
                                        "stops halfway). On: full padding for every section, "
                                        "crops may include part of a neighbour")
        self.pad_overlap.addActionListener(_Act(self._apply_pad_settings))
        f = _Form("Crop (all sections)")
        f.row((0, JLabel("Padding")), (1, JLabel("shape")), (2, self.pad_mode))
        f.row((1, JLabel("size")), (2, self.pad_frac), (3, JLabel("% of diagonal")))
        f.row((1, JLabel("neighbours")), (2, self.pad_overlap))
        f.row((0, JLabel("Outside")), (1, JLabel("fill value")), (2, self.fill_val))
        self._pad_form = f
        return f.panel

    def _build_section_panel(self):
        p = JPanel(BorderLayout())
        p.setBorder(BorderFactory.createTitledBorder("Sections on this slide"))
        self.sec_model = _ROModel(["No", "#", "Sample", "Size", "Circ", "Fill %", "Origin", "Excl"], 0)
        t = JTable(self.sec_model)
        t.setSelectionMode(ListSelectionModel.MULTIPLE_INTERVAL_SELECTION)
        cm = t.getColumnModel()
        for i, w in enumerate([36, 24, 60, 56, 36, 40, 50, 30]):
            cm.getColumn(i).setPreferredWidth(w)
        win = self
        class SL(ListSelectionListener):
            def valueChanged(_s, e):
                if e.getValueIsAdjusting() or win._sync_guard:
                    return
                ids = [win._sec_row_ids[r] for r in t.getSelectedRows()]
                win.canvas.set_selection(ids)
                win.hist_size.repaint(); win.hist_circ.repaint()
        t.getSelectionModel().addListSelectionListener(SL())
        self.sec_table = t
        p.add(JScrollPane(t), BorderLayout.CENTER)
        self.hist_size = Histogram(self, 'size')
        self.hist_circ = Histogram(self, 'circ')
        p.add(_stack(
            _group("Size / circularity (all slides; ticks = this slide)",
                   [self.hist_size, self.hist_circ], 1),
            _group("Order (this slide)", [
                _btn("Move up", lambda: self.move_sections(-1), "Selected sections up"),
                _btn("Move down", lambda: self.move_sections(1), "Selected sections down"),
                _btn("Auto order", self.auto_order_current,
                     "Rows top to bottom, then left to right"),
                self.det_rtl,
                _btn("Mark slide reviewed", lambda: self.toggle_reviewed([self.sid], True))], 3),
            _group("Properties (selected sections)", [
                _btn("Sample...", self.set_section_sample, "Empty = use the slide's sample"),
                _btn("Padding...", self.set_section_padding, "Empty = auto"),
                _btn("Exclude on/off", self.toggle_section_excluded)], 3)),
            BorderLayout.SOUTH)
        return p

    def _install_keys(self):
        rp = self.getRootPane()
        im = rp.getInputMap(JComponent.WHEN_IN_FOCUSED_WINDOW)
        am = rp.getActionMap()
        for name, ks, fn in [
                ("undo", KeyStroke.getKeyStroke(KeyEvent.VK_Z, InputEvent.CTRL_DOWN_MASK), self.do_undo),
                ("redo", KeyStroke.getKeyStroke(KeyEvent.VK_Y, InputEvent.CTRL_DOWN_MASK), self.do_redo),
                ("redo2", KeyStroke.getKeyStroke(KeyEvent.VK_Z, InputEvent.CTRL_DOWN_MASK | InputEvent.SHIFT_DOWN_MASK), self.do_redo),
                ("save", KeyStroke.getKeyStroke(KeyEvent.VK_S, InputEvent.CTRL_DOWN_MASK), self.save),
                ("next", KeyStroke.getKeyStroke(KeyEvent.VK_PAGE_DOWN, 0), lambda: self.step_slide(1)),
                ("prev", KeyStroke.getKeyStroke(KeyEvent.VK_PAGE_UP, 0), lambda: self.step_slide(-1))]:
            im.put(ks, name)
            am.put(name, _KeyAct(fn))

    # ---------------------------------------------------------- data
    def _all_channels(self):
        chs = set()
        for s in self.proj['slides'].values():
            if not s.get('missing'):
                chs.update(s['channels'].keys())
        return sorted(chs)

    def _slide_ids_all(self):
        return spp.ordered_slide_ids(self.proj, include_excluded=True)

    def source(self, sid):
        s = self.sources.get(sid)
        if s is None:
            s = core.SlideSource(self.root, self.proj, sid, self.manifest, self.ws)
            self.sources[sid] = s
        return s

    def _reconcile_orientation(self):
        """Sections are stored in view coords of the orientation they
        were drawn under (roi_orient). If stage 1 changed the slide
        rotation since, map them to the current orientation."""
        n = 0
        for sid, s in self.proj['slides'].items():
            cur = [int(s.get('rotation', 0)), bool(s.get('flip_h', False))]
            ro = s.get('roi_orient')
            ids = spp.slide_section_ids(self.proj, sid)
            if ro is not None and list(ro) != cur and ids and s.get('width'):
                for i in ids:
                    sec = self.proj['sections'][i]
                    sec['polygon'] = spp.reorient_polygon(
                        sec['polygon'], s['width'], s['height'], ro, cur)
                n += 1
            s['roi_orient'] = cur
        if n:
            IJ.log("[Cropper] %d slide(s): sections re-mapped to the new "
                   "stage-1 orientation" % n)
            self.dirty = True

    def _renumber(self):
        self.numbers = dict((r['sec_id'], r['NNN'])
                            for r in spp.numbered_sections(self.proj))

    def visible_channels(self):
        return [ch for ch, cb in self.chan_boxes.items() if cb.isSelected()
                and (self.sid is None or ch in self.proj['slides'][self.sid]['channels'])]

    def get_mask(self):
        return self.masks.get(self.sid)

    PIXEL_PRESETS = [("ECHO 4x", 0.8854), ("ECHO 10x", 0.3541578), ("ECHO 20x", 0.1770)]

    def ask_pixel_size(self):
        """Pixel size (um/px) of the full-resolution slide. Stored in the
        project (also used as export calibration)."""
        cur = self.proj['settings'].get('pixel_size_um')
        opts = ["%s  (%g um/px)" % (n, v) for n, v in self.PIXEL_PRESETS] + ["Other..."]
        ch = JOptionPane.showInputDialog(
            self, "Pixel size of the full-resolution images%s:" %
            ((" (now %g um/px)" % cur) if cur else ""), "Pixel size",
            JOptionPane.QUESTION_MESSAGE, None, opts, opts[1])
        if ch is None:
            return None
        k = opts.index(ch)
        if k < len(self.PIXEL_PRESETS):
            v = self.PIXEL_PRESETS[k][1]
        else:
            s = JOptionPane.showInputDialog(self, "Pixel size in um/px:", str(cur or ""))
            try:
                v = float(s)
            except (TypeError, ValueError):
                return None
            if v <= 0:
                return None
        self.det_px.setText("%g" % v)          # same setting as Detection > pixel size
        self._set_pixel_size()
        return v

    def _unit_changed(self):
        unit = core.SIZE_UNITS[self.det_unit.getSelectedIndex()][0]
        if unit == self._unit_prev:
            return
        if unit == 'mm2' and not self.proj['settings'].get('pixel_size_um'):
            if self.ask_pixel_size() is None:
                self.det_unit.setSelectedIndex(
                    [k for k, _l in core.SIZE_UNITS].index(self._unit_prev))
                return
        # values mean something else in the new unit
        self.det_smin.setText("")
        self.det_smax.setText("")
        self._unit_prev = unit
        self.detect_params()
        self._refresh_sections()
        self.set_status("Size unit: %s - enter the limits again (Log lists sizes after "
                        "a detection)" % core.SIZE_UNITS[self.det_unit.getSelectedIndex()][1])

    def _switch_len_unit(self):
        """Lengths unit changed: convert the three length fields so they keep
        their meaning (scale of the current slide)."""
        new = self.det_len.getSelectedIndex()
        if new == self._len_idx:
            return
        self._len_idx = new
        try:
            src = self.source(self.sid)
            d = core.normalize_detect(self.proj['settings']['detect'])
            um_px = core.detection_scale(src, dict(d, len_unit='px'))[3]   # um per px in px mode
        except Exception:
            um_px = core.DET_UM
        k = um_px if new == 0 else 1.0 / um_px                      # px -> um, or um -> px
        for fld in (self.det_blur, self.det_close, self.det_sep):
            t = fld.getText().strip()
            try:
                fld.setText("%.3g" % (float(t) * k) if t else t)
            except ValueError:
                pass
        self._update_px_row()
        if new == 0 and not self._metadata_um(self.sid) and \
                not self.proj['settings'].get('pixel_size_um'):
            JOptionPane.showMessageDialog(
                self, u"No pixel size was found in the image files of this slide.\n"
                      u"Enter it in Detection > pixel size (" + unichr(181) + u"m per pixel, square "
                      u"pixels),\nor use px lengths.", "Pixel size", JOptionPane.WARNING_MESSAGE)
            self.det_px.requestFocusInWindow()

    def _metadata_um(self, sid):
        """Pixel size stored in this slide's image files, or None."""
        if not sid or sid not in self.proj['slides']:
            return None
        for rel in sorted(self.proj['slides'][sid]['channels'].values()):
            um = core.file_um_per_px(spp.abs_path(self.root, rel))
            if um:
                return um
        return None

    def _update_px_row(self):
        """The pixel size field: only in um mode when the files say nothing."""
        show = self.det_len.getSelectedIndex() == 0 and not self._metadata_um(self.sid)
        for c in self._px_row:
            c.setVisible(bool(show))
        self._px_row[1].getParent().revalidate()

    def _set_pixel_size(self):
        t = self.det_px.getText().strip()
        try:
            v = float(t) if t else None
        except ValueError:
            v = None
        if v is not None and v <= 0:
            v = None
        if v == self.proj['settings'].get('pixel_size_um'):
            return
        self.proj['settings']['pixel_size_um'] = v
        for s in self.sources.values():          # the project value comes first
            md = self._metadata_um(s.sid)
            s.um0, s.um_source = ((v, "project") if v else
                                  (md, "image metadata") if md else (None, None))
        self._mark_dirty()

    def detect_params(self):
        d = core.normalize_detect(self.proj['settings']['detect'])
        d['len_unit'] = 'um' if self.det_len.getSelectedIndex() == 0 else 'px'
        chans = [ch for ch, cb in self.det_chan.items() if cb.isSelected()]
        d['channels'] = None if len(chans) == len(self.det_chan) else chans
        d['method'] = str(self.det_method.getSelectedItem())
        m = self.det_manual.getText().strip()
        d['manual'] = int(m) if m.isdigit() else None
        d['invert'] = self.det_invert.isSelected()
        d['blur'] = _num(self.det_blur, d['blur'])
        d['close'] = _num(self.det_close, d['close'])
        d['signal'] = core.SIGNALS[self.det_signal.getSelectedIndex()]
        d['tissue'] = core.TISSUE_MODES[self.det_tissue.getSelectedIndex()]
        d['separate'] = None if self.det_sep.getText().strip() == "" else _num(self.det_sep, 0)
        d['size_split'] = 0 if self.det_mult.getText().strip() == "" else max(0.0, _num(self.det_mult, core.SEED_MULT))
        d['border'] = self.det_border.isSelected()
        opt = lambda f: None if f.getText().strip() == "" else _num(f, None)
        d['size_unit'] = core.SIZE_UNITS[self.det_unit.getSelectedIndex()][0]
        d['size_min'] = opt(self.det_smin)
        d['size_max'] = opt(self.det_smax)
        d['circ_min'] = max(0.0, min(1.0, _num(self.det_cmin, 0.0)))
        d['circ_max'] = max(0.0, min(1.0, _num(self.det_cmax, 1.0)))
        d['circ_on'] = 'padded' if self.det_con.getSelectedIndex() == 0 else 'raw'
        d['fill_min'] = opt(self.det_fill)
        d['outline'] = str(self.det_outline.getSelectedItem())
        d['simplify'] = _num(self.det_simplify, d['simplify'])
        d['right_to_left'] = self.det_rtl.isSelected()
        if d != self.proj['settings']['detect']:
            self.proj['settings']['detect'] = d
            self._mark_dirty()
        return d

    # ---------------------------------------------------------- refresh
    def _slide_status(self, sid, counts, med):
        s = self.proj['slides'][sid]
        if s.get('excluded'):
            return "excluded"
        if s.get('reviewed'):
            return "reviewed"
        n = counts.get(sid, 0)
        if n == 0:
            return "none"
        if med and n < 0.5 * med:
            return "check"
        ids = spp.slide_section_ids(self.proj, sid)
        if any(self.proj['sections'][i].get('origin') != 'auto' for i in ids):
            return "edited"
        return "auto"

    def _refresh_slides(self):
        ids = self._slide_ids_all()
        counts = dict((sid, len(spp.slide_section_ids(self.proj, sid, False)))
                      for sid in ids)
        nz = sorted(v for v in counts.values() if v > 0)
        med = nz[len(nz) // 2] if nz else 0
        self._sync_guard = True
        try:
            sel = [self._slide_row_ids[r] for r in self.slide_table.getSelectedRows()] \
                if hasattr(self, '_slide_row_ids') else []
            self.slide_model.setRowCount(0)
            self._slide_row_ids = ids
            for k, sid in enumerate(ids):
                s = self.proj['slides'][sid]
                self.slide_model.addRow([k + 1, sid, s.get('sample') or "",
                                         counts[sid], self._slide_status(sid, counts, med),
                                         canvas_orient(s)])
            sm = self.slide_table.getSelectionModel()
            for r, sid in enumerate(ids):
                if sid in sel:
                    sm.addSelectionInterval(r, r)
            if self.sid in ids and not sel:
                r = ids.index(self.sid)
                sm.setSelectionInterval(r, r)
        finally:
            self._sync_guard = False

    def _refresh_sections(self):
        self._sync_guard = True
        try:
            self.sec_model.setRowCount(0)
            ids = spp.slide_section_ids(self.proj, self.sid) if self.sid else []
            self._sec_row_ids = ids
            m = self.metrics()
            if ids and self.sid not in self.raws:
                self._ensure_mask_async(self.sid)     # Fill % fills in when ready
            for k, i in enumerate(ids):
                s = self.proj['sections'][i]
                size, circ = m.get(i, (None, None))
                self.sec_model.addRow([
                    self.numbers.get(i, "-"), k + 1, spp.section_sample(self.proj, i),
                    "-" if size is None else ("%.3g" % size),
                    "-" if circ is None else ("%.2f" % circ),
                    self._fill_text(i),
                    s.get('origin', ''), "x" if s.get('excluded') else ""])
            self.hist_size.repaint()
            self.hist_circ.repaint()
            sel = set(self.canvas.selected)
            sm = self.sec_table.getSelectionModel()
            for r, i in enumerate(ids):
                if i in sel:
                    sm.addSelectionInterval(r, r)
        finally:
            self._sync_guard = False

    def _after_change(self, changed_ids=None, slides=True):
        self._renumber()
        self._refresh_sections()
        if slides:
            self._refresh_slides()
        self.canvas.invalidate_overlay(changed_ids)
        self._mark_dirty()

    def _mark_dirty(self):
        self.dirty = True
        self.save_label.setText(spp.UNSAVED_TEXT)
        self.save_label.setForeground(Color(200, 120, 0))

    def set_status(self, text):
        self.status.setText(text)

    # ---------------------------------------------------------- canvas callbacks
    def show_coords(self, x, y):
        self.coords.setText("x %d  y %d" % (x, y))

    def on_mode_changed(self, mode):
        b = self.mode_buttons.get(mode)
        if b is not None and not b.isSelected():
            b.setSelected(True)
        if mode != 'order':
            self.order_clicked = []
        self.set_status({
            'edit': "Edit: click/box select. Hand-drawn sections only: drag to move, "
                    "drag a vertex, double-click edge = add vertex, Alt+click vertex = delete",
            'draw': "Draw: click points; double-click, Enter or click the "
                    "first point to close; Backspace = undo point",
            'wand': "Wand: click inside tissue (uses the detection mask)",
            'split': "Split: click points of the cut line, double-click or Enter = cut "
                     "(both parts re-detected); Backspace = undo point",
            'order': "Click order: click sections in order; Enter/Esc = done"}[mode])

    # ---------------------------------------------------------- metrics
    def _fill_text(self, sec_id):
        """Signal fill % of a section ROI (above-threshold pixels before
        closing / ROI area), from the slide's detection mask."""
        s = self.proj['sections'][sec_id]
        r = self.raws.get(s['slide'])
        if r is None:
            return "-"
        raw, f = r
        p = s['polygon']
        key = (id(raw), len(p), tuple(p[0]), tuple(p[len(p) // 2]))
        c = self._fcache.get(sec_id)
        if c is None or c[0] != key:
            c = (key, core.signal_fill(raw, [[x * f, y * f] for x, y in p]))
            self._fcache[sec_id] = c
        return "%.0f" % c[1]

    def metrics(self):
        """{sec_id: (size in the detection size unit, circularity)} for all
        sections; circularity on the outline or the padded shape (as set
        in Detection). Cached per polygon / padding / settings. Padded
        shapes are computed on a worker thread (circularity None until
        ready; the table and histograms refresh as results arrive)."""
        d = self.proj['settings']['detect']
        unit, con = d.get('size_unit', 'pct'), d.get('circ_on', 'padded')
        cache = getattr(self, '_mcache', None)
        if cache is None:
            cache = self._mcache = {}
        raw = {}
        sigs = {}
        pending = []
        for i, s in self.proj['sections'].items():
            p = s['polygon']
            sid = s['slide']
            if con == 'padded' and sid not in sigs:
                sigs[sid] = spp.slide_signature(self.proj, sid)   # neighbours shape the padding
            key = (len(p), tuple(p[0]), tuple(p[-1]), con, spp.padding_mode(self.proj),
                   spp.section_padding(self.proj, i) if con == 'padded' else 0,
                   sigs.get(sid))
            c = cache.get(i)
            if c is None or c[0] != key:
                if con == 'padded':
                    pending.append((i, key))
                    raw[i] = (spp.poly_area(p), None)
                    continue
                c = (key, core.section_area_circ(self.proj, i, con))
                cache[i] = c
            raw[i] = c[1]
        if pending:
            self._start_metrics_job(pending)
        amax = {}
        for i, (a, _c) in raw.items():
            sid = self.proj['sections'][i]['slide']
            amax[sid] = max(amax.get(sid, 0), a)
        px_um = self.proj['settings'].get('pixel_size_um')
        out = {}
        for i, (a, c) in raw.items():
            try:
                sz = core.size_in_unit(a, 1.0, unit,
                                       amax[self.proj['sections'][i]['slide']], px_um)
            except ValueError:
                sz = None
            out[i] = (sz, c)
        return out

    def _start_metrics_job(self, pending):
        """Padded-shape circularity for `pending` [(sec_id, key)] on a worker
        thread (current slide first), from a snapshot of the project."""
        if getattr(self, '_mjob', False):
            self._mjob_again = True          # rerun when the current job ends
            return
        self._mjob, self._mjob_again = True, False
        cur = self.sid
        pending = sorted(pending, key=lambda t: 0 if self.proj['sections'][t[0]]['slide'] == cur else 1)
        snap = copy.deepcopy(self.proj)
        win = self

        def work():
            batch = []
            try:
                for n, (i, key) in enumerate(pending):
                    try:
                        v = core.section_area_circ(snap, i, 'padded')
                    except Exception as e:
                        IJ.log("[Cropper] padded shape %s: %s" % (i, e))
                        v = (spp.poly_area(snap['sections'][i]['polygon']), None)
                    batch.append((i, key, v))
                    if len(batch) >= 25 or n == len(pending) - 1:
                        b, batch = batch, []
                        on_edt(lambda b=b: win._metrics_in(b))
            finally:
                on_edt(win._metrics_done)
        t = threading.Thread(target=work)
        t.setDaemon(True)
        t.start()

    def _metrics_in(self, batch):
        for i, key, v in batch:
            self._mcache[i] = (key, v)
        if self.isDisplayable():
            self._refresh_sections()

    def _metrics_done(self):
        self._mjob = False
        if self._mjob_again and self.isDisplayable():
            self._mjob_again = False
            self._refresh_sections()         # restarts for what changed meanwhile

    def on_canvas_selection(self, ids):
        self.hist_size.repaint(); self.hist_circ.repaint()
        self._sync_guard = True
        try:
            sm = self.sec_table.getSelectionModel()
            sm.clearSelection()
            for r, i in enumerate(self._sec_row_ids):
                if i in ids:
                    sm.addSelectionInterval(r, r)
        finally:
            self._sync_guard = False

    def canvas_image_changed(self):
        self.canvas.invalidate_image()

    def _toggle_mask(self):
        self.canvas.show_mask = self.mask_box.isSelected()
        if self.canvas.show_mask and self.sid not in self.masks:
            self._ensure_mask(self.sid)
        self.canvas.repaint()

    def _toggle_pad(self):
        self.canvas.show_pad = self.pad_box.isSelected()
        self.canvas.repaint()

    # ---------------------------------------------------------- undo
    def _snapshot(self, sid):
        ids = spp.slide_section_ids(self.proj, sid)
        o = self.proj.get('orientation', {})
        return {'sections': dict((i, copy.deepcopy(self.proj['sections'][i])) for i in ids),
                'orientation': dict((i, copy.deepcopy(o[i])) for i in ids if i in o)}

    def _restore(self, sid, snap):
        o = self.proj.setdefault('orientation', {})
        for i in spp.slide_section_ids(self.proj, sid):
            del self.proj['sections'][i]
            o.pop(i, None)
        for i, s in snap['sections'].items():
            self.proj['sections'][i] = copy.deepcopy(s)
        for i, t in snap.get('orientation', {}).items():
            o[i] = copy.deepcopy(t)

    def begin_edit(self, sid=None):
        sid = sid or self.sid
        st = self.undo.setdefault(sid, [])
        st.append(self._snapshot(sid))
        del st[:-UNDO_MAX]
        self.redo[sid] = []

    def do_undo(self):
        st = self.undo.get(self.sid)
        if not st:
            self.set_status("Nothing to undo"); return
        self.redo.setdefault(self.sid, []).append(self._snapshot(self.sid))
        self._restore(self.sid, st.pop())
        self.canvas.set_selection([])
        self._after_change()

    def do_redo(self):
        st = self.redo.get(self.sid)
        if not st:
            self.set_status("Nothing to redo"); return
        self.undo.setdefault(self.sid, []).append(self._snapshot(self.sid))
        self._restore(self.sid, st.pop())
        self.canvas.set_selection([])
        self._after_change()

    # ---------------------------------------------------------- slides
    def show_slide(self, sid, from_table=False):
        if sid not in self.proj['slides']:
            return
        if self.canvas.mode == 'order':
            self.order_done()
        self.sid = sid
        try:
            self.canvas.set_slide(self.source(sid))
        except Exception as e:
            IJ.log("[Cropper] cannot open slide %s: %s" % (sid, e))
            IJ.log(traceback.format_exc())
        if hasattr(self, '_px_row'):
            self._update_px_row()
        self._refresh_sections()
        if not from_table:
            self._refresh_slides()
            r = self._slide_row_ids.index(sid)
            self._sync_guard = True
            self.slide_table.getSelectionModel().setSelectionInterval(r, r)
            self.slide_table.scrollRectToVisible(self.slide_table.getCellRect(r, 0, True))
            self._sync_guard = False
        s = self.proj['slides'][sid]
        self.set_status("%s   sample=%s   %s" % (sid, s.get('sample') or "-",
                                                canvas_orient(s) or "no rotation"))
        if self.canvas.show_mask and sid not in self.masks:
            self._ensure_mask(sid)

    def step_slide(self, d):
        ids = self._slide_ids_all()
        if not ids:
            return
        k = ids.index(self.sid) if self.sid in ids else 0
        k = max(0, min(len(ids) - 1, k + d))
        self.show_slide(ids[k])

    def _selected_slides(self):
        ids = [self._slide_row_ids[r] for r in self.slide_table.getSelectedRows()]
        return ids or ([self.sid] if self.sid else [])

    def move_slides(self, d):
        ids = self._slide_ids_all()
        sel = set(self._selected_slides())
        order = list(ids)
        rng = range(len(order)) if d < 0 else range(len(order) - 1, -1, -1)
        for k in rng:
            j = k + d
            if order[k] in sel and 0 <= j < len(order) and order[j] not in sel:
                order[k], order[j] = order[j], order[k]
        for k, sid in enumerate(order):
            self.proj['slides'][sid]['order'] = k
        self._after_change()
        sm = self.slide_table.getSelectionModel()
        self._sync_guard = True
        sm.clearSelection()
        for r, sid in enumerate(self._slide_row_ids):
            if sid in sel:
                sm.addSelectionInterval(r, r)
        self._sync_guard = False

    def _ask_label(self, title, initial=""):
        v = JOptionPane.showInputDialog(self, title, initial)
        if v is None:
            return None
        v = v.strip()
        w = spp.label_warnings(v)
        if w:
            r = JOptionPane.showConfirmDialog(
                self, w + "\nThis can break channel detection when the files "
                "are loaded again by name. Use anyway?", "Sample name",
                JOptionPane.YES_NO_OPTION)
            if r != JOptionPane.YES_OPTION:
                return None
        return v

    def set_slide_sample(self):
        sids = self._selected_slides()
        if not sids:
            return
        cur = self.proj['slides'][sids[0]].get('sample') or ""
        v = self._ask_label("Sample for %d selected slide(s):" % len(sids), cur)
        if v is None:
            return
        spp.set_field(self.proj, 'slides', sids, 'sample', v)
        self._after_change()

    def sample_from_name(self):
        sids = self._selected_slides()
        if not sids:
            return
        toks = TOKEN_SPLIT.split(sids[0])
        desc = "\n".join("  %d: %s" % (i + 1, t) for i, t in enumerate(toks))
        v = JOptionPane.showInputDialog(
            self, "Tokens of '%s':\n%s\n\nToken number(s) to use as sample,\n"
            "e.g. 1  or  1-2 (joined without separator):" % (sids[0], desc), "1")
        if v is None:
            return
        v = v.strip()
        try:
            if '-' in v:
                a, b = [int(x) for x in v.split('-')]
            else:
                a = b = int(v)
        except ValueError:
            JOptionPane.showMessageDialog(self, "Enter a number like 1 or 1-2")
            return
        bad = []
        for sid in sids:
            t = TOKEN_SPLIT.split(sid)
            if b > len(t):
                bad.append(sid); continue
            self.proj['slides'][sid]['sample'] = "".join(t[a - 1:b])
        if bad:
            JOptionPane.showMessageDialog(self, "Skipped (too few tokens):\n" + "\n".join(bad))
        self._after_change()

    def toggle_reviewed(self, sids=None, value=None):
        sids = sids or self._selected_slides()
        if value is None:
            value = not all(self.proj['slides'][s].get('reviewed') for s in sids)
        spp.set_field(self.proj, 'slides', sids, 'reviewed', value)
        self._after_change()

    def toggle_slide_excluded(self):
        sids = self._selected_slides()
        value = not all(self.proj['slides'][s].get('excluded') for s in sids)
        spp.set_field(self.proj, 'slides', sids, 'excluded', value)
        self._after_change()

    # ---------------------------------------------------------- detection
    def _ensure_mask_async(self, sid):
        """Signal mask of a slide (for the Fill % column) built on a worker
        thread; the section table refreshes when it is ready."""
        jobs = getattr(self, '_mask_jobs', None)
        if jobs is None:
            jobs = self._mask_jobs = set()
        if sid in jobs:
            return
        jobs.add(sid)
        src = self.source(sid)
        params = self.detect_params()            # UI fields: read on the EDT
        win = self

        def work():
            pool = core.ReaderPool()
            try:
                comb, f, pp, info = core.detection_input(src, pool, win.proj, params)
                mask, t, raw = core.make_mask(comb, pp, want_raw=True)
                def done():
                    jobs.discard(sid)
                    win.raws[sid] = (raw, f)
                    win.masks.setdefault(sid, (mask, f))
                    win.combs.setdefault(sid, (comb, f, t))
                    win.pxp.setdefault(sid, pp)
                    if win.sid == sid and win.isDisplayable():
                        win.det_info.setText("threshold %d   |   %s" % (t, info))
                        win._refresh_sections()
                        win.canvas.repaint()
                on_edt(done)
            except Exception as e:
                IJ.log("[Cropper] signal mask: %s" % e)
                on_edt(lambda: jobs.discard(sid))
            finally:
                pool.close()
        t = threading.Thread(target=work)
        t.setDaemon(True)
        t.start()

    def _ensure_mask(self, sid):
        src = self.source(sid)
        pool = core.ReaderPool()
        try:
            params = self.detect_params()
            comb, f, pp, info = core.detection_input(src, pool, self.proj, params)
            mask, t, raw = core.make_mask(comb, pp, want_raw=True)
            self.raws[sid] = (raw, f)
            self.masks[sid] = (mask, f)
            self.combs[sid] = (comb, f, t)
            self.pxp[sid] = pp
            self.det_info.setText("threshold %d   |   %s" % (t, info))
        finally:
            pool.close()

    def _apply_detection(self, sid, res):
        self.begin_edit(sid)
        spp.delete_sections(self.proj, spp.slide_section_ids(self.proj, sid))   # also their step-3 tags
        for k, poly in enumerate(res['polys']):
            spp.new_section(self.proj, sid, poly, origin='auto', order=k)
        self.proj['slides'][sid]['reviewed'] = False
        self.masks[sid] = (res['mask'], res['f'])
        self.combs[sid] = (res['comb'], res['f'], res['threshold'])
        self.raws[sid] = (res['raw'], res['f'])
        self.pxp[sid] = res.get('px_params')
        # every object the detection found (kept or not): the histograms show
        # them, so the min / max lines can be placed against the whole spread
        prm = res.get('params') or {}
        self.cands[sid] = {'unit': prm.get('size_unit'), 'con': prm.get('circ_on'),
                           'items': [(r['size'], r['circ'], r['rejected'] is None)
                                     for r in res.get('report', [])]}

    def _confirm_overwrite(self, sids):
        touched = [s for s in sids if self.proj['slides'][s].get('reviewed') or any(
            self.proj['sections'][i].get('origin') != 'auto'
            for i in spp.slide_section_ids(self.proj, s))]
        if not touched:
            return sids
        opts = ["Overwrite all", "Skip those", "Cancel"]
        r = JOptionPane.showOptionDialog(
            self, "%d slide(s) have edited or reviewed sections:\n%s" % (
                len(touched), "\n".join(touched[:12]) + ("\n..." if len(touched) > 12 else "")),
            "Detect", JOptionPane.DEFAULT_OPTION, JOptionPane.WARNING_MESSAGE,
            None, opts, opts[1])
        if r == 0:
            return sids
        if r == 1:
            return [s for s in sids if s not in touched]
        return []

    def detect_current(self):
        if self.sid:
            self._detect([self.sid])

    def detect_selected(self):
        self._detect(self._selected_slides())

    def _detect(self, sids):
        sids = self._confirm_overwrite(sids)
        if not sids:
            return
        params = self.detect_params()
        prog = export_mod.ProgressDialog(self, "Detecting sections", len(sids))
        win = self
        def work():
            pool = core.ReaderPool()
            results = []
            prog.eta.set_total(len(sids))      # same detection resolution for every slide
            try:
                for k, sid in enumerate(sids):
                    if prog.cancelled:
                        break
                    if k:
                        prog.eta.add(1)
                    prog.update(k, "Detecting %s (%d/%d)" % (sid, k + 1, len(sids)))
                    try:
                        src = core.SlideSource(win.root, win.proj, sid, win.manifest, win.ws)
                        results.append((sid, core.detect_sections(src, pool, win.proj, params)))
                    except Exception as e:
                        IJ.log("[Cropper] detect %s failed: %s" % (sid, e))
                        IJ.log(traceback.format_exc())
            finally:
                pool.close()
            def done():
                prog.dispose()
                unit = [l for k, l in core.SIZE_UNITS if k == params.get('size_unit')][0]
                rej = {'size': 0, 'circularity': 0, 'fill': 0}
                for sid, res in results:
                    win._apply_detection(sid, res)
                    IJ.log("[Cropper] %s: threshold %d, %s, objects (size in %s, circularity on %s):" % (
                        sid, res['threshold'], res.get('scale_info', ''), unit,
                        "section+padding" if params.get('circ_on') == 'padded' else "section"))
                    for r in sorted(res['report'], key=lambda r: (r['cy'], r['cx'])):
                        IJ.log("    at (%5d,%5d)  size %10.4g  circ %.3f  fill %5.1f%%  %s" % (
                            r['cx'], r['cy'], r['size'], r['circ'], r.get('fill') or 0,
                            ("rejected: " + r['rejected']) if r['rejected'] else "kept"))
                        if r['rejected']:
                            rej[r['rejected']] += 1
                n = sum(len(r['polys']) for _, r in results)
                if results:
                    win.det_info.setText("thr %d | %d kept, %d out (size %d, circ %d, fill %d) | %s" % (
                        results[-1][1]['threshold'], n, sum(rej.values()),
                        rej['size'], rej['circularity'], rej['fill'],
                        results[-1][1].get('scale_info', '')))
                win.canvas.set_selection([])
                win._after_change()
                win.set_status("Detected %d section(s) on %d slide(s)" % (n, len(results)))
            on_edt(done)
        t = threading.Thread(target=work)
        t.setDaemon(True)
        t.start()
        prog.setVisible(True)

    def wand_add(self, x, y):
        if self.sid not in self.masks:
            self._ensure_mask(self.sid)
        mask, f = self.masks[self.sid]
        poly = core.wand_polygon(mask, f, x, y, self.detect_params())
        if poly is None:
            self.set_status("Wand: no tissue in the mask at this point "
                            "(try a lower manual threshold, or Draw)")
            return
        self.begin_edit()
        i = spp.new_section(self.proj, self.sid, poly, origin='manual')
        self.canvas.set_selection([i])
        self._after_change([i])

    def _apply_pad_settings(self):
        st = self.proj['settings']
        pf = _num(self.pad_frac, st['padding_frac'] * 100) / 100.0
        fv = _num(self.fill_val, st['fill_value'])
        pm = 'hull' if self.pad_mode.getSelectedIndex() == 0 else 'outline'
        ov = bool(self.pad_overlap.isSelected())
        if (pf != st['padding_frac'] or fv != st['fill_value'] or pm != spp.padding_mode(self.proj)
                or ov != bool(st.get('padding_overlap'))):
            st['padding_frac'] = max(0.0, pf)
            st['fill_value'] = fv
            st['padding_mode'] = pm
            st['padding_overlap'] = ov
            self._after_change(slides=False)

    # ---------------------------------------------------------- section edits
    def _sel(self):
        return [i for i in self.canvas.selected if i in self.proj['sections']]

    def geometry_edited(self, ids):
        for i in ids:
            s = self.proj['sections'].get(i)
            if s is not None and s.get('origin') == 'auto':
                s['origin'] = 'edited'
        self._after_change(ids)

    def add_drawn(self, pts):
        self.begin_edit()
        i = spp.new_section(self.proj, self.sid, pts, origin='drawn')
        self.canvas.set_selection([i])
        self._after_change([i])

    def delete_selected(self):
        ids = self._sel()
        if not ids:
            return
        self.begin_edit()
        spp.delete_sections(self.proj, ids)
        self._compact_orders(self.sid)
        self.canvas.set_selection([])
        self._after_change(ids)

    def delete_vertex(self, sec_id, vi):
        poly = self.proj['sections'][sec_id]['polygon']
        if len(poly) <= 3:
            self.set_status("A section needs at least 3 vertices")
            return
        self.begin_edit()
        del poly[vi]
        self.geometry_edited([sec_id])

    def insert_vertex(self, sec_id, idx, pt):
        self.begin_edit()
        self.proj['sections'][sec_id]['polygon'].insert(idx, pt)
        self.geometry_edited([sec_id])

    def _compact_orders(self, sid):
        spp.reorder_slide_sections(self.proj, sid, spp.slide_section_ids(self.proj, sid))

    def split_poly(self, pts):
        """Split every section the drawn polyline crosses and re-detect
        both slices (seeded growth through tissue, see split_seeded); if
        that finds no tissue on one side, cut along the line instead."""
        hits = [i for i in spp.slide_section_ids(self.proj, self.sid)
                if core.polyline_crosses(self.proj['sections'][i]['polygon'], pts)]
        if not hits:
            self.set_status("Split: the line does not cross a section")
            return
        if self.sid not in self.combs:
            self._ensure_mask(self.sid)
        comb, f, t = self.combs[self.sid]
        # lengths in px, as the detection image of this slide was made
        params = self.pxp.get(self.sid) or core.px_params(core.normalize_detect(self.detect_params()),
                                                          core.DET_UM)
        self.begin_edit()
        new_ids, ids, n_geo = [], spp.slide_section_ids(self.proj, self.sid), 0
        for i in hits:
            sec = self.proj['sections'][i]
            parts = core.split_seeded(comb, f, t, sec['polygon'], pts, params)
            if not parts:
                parts = core.split_by_polyline(sec['polygon'], pts)
                if not parts:
                    continue
                parts = [parts[k] for k in spp.auto_order(list(parts))]
                n_geo += 1
            sec['polygon'] = parts[0]
            sec['origin'] = 'edited'
            j = spp.new_section(self.proj, self.sid, parts[1], origin='edited')
            for k in ('padding', 'sample', 'excluded'):
                self.proj['sections'][j][k] = sec.get(k)
            ids.insert(ids.index(i) + 1, j)
            new_ids += [i, j]
        spp.reorder_slide_sections(self.proj, self.sid, ids)
        self.canvas.set_selection(new_ids)
        self._after_change()
        self.set_status("Split %d section(s)%s" % (
            len(new_ids) // 2,
            (" (%d cut along the line: no tissue found on one side)" % n_geo) if n_geo else ""))

    def merge_selected(self):
        ids = self._sel()
        if len(ids) < 2:
            self.set_status("Merge: select 2 or more sections")
            return
        self.begin_edit()
        secs = self.proj['sections']
        ids.sort(key=lambda i: secs[i]['order'])
        secs[ids[0]]['polygon'] = core.merge_polygons([secs[i]['polygon'] for i in ids])
        secs[ids[0]]['origin'] = 'edited'
        spp.delete_sections(self.proj, ids[1:])
        self._compact_orders(self.sid)
        self.canvas.set_selection([ids[0]])
        self._after_change()

    def move_sections(self, d):
        sel = set(self._sel())
        if not sel:
            return
        self.begin_edit()
        order = spp.slide_section_ids(self.proj, self.sid)
        rng = range(len(order)) if d < 0 else range(len(order) - 1, -1, -1)
        for k in rng:
            j = k + d
            if order[k] in sel and 0 <= j < len(order) and order[j] not in sel:
                order[k], order[j] = order[j], order[k]
        spp.reorder_slide_sections(self.proj, self.sid, order)
        self._after_change()

    def auto_order_current(self):
        ids = spp.slide_section_ids(self.proj, self.sid)
        if not ids:
            return
        self.begin_edit()
        d = self.detect_params()
        idx = spp.auto_order([self.proj['sections'][i]['polygon'] for i in ids],
                             d.get('row_tol', 0.5), d.get('right_to_left'))
        spp.reorder_slide_sections(self.proj, self.sid, [ids[k] for k in idx])
        self._after_change()

    def order_click(self, sec_id):
        if not self.order_clicked:
            self.begin_edit()
        if sec_id in self.order_clicked:
            return
        self.order_clicked.append(sec_id)
        rest = [i for i in spp.slide_section_ids(self.proj, self.sid)
                if i not in self.order_clicked]
        spp.reorder_slide_sections(self.proj, self.sid, self.order_clicked + rest)
        self._after_change()
        self.set_status("Click order: %d clicked. Enter/Esc = done" % len(self.order_clicked))

    def order_done(self):
        self.order_clicked = []
        self.canvas.repaint()

    def set_section_sample(self):
        ids = self._sel()
        if not ids:
            self.set_status("Select sections first"); return
        v = self._ask_label("Sample for %d section(s) (empty = use slide sample):" % len(ids),
                            self.proj['sections'][ids[0]].get('sample') or "")
        if v is None:
            return
        self.begin_edit()
        spp.set_field(self.proj, 'sections', ids, 'sample', v or None)
        self._after_change()

    def set_section_padding(self):
        ids = self._sel()
        if not ids:
            self.set_status("Select sections first"); return
        v = JOptionPane.showInputDialog(
            self, "Padding in px for %d section(s)\n(empty = auto, %g%% of diagonal):"
            % (len(ids), self.proj['settings']['padding_frac'] * 100), "")
        if v is None:
            return
        v = v.strip()
        try:
            val = None if v == "" else max(0, int(float(v)))
        except ValueError:
            return
        self.begin_edit()
        spp.set_field(self.proj, 'sections', ids, 'padding', val)
        self._after_change(ids)

    def toggle_section_excluded(self):
        ids = self._sel()
        if not ids:
            return
        self.begin_edit()
        v = not all(self.proj['sections'][i].get('excluded') for i in ids)
        spp.set_field(self.proj, 'sections', ids, 'excluded', v)
        self._after_change(ids)

    # ---------------------------------------------------------- save / close
    def save(self):
        self.detect_params()
        self._apply_pad_settings()
        mine = self.proj
        changed = []
        def fn(p):
            for sid, s in mine['slides'].items():
                d = p['slides'].get(sid)
                if d is None:
                    continue
                disk = [int(d.get('rotation', 0)), bool(d.get('flip_h', False))]
                if list(s.get('roi_orient') or disk) != disk:
                    changed.append(sid)
            p['sections'] = copy.deepcopy(mine['sections'])
            # step-3 tags: drop those of deleted sections, bring back those of
            # sections restored by undo (others may be newer from step 3)
            o = p.setdefault('orientation', {})
            for i in [i for i in o if i not in p['sections']]:
                del o[i]
            for i, t in mine.get('orientation', {}).items():
                if i in p['sections'] and i not in o:
                    o[i] = copy.deepcopy(t)
            p['next_section_id'] = max(p.get('next_section_id', 1), mine['next_section_id'])
            for sid, s in mine['slides'].items():
                d = p['slides'].get(sid)
                if d is None:
                    continue
                for k in ('order', 'sample', 'excluded', 'reviewed', 'roi_orient'):
                    d[k] = copy.deepcopy(s.get(k))
            for k in ('detect', 'padding_frac', 'padding_mode', 'padding_overlap', 'fill_value',
                      'filename_template', 'pixel_size_um'):
                p['settings'][k] = copy.deepcopy(mine['settings'].get(k))
        try:
            saved = spp.update_project(self.root, fn)
        except Exception as e:
            IJ.log(traceback.format_exc())
            JOptionPane.showMessageDialog(self, "Save failed:\n%s" % e)
            return False
        self.proj = saved
        if changed:
            # stage 1 rotated these slides while we were open
            self._reconcile_orientation()
            for sid in changed:
                self.sources.pop(sid, None)
                self.masks.pop(sid, None)
            spp.save_project(self.root, self.proj)
            if self.sid in changed:
                self.show_slide(self.sid)
            JOptionPane.showMessageDialog(
                self, "Stage 1 changed the orientation of %d slide(s) while "
                "this window was open.\nTheir sections were re-mapped; please "
                "check them." % len(changed))
        self.exposure_version += 1          # stage-1 exposure may have changed
        self.canvas.invalidate_image()
        self._renumber()
        self._refresh_slides()
        self._refresh_sections()
        self.dirty = False
        self.save_label.setText(spp.saved_text(self.proj.get("modified")))
        self.save_label.setForeground(Color(40, 120, 60))
        return True

    def _close(self):
        if self.dirty:
            r = JOptionPane.showConfirmDialog(
                self, "Save sections before closing?", "Unsaved changes",
                JOptionPane.YES_NO_CANCEL_OPTION)
            if r in (JOptionPane.CANCEL_OPTION, JOptionPane.CLOSED_OPTION):
                return
            if r == JOptionPane.YES_OPTION and not self.save():
                return
        self.canvas.stop_loader()
        self.canvas.pool.close()
        self.dispose()

    def open_export(self):
        self.detect_params()
        self._apply_pad_settings()
        export_mod.open_export_dialog(self)

    def update_names(self):
        """Rename the section files already exported (step 2 and the step-3
        folders made from them) to the current sample names / numbering."""
        if self.dirty and not self.save():
            return
        try:
            plans = srn.plan_renames(self.root, self.proj)
        except Exception as e:
            IJ.log(traceback.format_exc())
            JOptionPane.showMessageDialog(self, u"Could not read the exports:\n%s" % e)
            return
        if not plans:
            JOptionPane.showMessageDialog(self, "The exported file names already match the project.",
                                          "Update names", JOptionPane.INFORMATION_MESSAGE)
            return
        ok = [p for p in plans if not p['error']]
        nf = sum(len(p['renames']) for p in ok)
        msg = u"Rename %d file(s) in %d folder(s) to the current\nsample names and numbering? (no new crops)" % (
            nf, len(ok))
        bad = [p for p in plans if p['error']]
        if bad:
            msg += u"\n\n%d folder(s) will be skipped: %s" % (len(bad), bad[0]['error'])
        if JOptionPane.showConfirmDialog(self, msg, "Update names",
                                         JOptionPane.OK_CANCEL_OPTION) != JOptionPane.OK_OPTION:
            return
        import time
        n, errors = srn.apply_renames(plans, time.strftime("%Y%m%d_%H%M%S"))
        for e in errors:
            IJ.log("[Cropper] update names: " + e)
        JOptionPane.showMessageDialog(self, "%d file(s) renamed%s" % (
            n, ("\n%d folder(s) skipped, see Log" % len(errors)) if errors else ""),
            "Update names", JOptionPane.INFORMATION_MESSAGE)


def canvas_orient(slide):
    parts = []
    if slide.get('rotation'):
        parts.append("R%d" % slide['rotation'])
    if slide.get('flip_h'):
        parts.append("F")
    return " ".join(parts)

