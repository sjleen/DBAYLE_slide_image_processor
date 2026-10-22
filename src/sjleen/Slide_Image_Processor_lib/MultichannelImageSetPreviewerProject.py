# ============================================================
# MultichannelImageSetPreviewerProject.py
#
# Section-pipeline (stage 1) integration for MISP, loaded via
# execfile() into the main globals:
#   - orientation helpers used by ImagePanel / MergedImagePanel
#     (display-only slide rotation + flip; pyramid untouched)
#   - image discovery dialog: folder tag + filename pattern ->
#     checklist -> files to load
#   - project bar: group check, rotate / flip checked slides,
#     Save to project
#   - project.json restore (channel params, slide orientation)
#
# Shared file format: SectionPipelineProject.py
# After loading call: register_project_methods(MainWindow)
# ============================================================

import os, math, fnmatch, time, traceback
from jarray import zeros

from java.awt       import (BorderLayout, Dimension, Color, Font, FlowLayout,
                           Insets)
from java.awt.event import (MouseAdapter, ActionListener, ComponentAdapter)
from java.awt.geom  import AffineTransform, Point2D
from java.awt.image import BufferedImage, IndexColorModel
from java.lang      import Boolean, Object
from javax.swing    import (JDialog, JPanel, JLabel, JButton, JTextField,
                            JTable, JScrollPane, JOptionPane, JPopupMenu,
                            JMenuItem, JCheckBox, Box, BoxLayout,
                            ListSelectionModel, SwingUtilities, BorderFactory,
                            JComponent, KeyStroke, AbstractAction)
from javax.swing.border import EmptyBorder
from javax.swing.event  import TableModelListener
from javax.swing.table  import DefaultTableModel

from ij import IJ
from ij.process import ByteProcessor, ColorProcessor, Blitter, ImageProcessor

import SectionPipelineProject as spp
import FileNameParser as fnp


DEFAULT_NAME_PATTERN = "*.tif;*.tiff;*.jpg;*.jpeg;*.png"


# ==============================================================
# Orientation helpers (panel space <-> source-oriented buffer)
#
# view = rotateCW(rot)( flipH(source) ), same convention as
# SectionPipelineProject. The panel renders a buffer in SOURCE
# orientation and draws it through orient_transform().
# ==============================================================

def orient_buf_dims(panel):
    pw = max(1, panel.getWidth())
    ph = max(1, panel.getHeight())
    if getattr(panel, 'view_rot', 0) in (90, 270):
        return ph, pw
    return pw, ph


def orient_transform(panel, bw, bh):
    at = AffineTransform()
    at.translate(panel.getWidth() / 2.0, panel.getHeight() / 2.0)
    rot = getattr(panel, 'view_rot', 0)
    if rot:
        at.rotate(math.radians(rot))
    if getattr(panel, 'view_flip', False):
        at.scale(-1.0, 1.0)
    at.translate(-bw / 2.0, -bh / 2.0)
    return at


def panel_to_buf(panel, px, py):
    bw, bh = orient_buf_dims(panel)
    inv = orient_transform(panel, bw, bh).createInverse()
    p = inv.transform(Point2D.Double(px, py), None)
    return p.getX(), p.getY()


def panel_delta_to_buf(panel, dx, dy):
    bw, bh = orient_buf_dims(panel)
    inv = orient_transform(panel, bw, bh).createInverse()
    p = inv.deltaTransform(Point2D.Double(dx, dy), None)
    return p.getX(), p.getY()


def toggle_zoom(panel, px, py):
    """Double-click at any zoom: full resolution (1 image px per screen
    px) centred on the clicked point."""
    ix, iy = panel._panel_to_img(px, py)
    fit = panel._fit_scale()
    panel.zoom = max(1.0, 1.0 / fit) if fit > 0 else 1.0
    panel.cx, panel.cy = ix, iy
    panel._render_cache = None
    panel.repaint()
    panel._notify_sync()


def orient_tag(rot, flip):
    parts = []
    if rot:  parts.append("R%d" % rot)
    if flip: parts.append("F")
    return " [" + " ".join(parts) + "]" if parts else ""


# ==============================================================
# Fast pixel helpers: Java image ops instead of per-pixel Jython
# loops (~1 us per pixel). Results are pixel-identical to the old
# loops: tint = (v * c) // 255, merge = clamp(sum, 255).
# ==============================================================

_SAT_CM = {}
_TINT = {}


def sat_image(p8, under, over):
    """8-bit gray BufferedImage where 0 shows blue (under) and 255 red
    (over), through an indexed color table."""
    key = (bool(under), bool(over))
    cm = _SAT_CM.get(key)
    if cm is None:
        r = zeros(256, 'b'); g = zeros(256, 'b'); b = zeros(256, 'b')
        for v in range(256):
            s = v - 256 if v > 127 else v
            r[v] = g[v] = b[v] = s
        if under:
            r[0], g[0], b[0] = 0, 0, -1          # (0, 0, 255)
        if over:
            r[255], g[255], b[255] = -1, 0, 0    # (255, 0, 0)
        cm = IndexColorModel(8, 256, r, g, b)
        _SAT_CM[key] = cm
    w, h = p8.getWidth(), p8.getHeight()
    bi = BufferedImage(w, h, BufferedImage.TYPE_BYTE_INDEXED, cm)
    bi.getRaster().setDataElements(0, 0, w, h, p8.getPixels())
    return bi


def tint_table(c):
    t = _TINT.get(c)
    if t is None:
        t = zeros(256, 'i')
        for v in range(256):
            t[v] = (v * c) // 255
        _TINT[c] = t
    return t


def new_rgb_acc(w, h):
    return [ByteProcessor(w, h), ByteProcessor(w, h), ByteProcessor(w, h)]


def add_tinted(acc, p8, color, x=0, y=0):
    """Add p8 tinted by color into the [r, g, b] ByteProcessors at
    (x, y); Blitter.ADD saturates at 255 and clips to the target."""
    for a, c in zip(acc, (color.getRed(), color.getGreen(), color.getBlue())):
        if c == 0:
            continue
        if c == 255:
            t = p8
        else:
            t = p8.duplicate()
            t.applyTable(tint_table(c))
        a.copyBits(t, x, y, Blitter.ADD)


def rgb_from(acc):
    cp = ColorProcessor(acc[0].getWidth(), acc[0].getHeight())
    cp.setRGB(acc[0].getPixels(), acc[1].getPixels(), acc[2].getPixels())
    return cp


def colorize8(p8, color):
    acc = new_rgb_acc(p8.getWidth(), p8.getHeight())
    add_tinted(acc, p8, color)
    return rgb_from(acc)


# ==============================================================
# Background tile loading (no file reads on the paint thread)
#
# Panels ask get(); a missing tile is queued (newest request first)
# and None is returned, so the panel draws the overview (smallest
# pyramid level, read once) for that area. When the loader thread has
# read the tile it re-renders only the panels that use that file.
# ==============================================================

import threading
from collections import OrderedDict

_ASYNC = {}


def get_async(tile_cache):
    a = _ASYNC.get(id(tile_cache))
    if a is None:
        a = AsyncTiles(tile_cache)
        _ASYNC[id(tile_cache)] = a
    return a


class AsyncTiles(object):
    MAX_WANT = 256

    def __init__(self, tile_cache):
        self.cache = tile_cache
        self.cond = threading.Condition()
        self.want = OrderedDict()           # key -> args, newest last
        self.panels = {}                    # path -> [panels]
        self.overviews = {}                 # path -> processor
        self.pool = TileReaderPool()        # loader thread only
        self.alive = True
        t = threading.Thread(target=self._run)
        t.setDaemon(True)
        t.start()

    def register(self, panel, paths):
        for p in paths:
            self.panels.setdefault(p, []).append(panel)

    def get(self, path, tx, ty, tw, th, pt, le):
        key = (path, tx, ty)
        with self.cond:
            proc = self.cache.get(key)
            if proc is not None:
                return proc
            self.want.pop(key, None)
            self.want[key] = (path, tx, ty, tw, th)
            while len(self.want) > self.MAX_WANT:
                self.want.popitem(last=False)
            self.cond.notifyAll()
        return None

    def overview(self, path, reader_pool):
        """Whole smallest level, read once (small), on the caller's thread."""
        ov = self.overviews.get(path)
        if ov is None:
            r = reader_pool.get(path)
            w, h = r.getSizeX(), r.getSizeY()
            ov = bytes_to_processor(r.openBytes(0, 0, 0, w, h), r.getPixelType(),
                                    w, h, r.isLittleEndian())
            self.overviews[path] = ov
        return ov

    def _run(self):
        while self.alive:
            with self.cond:
                while self.alive and not self.want:
                    self.cond.wait()
                if not self.alive:
                    break
                key, (path, tx, ty, tw, th) = self.want.popitem(last=True)
            try:
                r = self.pool.get(path)
                proc = bytes_to_processor(r.openBytes(0, tx, ty, tw, th),
                                          r.getPixelType(), tw, th, r.isLittleEndian())
            except Exception as e:
                IJ.log("[MISP] tile %s (%d,%d): %s" % (os.path.basename(path), tx, ty, e))
                continue
            with self.cond:
                self.cache.put(key, proc)
            for p in self.panels.get(path, []):
                p._render_cache = None
                p.repaint()                 # thread-safe, coalesced

    def close(self):
        with self.cond:
            self.alive = False
            self.cond.notifyAll()
        self.pool.close_all()


def fill_from_overview(region_proc, ov, level_dims, level, ix0, iy0, rw, rh):
    """Prefill a level-`level` region with the upscaled overview so
    not-yet-loaded tiles show a coarse picture instead of black."""
    lw = level_dims[level][0]
    k = float(ov.getWidth()) / lw
    x0 = int(math.floor(ix0 * k)); y0 = int(math.floor(iy0 * k))
    x1 = int(math.ceil((ix0 + rw) * k)); y1 = int(math.ceil((iy0 + rh) * k))
    x0 = max(0, min(ov.getWidth() - 1, x0)); y0 = max(0, min(ov.getHeight() - 1, y0))
    x1 = max(x0 + 1, min(ov.getWidth(), x1)); y1 = max(y0 + 1, min(ov.getHeight(), y1))
    ov.setRoi(x0, y0, x1 - x0, y1 - y0)
    part = ov.crop()
    ov.resetRoi()
    part.setInterpolationMethod(ImageProcessor.BILINEAR)
    region_proc.insert(part.resize(rw, rh), 0, 0)


# ==============================================================
# Discovery
# ==============================================================

def discover_files(root, folder_tag, name_pattern):
    """Images under root whose folder path (relative to root) has a
    folder name containing one of the ';'-separated tags (any tag;
    case-insensitive; empty = all folders) and whose filename matches
    one of the ';'-separated glob patterns."""
    tags = [t.strip().lower() for t in (folder_tag or "").split(';')
            if t.strip()]
    pats = [p.strip().lower() for p in
            (name_pattern or DEFAULT_NAME_PATTERN).split(';') if p.strip()]
    out = []
    for dirpath, dirs, files in os.walk(root):
        keep = []
        for d in dirs:
            if d.startswith('.') or d == WORKSPACE_DIR:
                continue
            if fnp._is_mergepreviewer_export_folder(os.path.join(dirpath, d)):
                continue
            keep.append(d)
        dirs[:] = sorted(keep)
        if tags:
            rel = os.path.relpath(dirpath, root)
            parts = [] if rel == '.' else \
                [p.lower() for p in rel.replace('\\', '/').split('/')]
            if not any(t in p for t in tags for p in parts):
                continue
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext not in fnp.SUPPORTED_EXTENSIONS:
                continue
            if not any(fnmatch.fnmatch(f.lower(), p) for p in pats):
                continue
            out.append(os.path.join(dirpath, f))
    return out


class _CheckModel(DefaultTableModel):
    def getColumnClass(self, c):
        return Boolean if c == 0 else Object

    def isCellEditable(self, r, c):
        return c == 0


def select_images_dialog(root):
    """Modal discovery + checklist. Returns list of checked full paths,
    or None if cancelled. Saves the discovery settings to project.json."""
    try:
        proj = spp.load_project(root, create=True)
    except Exception as e:
        IJ.log("[WARN] project.json unreadable, using defaults: " + unicode(e))
        proj = spp.new_project(root)
    disc = proj['stage1'].get('discovery') or {}
    unchecked0 = set(disc.get('unchecked', []))

    dlg = JDialog(None, "Select images - " + os.path.basename(root), True)
    dlg.setSize(900, 620)
    dlg.setLocationRelativeTo(None)
    c = dlg.getContentPane()
    c.setLayout(BorderLayout(6, 6))

    top = JPanel(FlowLayout(FlowLayout.LEFT, 6, 4))
    top.setBorder(BorderFactory.createCompoundBorder(
        EmptyBorder(4, 6, 0, 6), BorderFactory.createTitledBorder("Find images")))
    top.add(JLabel("Folder tag (any of, ';'):"))
    tag_f = JTextField(disc.get('folder_tag', ""), 14)
    top.add(tag_f)
    top.add(JLabel("File pattern (';'):"))
    pat_f = JTextField(disc.get('name_pattern', DEFAULT_NAME_PATTERN), 22)
    top.add(pat_f)
    scan_b = JButton("Scan")
    top.add(scan_b)
    c.add(top, BorderLayout.NORTH)

    model = _CheckModel(["Use", "Folder", "File", "MB"], 0)
    table = JTable(model)
    table.setSelectionMode(ListSelectionModel.MULTIPLE_INTERVAL_SELECTION)
    table.setAutoCreateRowSorter(True)
    cm = table.getColumnModel()
    for i, w in enumerate([40, 360, 380, 60]):
        cm.getColumn(i).setPreferredWidth(w)
    c.add(JScrollPane(table), BorderLayout.CENTER)

    bottom = JPanel(BorderLayout())
    btns = JPanel(FlowLayout(FlowLayout.LEFT, 4, 4))
    btns.setBorder(BorderFactory.createTitledBorder("Check"))
    count_l = JLabel("")
    ok_row = JPanel(FlowLayout(FlowLayout.RIGHT, 4, 4))
    ok_b = JButton("Load checked")
    cancel_b = JButton("Cancel")
    ok_row.add(count_l); ok_row.add(ok_b); ok_row.add(cancel_b)
    bottom.add(btns, BorderLayout.WEST)
    bottom.add(ok_row, BorderLayout.EAST)
    c.add(bottom, BorderLayout.SOUTH)

    state = {'paths': [], 'result': None}

    def update_count():
        n = model.getRowCount()
        k = sum(1 for r in range(n) if model.getValueAt(r, 0))
        count_l.setText("%d / %d checked   " % (k, n))

    def scan():
        paths = discover_files(root, tag_f.getText(), pat_f.getText())
        state['paths'] = paths
        model.setRowCount(0)
        for p in paths:
            rel = spp.rel_path(root, p)
            try: mb = "%.1f" % (os.path.getsize(p) / 1048576.0)
            except Exception: mb = "?"
            model.addRow([Boolean(rel not in unchecked0),
                          os.path.dirname(rel) or ".",
                          os.path.basename(p), mb])
        update_count()

    def set_rows(rows, val):
        for r in rows:
            model.setValueAt(Boolean(val), r, 0)
        update_count()

    def all_rows():
        return range(model.getRowCount())

    def sel_rows():
        return [table.convertRowIndexToModel(v) for v in table.getSelectedRows()]

    for label, fn in [
            ("Check all",       lambda: set_rows(all_rows(), True)),
            ("Uncheck all",     lambda: set_rows(all_rows(), False)),
            ("Invert",          lambda: [set_rows([r], not model.getValueAt(r, 0))
                                         for r in all_rows()]),
            ("Check selected",  lambda: set_rows(sel_rows(), True)),
            ("Uncheck selected", lambda: set_rows(sel_rows(), False))]:
        b = JButton(label)
        b.addActionListener(_Action(fn))
        btns.add(b)

    class _ModelL(TableModelListener):
        def tableChanged(_s, e):
            update_count()
    model.addTableModelListener(_ModelL())

    def do_ok():
        files, unchecked = [], []
        for r in all_rows():
            p = state['paths'][r]
            if model.getValueAt(r, 0):
                files.append(p)
            else:
                unchecked.append(spp.rel_path(root, p))
        if not files:
            JOptionPane.showMessageDialog(dlg, "Check at least one image.",
                                          "Nothing selected",
                                          JOptionPane.WARNING_MESSAGE)
            return
        settings = {'folder_tag': tag_f.getText().strip(),
                    'name_pattern': pat_f.getText().strip(),
                    'unchecked': unchecked}
        def fn(p):
            p['stage1']['discovery'] = settings
        try:
            spp.update_project(root, fn)
        except Exception as e:
            IJ.log("[WARN] could not save discovery settings: " + unicode(e))
        state['result'] = files
        dlg.dispose()

    scan_b.addActionListener(_Action(scan))
    ok_b.addActionListener(_Action(do_ok))
    cancel_b.addActionListener(_Action(dlg.dispose))
    scan()
    dlg.setVisible(True)          # modal: blocks until disposed
    return state['result']


class _Action(ActionListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, e):
        try:
            self.fn()
        except Exception as ex:
            IJ.log("[ERROR] " + unicode(ex))
            IJ.log(traceback.format_exc())


class _Click(MouseAdapter):
    def __init__(self, fn):
        self.fn = fn

    def mouseClicked(self, e):
        self.fn(e)


# ==============================================================
# MainWindow methods
# ==============================================================

def _pj_init(self):
    """Before _build_ui: load project, orientation, view-space ref."""
    self.header_labels     = {}
    self.header_checkboxes = {}
    self.project_dirty     = False
    try:
        self.project = spp.load_project(self.folder_path, create=True)
    except Exception as e:
        IJ.log("[WARN] project.json unreadable, starting fresh: " + unicode(e))
        self.project = spp.new_project(self.folder_path)
    self.slide_orient = {}
    slides = self.project.get('slides', {})
    for sid in self.set_ids:
        s = slides.get(sid)
        if s:
            self.slide_orient[sid] = (int(s.get('rotation', 0)),
                                      bool(s.get('flip_h', False)))
        else:
            self.slide_orient[sid] = (0, False)
    self._recompute_ref()


def _pj_set_dims(self, sid):
    w = h = 1
    for ch, path in self.sets[sid].items():
        e = self.manifest.get(source_key(self.folder_path, path), {})
        try:
            w = max(w, int(e.get('width', '1')))
            h = max(h, int(e.get('height', '1')))
        except Exception:
            pass
    return w, h


def _pj_recompute_ref(self):
    rw = rh = 1
    for sid in self.set_ids:
        w, h = self._set_dims(sid)
        vw, vh = spp.view_size(w, h, self.slide_orient.get(sid, (0, False))[0])
        rw = max(rw, vw); rh = max(rh, vh)
    self.ref_w, self.ref_h = rw, rh


def _pj_after_ui(self):
    """After _build_ui: push orientation + saved channel params."""
    self._apply_orientations(self.set_ids)
    params = self.project['stage1'].get('channel_params', {})
    # colors the files state clearly (pseudo color, RGB split, stored LUT)
    # are the first LUT; saved project settings below take precedence
    for ch, lut in (self.parse_result.get("channel_luts") or {}).items():
        cp = self.contrast_panels.get(ch)
        if cp is not None and ch not in params and lut in LUT_BY_NAME:
            cp.lut_combo.setSelectedItem(lut)
            cp.lut_name = lut
    for ch, p in params.items():
        cp = self.contrast_panels.get(ch)
        if cp is None:
            continue
        try:
            _restore_contrast(cp, p)
        except Exception as e:
            IJ.log(u"[WARN] restore contrast {}: {}".format(ch, e))
    win = self
    for cp in self.contrast_panels.values():
        prev = cp.on_change_cb
        def make(prev_cb):
            def cb():
                win._mark_dirty()
                if prev_cb is not None:
                    prev_cb()
            return cb
        cp.on_change_cb = make(prev)
    self.project_dirty = False
    self._update_project_status()
    # Ctrl+S = Save to project (as in the other tools)
    rp = self.getRootPane()
    rp.getInputMap(JComponent.WHEN_IN_FOCUSED_WINDOW).put(
        KeyStroke.getKeyStroke("ctrl S"), "save_project")
    class _Save(AbstractAction):
        def actionPerformed(_s, e):
            win._save_project()
    rp.getActionMap().put("save_project", _Save())


def _restore_contrast(cp, p):
    mx = cp.max_val
    vmin = max(0, min(mx, int(float(p.get('min', 0)))))
    vmax = max(0, min(mx, int(float(p.get('max', mx)))))
    cp.min_slider.setValue(vmin); cp.min_field.setText(str(vmin))
    cp.max_slider.setValue(vmax); cp.max_field.setText(str(vmax))
    lut = p.get('lut')
    if lut and lut in LUT_BY_NAME:
        cp.lut_combo.setSelectedItem(lut)
        cp.lut_name = lut
    cp._apply()


def _styled(comp):
    comp.setForeground(TEXT_CLR)
    comp.setFont(Font("SansSerif", Font.PLAIN, 12))
    return comp


# ---- dark-theme widgets for the MISP bars ---------------------------

def dark_label(text):
    return _styled(JLabel(text))


def dark_button(text, fn, tip=None):
    b = JButton(text)
    b.setFocusPainted(False)
    b.setMargin(Insets(2, 6, 2, 6))
    if tip: b.setToolTipText(tip)
    b.addActionListener(_Action(fn))
    return b


def dark_check(text, on):
    cb = _styled(JCheckBox(text, on))
    cb.setBackground(HEADER_BG)
    cb.setFocusPainted(False)
    return cb


def dark_group(title, comps):
    """Titled box (thin line, header colors) around a row of controls;
    keeps its preferred height inside a horizontal BoxLayout."""
    p = JPanel(FlowLayout(FlowLayout.LEFT, 3, 0))
    p.setBackground(HEADER_BG)
    tb = BorderFactory.createTitledBorder(
        BorderFactory.createLineBorder(BORDER_CLR), title)
    tb.setTitleFont(Font("SansSerif", Font.PLAIN, 11))
    tb.setTitleColor(Color(170, 170, 170))
    p.setBorder(BorderFactory.createCompoundBorder(
        EmptyBorder(0, 0, 0, 6), tb))
    for c in comps:
        p.add(c)
    d = p.getPreferredSize()
    p.setMaximumSize(Dimension(d.width, d.height))
    return p


class WrapLayout(FlowLayout):
    """FlowLayout that reports the height of all its rows, so a panel of
    groups wraps onto more lines when the window is narrow."""
    def preferredLayoutSize(self, target):
        return self._size(target, True)

    def minimumLayoutSize(self, target):
        d = self._size(target, False)
        d.width -= self.getHgap() + 1
        return d

    def _size(self, target, pref):
        tw = target.getSize().width
        if tw <= 0 and target.getParent() is not None:
            tw = target.getParent().getSize().width
        if tw <= 0:
            tw = 1 << 30
        ins = target.getInsets()
        hg, vg = self.getHgap(), self.getVgap()
        maxw = tw - (ins.left + ins.right + hg * 2)
        w = h = rw = rh = 0
        for c in target.getComponents():
            if not c.isVisible():
                continue
            d = c.getPreferredSize() if pref else c.getMinimumSize()
            if rw > 0 and rw + hg + d.width > maxw:
                w = max(w, rw); h += rh + vg; rw = rh = 0
            if rw > 0:
                rw += hg
            rw += d.width
            rh = max(rh, d.height)
        w = max(w, rw); h += rh
        return Dimension(w + ins.left + ins.right + hg * 2,
                         h + ins.top + ins.bottom + vg * 2)


def _pj_build_north(self):
    """Left: view / set groups, wrapping onto more rows when narrow.
    Right (fixed): memory / folder, then Project + Files (same place as in
    the other tools)."""
    top_l, top_r = self._build_top_bar()
    proj_l, proj_r = self._build_project_bar()
    left = JPanel(WrapLayout(FlowLayout.LEFT, 0, 2))
    left.setBackground(HEADER_BG)
    for c in top_l + proj_l:
        left.add(c)
    right = JPanel()
    right.setLayout(BoxLayout(right, BoxLayout.Y_AXIS))
    right.setBackground(HEADER_BG)
    for items in (top_r, proj_r):
        row = JPanel(FlowLayout(FlowLayout.RIGHT, 0, 2))
        row.setBackground(HEADER_BG)
        for c in items:
            row.add(c)
        right.add(row)
    north = JPanel(BorderLayout())
    north.setBackground(HEADER_BG)
    north.setBorder(EmptyBorder(2, 8, 2, 4))
    north.add(left, BorderLayout.CENTER)
    north.add(right, BorderLayout.EAST)
    class _Rewrap(ComponentAdapter):
        def componentResized(_s, e):
            SwingUtilities.invokeLater(lambda: north.revalidate())
    left.addComponentListener(_Rewrap())
    return north


def _pj_build_project_bar(self):
    """(left groups, right groups) of the second bar (see _build_north)."""
    win = self
    self.match_field = JTextField("", 10)
    self.match_field.setToolTipText("Text contained in the set id")
    self.match_field.addActionListener(_Action(lambda: win._check_matching()))
    left = [dark_group("Check sets", [
        dark_button("All",    lambda: win._set_checks(lambda sid: True)),
        dark_button("None",   lambda: win._set_checks(lambda sid: False)),
        dark_button("Invert", lambda: win._set_checks(lambda sid: not win.set_checked[sid])),
        self.match_field,
        dark_button("Check matching", lambda: win._check_matching(),
                    "Add sets whose id contains the text")]),
            dark_group("Orient checked sets", [
        dark_button("90 CCW", lambda: win._orient_checked('ccw')),
        dark_button("90 CW",  lambda: win._orient_checked('cw')),
        dark_button("180",    lambda: win._orient_checked('180')),
        dark_button("Flip H", lambda: win._orient_checked('flip')),
        dark_button("Reset",  lambda: win._orient_checked('reset'))])]
    # same place and wording in all three tools: top right,
    # "Project" = save state + Save to project, "Files" = Export files...
    self.project_status = dark_label("")
    self.project_status.setPreferredSize(Dimension(130, 18))
    right = [dark_group("Project", [
        self.project_status,
        dark_button("Save to project", lambda: win._save_project(),
                    "Write exposure + slide orientation to .section_pipeline/project.json (Ctrl+S)")]),
             dark_group("Files (optional)", [
        dark_button("Export files...", lambda: win._open_export(),
                    "Optional: write single-channel / merged image files")])]
    return left, right


def _pj_open_export(self):
    """Export options live in their own window (opened from Files)."""
    d = getattr(self, "export_dialog", None)
    if d is None:
        d = JDialog(self, "Export images (optional)", False)
        d.getContentPane().add(self.export_bar)
        d.pack()
        d.setSize(max(900, d.getWidth()), d.getHeight())
        d.setLocationRelativeTo(self)
        self.export_dialog = d
    d.setVisible(True)
    d.toFront()


def _pj_set_checks(self, pred):
    for sid in self.set_ids:
        v = bool(pred(sid))
        self.set_checked[sid] = v
        cb = self.header_checkboxes.get(sid)
        if cb is not None:
            cb.setSelected(v)
    if self.show_checked_only:
        self._apply_set_filter()


def _pj_check_matching(self):
    txt = self.match_field.getText().strip().lower()
    if not txt:
        return
    self._set_checks(lambda sid: self.set_checked[sid] or txt in sid.lower())


def _pj_orient_checked(self, op):
    sids = [s for s in self.set_ids if self.set_checked.get(s)]
    if not sids:
        r = JOptionPane.showConfirmDialog(
            self, "No sets are checked. Apply to ALL sets?",
            "Orient", JOptionPane.OK_CANCEL_OPTION)
        if r != JOptionPane.OK_OPTION:
            return
        sids = list(self.set_ids)
    self._orient_sets(sids, op)


def _pj_orient_sets(self, sids, op):
    for sid in sids:
        rot, flip = self.slide_orient.get(sid, (0, False))
        if op == 'cw':
            rot, flip = spp.compose_orientation(rot, flip, 90, False)
        elif op == 'ccw':
            rot, flip = spp.compose_orientation(rot, flip, 270, False)
        elif op == '180':
            rot, flip = spp.compose_orientation(rot, flip, 180, False)
        elif op == 'flip':
            rot, flip = spp.compose_orientation(rot, flip, 0, True)
        elif op == 'reset':
            rot, flip = 0, False
        self.slide_orient[sid] = (rot, flip)
    self._apply_orientations(sids)
    self._mark_dirty()


def _pj_apply_orientations(self, sids):
    old = (self.ref_w, self.ref_h)
    self._recompute_ref()
    ref_changed = (self.ref_w, self.ref_h) != old
    targets = set(sids)
    panels = list(self.image_panels_all) + list(self.merged_panels_by_set.values())
    for p in panels:
        p.ref_w, p.ref_h = self.ref_w, self.ref_h
        sid = getattr(p, 'set_id', None)
        if sid in targets:
            rot, flip = self.slide_orient.get(sid, (0, False))
            p.set_orientation(rot, flip)
        elif ref_changed:
            p._render_cache = None
            p.repaint()
    for sid in sids:
        self._update_header_label(sid)


def _pj_update_header_label(self, sid):
    lbl = self.header_labels.get(sid)
    if lbl is None:
        return
    idx = self.set_ids.index(sid)
    rot, flip = self.slide_orient.get(sid, (0, False))
    lbl.setText(u"Set {}: {}{}".format(idx + 1, sid, orient_tag(rot, flip)))


def _pj_attach_header_menu(self, sid, comp):
    win = self
    menu = JPopupMenu()
    for text, op in [("Rotate 90 CCW", 'ccw'), ("Rotate 90 CW", 'cw'),
                     ("Rotate 180", '180'), ("Flip horizontal", 'flip'),
                     ("Reset orientation", 'reset')]:
        it = JMenuItem(text)
        it.addActionListener(_Action(lambda op=op: win._orient_sets([sid], op)))
        menu.add(it)
    class PopL(MouseAdapter):
        def mousePressed(_s, e):
            if e.isPopupTrigger(): menu.show(e.getComponent(), e.getX(), e.getY())
        def mouseReleased(_s, e):
            if e.isPopupTrigger(): menu.show(e.getComponent(), e.getX(), e.getY())
    comp.addMouseListener(PopL())
    comp.setToolTipText("Right-click: orientation of this set")


def _pj_mark_dirty(self):
    self.project_dirty = True
    self._update_project_status()


def _pj_update_project_status(self):
    if not hasattr(self, 'project_status'):
        return
    if self.project_dirty:
        self.project_status.setText(spp.UNSAVED_TEXT)
        self.project_status.setForeground(Color(230, 180, 80))
    else:
        exists = os.path.exists(spp.project_path(self.folder_path))
        self.project_status.setText(spp.saved_text(
            self.project.get("modified") if exists else False))
        self.project_status.setForeground(TEXT_CLR)


def _pj_save_project(self):
    params = {}
    for ch, cp in self.contrast_panels.items():
        cmin, cmax = cp._parse_fields()
        params[ch] = {'min': cmin, 'max': cmax, 'lut': cp.lut_name}
    dims = dict((sid, self._set_dims(sid)) for sid in self.set_ids)
    orient = dict(self.slide_orient)
    pr = self.parse_result
    root = self.folder_path

    def fn(p):
        spp.sync_slides(p, pr, root, dims)
        p['stage1']['channel_params'] = params
        for sid, (rot, flip) in orient.items():
            s = p['slides'].get(sid)
            if s is not None:
                s['rotation'] = rot
                s['flip_h'] = flip
    try:
        self.project = spp.update_project(root, fn)
    except Exception as e:
        IJ.log(traceback.format_exc())
        JOptionPane.showMessageDialog(self, "Save failed:\n" + unicode(e),
                                      "Save to project",
                                      JOptionPane.ERROR_MESSAGE)
        return False
    self.project_dirty = False
    self._update_project_status()
    IJ.log("[Project] saved: " + spp.project_path(root))
    return True


def _pj_confirm_close(self):
    """True = OK to close."""
    if not getattr(self, 'project_dirty', False):
        return True
    r = JOptionPane.showConfirmDialog(
        self, "Save exposure / orientation to the project before closing?",
        "Unsaved changes", JOptionPane.YES_NO_CANCEL_OPTION)
    if r == JOptionPane.CANCEL_OPTION or r == JOptionPane.CLOSED_OPTION:
        return False
    if r == JOptionPane.YES_OPTION:
        return self._save_project()
    return True


def register_project_methods(cls):
    cls._project_init          = _pj_init
    cls._project_after_ui      = _pj_after_ui
    cls._set_dims              = _pj_set_dims
    cls._recompute_ref         = _pj_recompute_ref
    cls._build_north           = _pj_build_north
    cls._build_project_bar     = _pj_build_project_bar
    cls._set_checks            = _pj_set_checks
    cls._check_matching        = _pj_check_matching
    cls._orient_checked        = _pj_orient_checked
    cls._orient_sets           = _pj_orient_sets
    cls._apply_orientations    = _pj_apply_orientations
    cls._update_header_label   = _pj_update_header_label
    cls._attach_header_menu    = _pj_attach_header_menu
    cls._mark_dirty            = _pj_mark_dirty
    cls._update_project_status = _pj_update_project_status
    cls._open_export           = _pj_open_export
    cls._save_project          = _pj_save_project
    cls._confirm_close         = _pj_confirm_close
