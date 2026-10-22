# ============================================================
# SectionCropperExport.py  (stage 2: Section Cropper)
#
# Export options -> job list (one row per output file, sortable,
# CSV) -> export run (background, cancellable).
#
# Output folder layout
#   <out>/<kind>_<FMT>/<template>.<ext>
#   <out>/sections.csv     one row per section (sec_id <-> files)
#   <out>/jobs.csv         the job list as executed
#   <out>/SectionCropper__<stamp>_export parameters.md
#     (the '_export parameters.md' suffix makes MISP / FileNameParser
#      skip this folder when scanning for raw images)
# ============================================================

import os, csv, time, copy, threading, traceback, codecs, json

from java.awt       import (BorderLayout, FlowLayout, GridLayout, Dimension,
                            Color, Cursor, GridBagLayout, GridBagConstraints,
                            Insets, Font)
from java.awt.event import ActionListener
from java.lang      import Runnable
from javax.swing    import (JDialog, JPanel, JLabel, JButton, JCheckBox,
                            JRadioButton, ButtonGroup, JTextField, JTable,
                            JScrollPane, JProgressBar, JOptionPane,
                            JFileChooser, BorderFactory, BoxLayout,
                            SwingUtilities, Timer)
from javax.swing.table import DefaultTableModel
from javax.swing.event import DocumentListener

from ij      import IJ, ImagePlus, ImageStack, CompositeImage
from ij.io   import FileSaver
from ij.process import LUT
from ij.gui  import ShapeRoi

import SectionPipelineProject as spp
import FileNameParser as fnp
import SectionCropperCore as core


# Output combinations: channel (single|merged) x depth (orig|8bit) x
# LUT (gray|color). Key = output folder prefix; format is appended.
# Each chosen output has its own formats; original-depth outputs are TIF only.
COMBOS = [
    # key                  channel   depth   lut      tooltip
    ('single_orig_gray',  'single', 'orig', 'gray',
     "Raw pixel values, one file per channel (analysis)"),
    ('single_orig_color', 'single', 'orig', 'color',
     "Raw pixel values + channel LUT stored as metadata (opens colored in Fiji)"),
    ('single_8bit_gray',  'single', '8bit', 'gray',
     "Exposure applied, 8-bit grayscale, one file per channel"),
    ('single_8bit_color', 'single', '8bit', 'color',
     "Exposure applied, RGB in the channel color, one file per channel"),
    ('merged_orig_color', 'merged', 'orig', 'color',
     "All channels as one composite TIF, raw values, channel LUTs + ranges"),
    ('merged_8bit_color', 'merged', '8bit', 'color',
     "Exposure applied, additive RGB merge (figures / browsing)"),
]
COMBO = dict((c[0], c) for c in COMBOS)
OLD_KINDS = {'orig': 'single_orig_gray', 'gray8': 'single_8bit_gray',
             'color8': 'single_8bit_color', 'merged': 'merged_8bit_color'}
PRESETS = [   # name, {output: formats}
    ("Analysis", {"single_orig_gray": ["TIF"]}),
    ("Figure", {"single_8bit_gray": ["JPG"], "merged_8bit_color": ["JPG"]}),
    ("Archive + view", {"single_orig_gray": ["TIF"], "merged_orig_color": ["TIF"],
                        "merged_8bit_color": ["JPG"]}),
]


def combo_formats(key, formats):
    return list(formats) if COMBO[key][2] == '8bit' else ['TIF']


def allowed_formats(key):
    return list(FORMATS) if COMBO[key][2] == "8bit" else ["TIF"]


def last_outputs(rec):
    """{output: formats} of an earlier export record (also old records that
    stored kinds + one shared 8-bit format list)."""
    if rec.get("outputs"):
        return dict((k, list(v)) for k, v in rec["outputs"].items() if k in COMBO)
    kinds = [OLD_KINDS.get(k, k) for k in
             (rec.get("kinds") or ["single_orig_gray", "merged_8bit_color"])]
    ff = rec.get("formats") or ["JPG"]
    return dict((k, combo_formats(k, ff)) for k in kinds if k in COMBO)
FORMATS = ['TIF', 'JPG', 'PNG']
EXT = {'TIF': '.tif', 'JPG': '.jpg', 'PNG': '.png'}


class _Act(ActionListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, e):
        try:
            self.fn()
        except Exception as ex:
            IJ.log("[Cropper] " + unicode(ex))
            IJ.log(traceback.format_exc())


class _Run(Runnable):
    def __init__(self, fn):
        self.fn = fn

    def run(self):
        self.fn()


def on_edt(fn):
    SwingUtilities.invokeLater(_Run(fn))


class ProgressDialog(JDialog):
    """Non-modal progress with Cancel. update() is thread-safe."""

    def __init__(self, owner, title, total):
        JDialog.__init__(self, owner, title, False)
        self.cancelled = False
        self.setSize(520, 130)
        self.setLocationRelativeTo(owner)
        c = self.getContentPane()
        c.setLayout(BorderLayout(6, 6))
        self.label = JLabel("  Starting...")
        self.bar = JProgressBar(0, max(1, total))
        self.bar.setStringPainted(True)
        c.add(self.label, BorderLayout.NORTH)
        c.add(self.bar, BorderLayout.CENTER)
        b = JButton("Cancel")
        dlg = self
        def cancel():
            dlg.cancelled = True
            dlg.label.setText("  Cancelling after the current item...")
        b.addActionListener(_Act(cancel))
        row = JPanel(FlowLayout(FlowLayout.RIGHT))
        row.add(b)
        c.add(row, BorderLayout.SOUTH)
        # time left from the real work (callers: eta.set_total / eta.add)
        self.eta = spp.WorkEta()
        self._text = u"Starting..."
        self._timer = Timer(1000, _Act(self._show))
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
        def f():
            if dlg.eta.fraction() is None:
                dlg.bar.setValue(value)
            dlg._text = text
            dlg._show()
        on_edt(f)

    def dispose(self):
        self._timer.stop()
        JDialog.dispose(self)


def to_front_later(w, delays=(150, 600)):
    """Bring w to the front again after a closing dialog has handed the
    focus back to the previously active window (the launcher)."""
    for ms in delays:
        def f(w=w):
            if w.isDisplayable():
                w.toFront()
                w.requestFocus()
        t = Timer(ms, _Act(f))
        t.setRepeats(False)
        t.start()


def memory_widgets():
    """[bar, label] showing the Fiji (JVM heap) memory use, refreshed every
    second, as in step 1; the timer stops once the window is gone."""
    from java.lang import Runtime as JRuntime
    bar = JProgressBar(0, 100)
    bar.setPreferredSize(Dimension(90, 14))
    lbl = JLabel("RAM: --")
    lbl.setFont(Font("SansSerif", Font.PLAIN, 11))
    lbl.setPreferredSize(Dimension(150, 18))

    def fmt(b):                              # same wording as step 1
        return ("%.1fG" % (b / 1073741824.0)) if b >= 1073741824 else ("%.0fM" % (b / 1048576.0))
    state = {'shown': False}

    def tick():
        if not bar.isDisplayable():
            if state['shown']:               # window closed
                timer.stop()
            return
        state['shown'] = True
        rt = JRuntime.getRuntime()
        mx, used = rt.maxMemory(), rt.totalMemory() - rt.freeMemory()
        pct = int(100.0 * used / mx) if mx > 0 else 0
        bar.setValue(pct)
        lbl.setText(u"RAM: %d%%  %s / %s" % (pct, fmt(used), fmt(mx)))
    timer = Timer(1000, _Act(tick))
    timer.start()
    tick()
    return [bar, lbl]


# ==============================================================
# Planning
# ==============================================================

def _neighbour_roi(proj, sec_id, rect):
    """Union of the other (not excluded) sections' outlines reaching into
    rect, minus this section's own outline; None if there are none."""
    if rect is None:
        return None
    s = proj['sections'][sec_id]
    x, y, w, h = rect
    acc = None
    for j in spp.slide_section_ids(proj, s['slide'], include_excluded=False):
        if j == sec_id:
            continue
        q = proj['sections'][j]['polygon']
        a0, b0, a1, b1 = spp.polygon_bounds(q)
        if a0 < x + w and a1 > x and b0 < y + h and b1 > y:
            r = ShapeRoi(spp.poly_roi(q))
            acc = r if acc is None else getattr(acc, 'or')(r)   # 'or' / 'not' are keywords
    if acc is None:
        return None
    acc = getattr(acc, 'not')(ShapeRoi(spp.poly_roi(s['polygon'])))
    return acc if acc.getBounds().width > 0 else None


def scoped_rows(proj, opts):
    """Numbered sections in the export scope (all / reviewed / selected slides)."""
    rows = spp.numbered_sections(proj)
    if opts['scope'] == 'reviewed':
        rows = [r for r in rows if proj['slides'][r['slide']].get('reviewed')]
    elif opts['scope'] == 'selected':
        sel = set(opts['slides'])
        rows = [r for r in rows if r['slide'] in sel]
    return rows


def plan_jobs(win, opts):
    """Returns (jobs, sections) where sections is the list of planned
    sections with crop geometry, and jobs one row per output file."""
    proj = win.proj
    rows = scoped_rows(proj, opts)
    all_ch = win._all_channels()
    secs, jobs, seen = [], [], {}
    tmpl = opts['template']

    def slide_dir(sid):
        """The slide's subfolder below the project folder ('' = top / flat)."""
        if opts.get('flat'):
            return ""
        chs = proj['slides'][sid].get('channels') or {}
        if not chs:
            return ""
        rel = fnp.real_path(sorted(chs.values())[0]).replace("\\", "/")
        d = os.path.dirname(rel).strip("/")
        return "" if d.startswith("..") else d

    for r in rows:
        sid = r['slide']
        src = win.source(sid)
        sdir = slide_dir(sid)
        roi, pad = spp.section_roi(proj, r['sec_id'])
        rect = spp.section_crop_rect(roi, src.vw, src.vh)
        sec = dict(r)
        sec.update({'roi': roi, 'pad': pad, 'rect': rect,
                    'nb_roi': _neighbour_roi(proj, r['sec_id'], rect) if opts.get('mask_nb') else None,
                    'channels': src.channels(), 'files': []})
        secs.append(sec)
        for key, chmode, depth, lut, _tip in COMBOS:
            if not opts["outputs"].get(key):
                continue
            chans = ["Merge"] if chmode == "merged" else all_ch
            for fmt in opts["outputs"][key]:
                for ch in chans:
                    name = spp.render_filename(tmpl, r, ch, EXT[fmt])
                    rel = "%s_%s/%s%s" % (key, fmt, sdir + "/" if sdir else "", name)
                    status = "ok"
                    if rect is None:
                        status = "outside slide"
                    elif ch != 'Merge' and ch not in src.chans:
                        status = "missing channel"
                    elif rel in seen:
                        status = "name collision (row %d)" % (seen[rel] + 1)
                    elif os.path.exists(os.path.join(opts['out'], rel)):
                        status = "exists (overwrite)"
                    seen.setdefault(rel, len(jobs))
                    jobs.append({
                        'NNN': r['NNN'], 'sec_id': r['sec_id'], 'sample': r['sample'],
                        'slide': sid, 'sec': r['sec'], 'kind': key,
                        'channel': ch, 'format': fmt, 'file': rel,
                        'rect': "" if rect is None else "%d,%d,%d,%d" % rect,
                        'pad': pad, 'rotation': src.rot,
                        'flip_h': src.flip, 'status': status})
    return jobs, secs


JOB_COLS = ['NNN', 'sample', 'slide', 'sec', 'channel', 'kind', 'format',
            'rect', 'pad', 'rotation', 'flip_h', 'file', 'status', 'sec_id']


def write_csv(path, rows, cols):
    f = open(path, 'wb')
    try:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([unicode(r.get(c, "")).encode('utf-8') for c in cols])
    finally:
        f.close()


# ==============================================================
# Export run
# ==============================================================

def _save(imp, path, fmt):
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    fs = FileSaver(imp)
    if fmt == 'TIF':
        ok = fs.saveAsTiff(path)
    elif fmt == 'JPG':
        FileSaver.setJpegQuality(95)
        ok = fs.saveAsJpeg(path)
    else:
        ok = fs.saveAsPng(path)
    if not ok:
        raise IOError("could not write " + path)


def _lut(proj, ch, gray):
    _mn, _mx, name = core.channel_params(proj, ch)
    r, g, b = (255, 255, 255) if gray else core.LUT_COLORS.get(name, (255, 255, 255))
    return LUT.createLutFromColor(Color(r, g, b))


def _hyperstack(name, ips, proj, chans, gray):
    """Multi-channel (composite) ImagePlus with raw pixels, per-channel
    LUT and display range from stage-1 exposure."""
    first = ips[chans[0]]
    w, h = first.getWidth(), first.getHeight()
    st = ImageStack(w, h)
    for ch in chans:
        ip = ips[ch]
        if ip.getWidth() != w or ip.getHeight() != h:
            ip = ip.resize(w, h)
        st.addSlice(ch, ip)
    imp = ImagePlus(name, st)
    imp.setDimensions(len(chans), 1, 1)
    # gray: COLOR mode (one channel at a time, gray LUT). GRAYSCALE mode
    # would be reset to red/green LUTs when the file is reopened.
    ci = CompositeImage(imp, CompositeImage.COLOR if gray else CompositeImage.COMPOSITE)
    for k, ch in enumerate(chans):
        cmin, cmax, _ = core.channel_params(proj, ch)
        ci.setPosition(k + 1, 1, 1)
        ci.setChannelLut(_lut(proj, ch, gray))
        ci.setDisplayRange(cmin, cmax)
    ci.setPosition(1, 1, 1)
    return ci


def render_output(kind, ch, ips, proj, chans, name, cache):
    _k, chmode, depth, lut, _t = COMBO[kind]
    gray = (lut == 'gray')
    if chmode == 'merged':
        chans = [c for c in chans if c in ips]
        if depth == '8bit':
            if 'rgb' not in cache:
                cache['rgb'] = core.composite(ips, proj, chans)
            return ImagePlus(name, cache['rgb'])
        return _hyperstack(name, ips, proj, chans, gray)
    cmin, cmax, _ = core.channel_params(proj, ch)
    if depth == '8bit':
        if gray:
            return ImagePlus(name, core.to8(ips[ch], cmin, cmax))
        return ImagePlus(name, core.colorize(ips[ch], proj, ch))
    # single channel, raw values. A 1-channel CompositeImage loses its
    # LUT on save; a plain image with a processor LUT keeps it.
    ip = ips[ch].duplicate()
    if not gray:
        ip.setLut(_lut(proj, ch, False))
    imp = ImagePlus(name, ip)
    imp.setDisplayRange(cmin, cmax)
    return imp


def run_export(win, opts, jobs, secs, prog):
    proj = win.proj
    out = opts['out']
    fill = float(proj['settings']['fill_value'])
    px_um = proj['settings'].get('pixel_size_um')
    by_sec = {}
    for j in jobs:
        if j['status'] in ("ok", "exists (overwrite)"):
            by_sec.setdefault(j['sec_id'], []).append(j)
    pool = core.ReaderPool()
    done, errors, n = 0, [], len(secs)
    # work of a section: its crop area x (channels read + files written)
    def work(sec):
        r = sec.get('rect')
        return (r[2] * r[3] if r else 0) * (len(sec.get('channels') or ()) +
                                            len(by_sec.get(sec['sec_id']) or ()))
    eta = getattr(prog, 'eta', None)
    if eta:
        eta.set_total(sum(work(s) for s in secs if by_sec.get(s['sec_id'])))
    try:
        for k, sec in enumerate(secs):
            if prog.cancelled:
                break
            todo = by_sec.get(sec['sec_id'])
            if not todo:
                continue
            w = work(sec)
            prog.update(k, "%s  %s / %s  (%d/%d)" % (sec['NNN'], sec['sample'],
                                                    sec['slide'], k + 1, n))
            try:
                src = win.source(sec['slide'])
                ips = {}
                for ch in src.channels():
                    c = src.chans[ch]
                    lw, lh = c['dims'][0]
                    ip = spp.read_view_region(pool.get(c['paths'][0]), lw, lh,
                                              src.rot, src.flip, sec['rect'])
                    if opts['mask']:
                        roi = sec['roi'].clone()
                        b = roi.getBounds()
                        roi.setLocation(b.x - sec['rect'][0], b.y - sec['rect'][1])
                        ip.setValue(fill)
                        ip.fillOutside(roi)
                    if sec.get('nb_roi') is not None:
                        roi = sec['nb_roi'].clone()
                        b = roi.getBounds()
                        roi.setLocation(b.x - sec['rect'][0], b.y - sec['rect'][1])
                        ip.setValue(fill)
                        ip.fill(roi)
                    ips[ch] = ip
                cache = {}
                for j in todo:
                    path = os.path.join(out, j['file'])
                    imp = render_output(j['kind'], j['channel'], ips, proj,
                                        src.channels(), os.path.basename(path), cache)
                    if px_um:
                        cal = imp.getCalibration()
                        cal.pixelWidth = cal.pixelHeight = float(px_um)
                        cal.setUnit("micron")
                    _save(imp, path, j['format'])
                    sec['files'].append(j['file'])
                    done += 1
            except Exception as e:
                errors.append("%s: %s" % (sec['sec_id'], e))
                IJ.log("[Cropper] export %s failed: %s" % (sec['sec_id'], e))
                IJ.log(traceback.format_exc())
            if eta:
                eta.add(w)
    finally:
        pool.close()
    return done, errors


def write_roi_sets(win, opts, secs):
    """One ImageJ RoiSet .zip per slide (ROIs/[subfolder/]<slide>_sections.zip):
    each section outline, named NNN_sample, in ORIGINAL slide-image pixels
    (the step-1 rotation / flip of the slide is undone). Returns the
    number of files written."""
    from java.io import FileOutputStream, BufferedOutputStream, DataOutputStream
    from java.util.zip import ZipOutputStream, ZipEntry
    from ij.io import RoiEncoder
    from ij.gui import PolygonRoi, Roi
    from jarray import array
    proj = win.proj
    by_slide = {}
    for s in secs:
        by_slide.setdefault(s['slide'], []).append(s)
    n = 0
    for sid, ss in sorted(by_slide.items()):
        src = win.source(sid)
        rot, flip = src.rot, src.flip
        chs = proj['slides'][sid].get('channels') or {}
        sub = ""
        if chs and not opts.get('flat'):
            d = os.path.dirname(fnp.real_path(sorted(chs.values())[0]).replace("\\", "/")).strip("/")
            sub = "" if d.startswith("..") else d
        d = os.path.join(opts['out'], "ROIs", sub) if sub else os.path.join(opts['out'], "ROIs")
        if not os.path.isdir(d):
            os.makedirs(d)
        path = os.path.join(d, spp.BAD_FILE_CHARS.sub('-', unicode(sid)) + "_sections.zip")
        zos = ZipOutputStream(BufferedOutputStream(FileOutputStream(path)))
        out = DataOutputStream(BufferedOutputStream(zos))
        try:
            used = set()
            for s in ss:
                poly = proj['sections'][s['sec_id']]['polygon']
                pts = [spp.view_to_src(x, y, src.w0, src.h0, rot, flip) for x, y in poly]
                roi = PolygonRoi(array([float(p[0]) for p in pts], 'f'),
                                 array([float(p[1]) for p in pts], 'f'), Roi.POLYGON)
                name = u"%s_%s" % (s['NNN'], s['sample'] or u"NA")
                name = spp.BAD_FILE_CHARS.sub('-', name)
                while name in used:
                    name += u"_"
                used.add(name)
                roi.setName(name)
                zos.putNextEntry(ZipEntry(name + ".roi"))
                RoiEncoder(out).write(roi)
                out.flush()
        finally:
            out.close()
        n += 1
    return n


def write_records(win, opts, jobs, secs, stamp, done, errors, cancelled):
    out = opts['out']
    if not os.path.isdir(out):
        os.makedirs(out)
    if opts.get('rois') and not cancelled:
        try:
            opts['_roi_files'] = write_roi_sets(win, opts, secs)
        except Exception as e:
            errors.append("ROI sets: %s" % e)
            IJ.log(traceback.format_exc())
    write_csv(os.path.join(out, "jobs.csv"), jobs, JOB_COLS)
    srows = []
    for s in secs:
        r = s['rect'] or (0, 0, 0, 0)
        srows.append({'NNN': s['NNN'], 'sec_id': s['sec_id'], 'sample': s['sample'],
                      'slide': s['slide'], 'sec': s['sec'], 'x': r[0], 'y': r[1],
                      'w': r[2], 'h': r[3], 'pad': s['pad'],
                      'rotation': win.proj['slides'][s['slide']].get('rotation', 0),
                      'flip_h': win.proj['slides'][s['slide']].get('flip_h', False),
                      'files': ";".join(s['files'])})
    write_csv(os.path.join(out, "sections.csv"), srows,
              ['NNN', 'sec_id', 'sample', 'slide', 'sec', 'x', 'y', 'w', 'h',
               'pad', 'rotation', 'flip_h', 'files'])
    md = os.path.join(out, "SectionCropper__%s_export parameters.md" % stamp)
    f = codecs.open(md, 'w', 'utf-8')
    try:
        st = win.proj['settings']
        f.write("# Section Cropper export\n\n")
        f.write("- **Slide Image Processor**: %s\n" % spp.VERSION)
        f.write("- **Timestamp**: %s\n" % stamp)
        f.write("- **Project root**: `%s`\n" % win.root)
        f.write("- **Output**: `%s`\n" % out)
        f.write("- **Filename template**: `%s`\n" % opts['template'])
        f.write("- **Outputs**: %s\n" % "; ".join(
            "%s (%s)" % (k, ", ".join(v)) for k, v in sorted(opts["outputs"].items())))
        f.write("- **Mask outside padded polygon**: %s (fill %s)\n" % (opts['mask'], st['fill_value']))
        f.write("- **Mask neighbouring sections**: %s\n" % bool(opts.get('mask_nb')))
        f.write("- **Subfolders**: %s\n" % ("ignored (one folder per type)" if opts.get('flat')
                                            else "kept below each type folder"))
        f.write("- **ROI sets**: %s\n" % ("ROIs/ (section outlines in original slide-image pixels)"
                                          if opts.get('rois') else "no"))
        f.write("- **Default padding**: %g%% of section bbox diagonal%s\n" % (
            st['padding_frac'] * 100, ", overlap allowed" if st.get('padding_overlap') else ""))
        f.write("- **Sections**: %d, files written: %d%s\n" % (
            len(secs), done, " (CANCELLED)" if cancelled else ""))
        f.write("\n## Exposure (stage 1)\n\n")
        for ch, p in sorted(win.proj['stage1'].get('channel_params', {}).items()):
            f.write("- **%s**: min=%s max=%s LUT=%s\n" % (ch, p.get('min'), p.get('max'), p.get('lut')))
        if errors:
            f.write("\n## Errors\n\n")
            for e in errors:
                f.write("- %s\n" % e)
    finally:
        f.close()
    rec = {'stamp': stamp, 'folder': spp.rel_path(win.root, out),
           'template': opts['template'], 'n_sections': len(secs),
           'files': done, 'cancelled': cancelled,
           "outputs": dict((k, list(v)) for k, v in opts["outputs"].items()),
           "kinds": sorted(opts["outputs"].keys()),
           "formats": sorted(set(f for v in opts["outputs"].values() for f in v))}
    def fn(p):
        p.setdefault('exports', []).append(rec)
    try:
        saved = spp.update_project(win.root, fn)
        win.proj.setdefault('exports', []).append(rec)
    except Exception as e:
        IJ.log("[Cropper] could not record export in project: %s" % e)


# ==============================================================
# Dialogs
# ==============================================================

def _tgroup(title, *rows):
    """Titled box stacking rows (panels / components) left-aligned."""
    p = JPanel()
    p.setLayout(BoxLayout(p, BoxLayout.Y_AXIS))
    b = BorderFactory.createTitledBorder(title)
    p.setBorder(BorderFactory.createCompoundBorder(
        BorderFactory.createEmptyBorder(4, 0, 0, 0), b))
    for r in rows:
        r.setAlignmentX(0.0)
        p.add(r)
    p.setAlignmentX(0.0)
    return p


def _grid_rows(rows):
    """[(label, field, extra-or-None)] -> label column + stretching fields."""
    p = JPanel(GridBagLayout())
    for y, (lbl, field, extra) in enumerate(rows):
        for x, comp, wx in ((0, lbl, 0.0), (1, field, 1.0), (2, extra, 0.0)):
            if comp is None:
                continue
            g = GridBagConstraints()
            g.gridx, g.gridy = x, y
            g.insets = Insets(2, 4, 2, 4)
            g.anchor = GridBagConstraints.WEST
            g.fill = GridBagConstraints.HORIZONTAL if x == 1 else GridBagConstraints.NONE
            g.weightx = wx
            p.add(comp, g)
    return p


def open_export_dialog(win):
    proj = win.proj
    st = proj['settings']
    # previous step-2 export (step 3 adds its own records to the same list)
    recs = [r for r in proj.get('exports', []) if r.get('stage') != 3]
    last = recs[-1] if recs else {}
    dlg = JDialog(win, "Export section files", False)
    c = dlg.getContentPane()
    c.setLayout(BorderLayout())
    body = JPanel()
    body.setLayout(BoxLayout(body, BoxLayout.Y_AXIS))
    body.setBorder(BorderFactory.createEmptyBorder(4, 8, 4, 8))
    c.add(body, BorderLayout.CENTER)

    # ---- destination ----
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_f = JTextField(os.path.join(win.root, "sections_" + stamp), 40)
    browse = JButton("Browse...")
    def inside_root(path):
        # Windows paths ignore case; Jython's normcase does not lower them
        r = os.path.normpath(os.path.abspath(win.root)).lower().rstrip("\\/")
        p = os.path.normpath(os.path.abspath(path)).lower()
        return p == r or p.startswith(r + os.sep)
    OUTSIDE_MSG = ("The output folder must be inside the project folder\n%s\n\n"
                   "Step 3 (Section Orienter) only looks for section files there." % win.root)
    def do_browse():
        ch = JFileChooser(win.root)
        ch.setFileSelectionMode(JFileChooser.DIRECTORIES_ONLY)
        if ch.showOpenDialog(dlg) == JFileChooser.APPROVE_OPTION:
            picked = ch.getSelectedFile().getAbsolutePath()
            if not inside_root(picked):
                JOptionPane.showMessageDialog(dlg, OUTSIDE_MSG, "Output folder",
                                              JOptionPane.WARNING_MESSAGE)
                return
            out_f.setText(os.path.join(picked, "sections_" + stamp))
    browse.addActionListener(_Act(do_browse))
    tmpl_f = JTextField(st.get('filename_template') or spp.DEFAULT_SETTINGS['filename_template'], 30)
    warn = JLabel(" ")
    warn.setForeground(Color(190, 90, 0))
    def check_t():
        probs = spp.check_template(tmpl_f.getText())
        warn.setText(("! " + "; ".join(probs)) if probs else
                     "fields: " + " ".join("{%s}" % f for f in spp.TEMPLATE_FIELDS))
    class DL(DocumentListener):
        def insertUpdate(_s, e): check_t()
        def removeUpdate(_s, e): check_t()
        def changedUpdate(_s, e): check_t()
    tmpl_f.getDocument().addDocumentListener(DL())
    check_t()
    body.add(_tgroup("Destination", _grid_rows([
        (JLabel("Output folder"), out_f, browse),
        (JLabel("File name"), tmpl_f, None),
        (JLabel(""), warn, None)])))

    # ---- which sections ----
    grp = ButtonGroup()
    scope = {}
    srow = JPanel(FlowLayout(FlowLayout.LEFT, 6, 2))
    for key, label in [('all', "All (not excluded)"), ('reviewed', "Reviewed slides only"),
                       ('selected', "Selected slides")]:
        rb = JRadioButton(label, key == 'all')
        grp.add(rb); scope[key] = rb; srow.add(rb)
    body.add(_tgroup("Sections", srow))

    # ---- outputs: one row per output kind, one column per file format ----
    prev = last_outputs(last)
    cells = {}                       # key -> {fmt: checkbox}
    n_ch = len(win._all_channels())
    kp = JPanel(GridBagLayout())
    kp.setBorder(BorderFactory.createEmptyBorder(2, 6, 4, 6))
    def put(comp, x, y, center=False):
        g = GridBagConstraints()
        g.gridx, g.gridy = x, y
        g.insets = Insets(1, 6, 1, 6)
        g.anchor = GridBagConstraints.CENTER if center else GridBagConstraints.WEST
        kp.add(comp, g)
    for x, t in enumerate(["Channels", "Bit depth", "Colors"] + FORMATS + ["Folder(s)"]):
        h = JLabel(t)
        h.setForeground(Color(90, 90, 90))
        put(h, x, 0, 3 <= x < 3 + len(FORMATS))
    names = {'single': "Single channel", 'merged': "Merged",
             'orig': "Original", '8bit': "8-bit", 'gray': "Gray", 'color': "Color (LUT)"}
    folder_lbls = {}
    for y, (key, chmode, depth, lut, tip) in enumerate(COMBOS):
        for x, t in enumerate((names[chmode], names[depth], names[lut])):
            l = JLabel(t)
            l.setToolTipText(tip)
            put(l, x, y + 1)
        cells[key] = {}
        for i, f in enumerate(FORMATS):
            if f in allowed_formats(key):
                cb = JCheckBox("", f in prev.get(key, []))
                cb.setToolTipText("%s as %s" % (tip, f))
                cells[key][f] = cb
                put(cb, 3 + i, y + 1, True)
            else:
                na = JLabel("-")
                na.setForeground(Color(150, 150, 150))
                na.setToolTipText("Original bit depth is saved as TIF only")
                put(na, 3 + i, y + 1, True)
        fl = JLabel(" ")
        fl.setForeground(Color(90, 90, 90))
        folder_lbls[key] = fl
        put(fl, 3 + len(FORMATS), y + 1)
    g = GridBagConstraints()               # keep the table left-aligned
    g.gridx, g.gridy, g.weightx = 4 + len(FORMATS), 0, 1.0
    kp.add(JLabel(""), g)

    def chosen():
        out = {}
        for key, row in cells.items():
            ff = [f for f in FORMATS if f in row and row[f].isSelected()]
            if ff:
                out[key] = ff
        return out

    prow = JPanel(FlowLayout(FlowLayout.LEFT, 6, 2))
    prow.add(JLabel("Presets"))
    summary = JLabel(" ")

    def refresh(*_a):
        outs = chosen()
        per = 0
        for key in cells:
            ff = outs.get(key, [])
            per += (1 if COMBO[key][1] == 'merged' else n_ch) * len(ff)
            folder_lbls[key].setText(", ".join("%s_%s" % (key, f) for f in ff) or " ")
        summary.setText("Per section: %d file(s)   (%d channel(s))" % (per, n_ch))

    def preset(outs):
        for key, row in cells.items():
            for f, cb in row.items():
                cb.setSelected(f in outs.get(key, []))
        refresh()

    for label, outs in PRESETS:
        prow.add(_btn(label, lambda outs=outs: preset(outs)))
    prow.add(_btn("Clear", lambda: preset({})))
    for row in cells.values():
        for cb in row.values():
            cb.addActionListener(_Act(refresh))
    body.add(_tgroup("Outputs  (Original = raw bit depth;  8-bit = exposure applied)",
                     kp, prow, _row(summary)))
    refresh()

    mask_cb = JCheckBox("Fill outside the padded polygon with %s" % st['fill_value'], True)
    mask_cb.setToolTipText("<html>On: pixels outside the padded polygon are set to the fill value,<br>"
                           "so only this section remains.<br>Off: the whole padded bounding box "
                           "is kept as in the slide<br>(neighbouring tissue inside the rectangle stays).</html>")
    nb_cb = JCheckBox("Fill neighbouring sections with %s" % st['fill_value'],
                      bool(st.get('padding_overlap')))
    nb_cb.setToolTipText("<html>On: other sections' outlines (without their padding) that reach<br>"
                         "into this crop are set to the fill value; the background around<br>"
                         "this section stays. Useful with 'Allow overlap' paddings.</html>")
    flat_cb = JCheckBox("Ignore subfolders (one folder per type)", False)
    flat_cb.setToolTipText("<html>Off: each type folder keeps the slide's subfolder below the "
                           "project folder.<br>On: all files of a type in one folder.</html>")
    roi_cb = JCheckBox("ROI sets: section outlines as ImageJ ROIs (one .zip per slide)", False)
    roi_cb.setToolTipText("<html>Folder ROIs/: each slide's sections (outline, no padding), named "
                          "NNN_sample, in the pixel<br>coordinates of the ORIGINAL slide image "
                          "(step-1 rotation / flip undone). Open with the ROI Manager.</html>")
    body.add(_tgroup("Options", _row(mask_cb), _row(nb_cb), _row(flat_cb), _row(roi_cb)))

    info = JLabel(" ")
    info.setForeground(Color(190, 40, 40))

    def collect():
        o = {'out': out_f.getText().strip(),
             'template': tmpl_f.getText().strip(),
             'scope': [k for k, rb in scope.items() if rb.isSelected()][0],
             'slides': win._selected_slides(),
             "outputs": chosen(),
             'mask': mask_cb.isSelected(),
             'mask_nb': nb_cb.isSelected(),
             'flat': flat_cb.isSelected(),
             'rois': roi_cb.isSelected()}
        if not o["out"] or not inside_root(o["out"]):
            raise ValueError("Output folder must be inside the project folder "
                             "(step 3 looks for section files there)")
        if not o["outputs"] and not o['rois']:
            raise ValueError("Choose at least one output (tick a format or ROI sets)")
        bad = [p for p in spp.check_template(o['template']) if p.startswith("unknown")]
        if bad:
            raise ValueError("; ".join(bad))
        return o

    planned = {}       # last plan, reused while options and sections are unchanged

    def plan_then(o, then):
        """Plan on a worker thread (padded crops of every section take a
        while); then(jobs, secs) runs on the EDT. Preview -> Export reuses
        the same plan."""
        key = json.dumps([o, win.proj.get('sections'), win.proj.get('settings'),
                          win.proj.get('slides')], sort_keys=True, default=unicode)
        if planned.get('key') == key:
            then(*planned['val'])
            return
        prog = ProgressDialog(dlg, "Planning the export", 1)
        prog.bar.setIndeterminate(True)
        prog.label.setText("  Computing the crop of every section...")
        for b in action_buttons:
            b.setEnabled(False)

        def work():
            res, err = None, None
            try:
                res = plan_jobs(win, o)
            except Exception as e:
                IJ.log(traceback.format_exc())
                err = e
            def fin():
                prog.dispose()
                for b in action_buttons:
                    b.setEnabled(True)
                if err is not None:
                    info.setText(u"Planning failed: %s" % err)
                elif not prog.cancelled:
                    planned['key'], planned['val'] = key, res
                    then(*res)
            on_edt(fin)
        t = threading.Thread(target=work)
        t.setDaemon(True)
        t.start()
        prog.setVisible(True)

    def preview():
        try:
            o = collect()
        except ValueError as e:
            info.setText(unicode(e)); return
        if not o['outputs']:
            info.setText("ROI sets only: no image files to list")
            return
        plan_then(o, lambda jobs, secs: show_job_table(win, o, jobs, secs, dlg))

    def export_now():
        try:
            o = collect()
        except ValueError as e:
            info.setText(unicode(e)); return
        if not o['outputs']:      # ROI sets only: outlines, no crops to plan
            export_rois_only(win, o, dlg)
            return
        def go(jobs, secs):
            planned.clear()       # files appear now: the next plan must see them
            start_export(win, o, jobs, secs, dlg)
        plan_then(o, go)

    action_buttons = [_btn("Preview job list", preview), _btn("Export", export_now)]

    south = JPanel(BorderLayout())
    south.setBorder(BorderFactory.createEmptyBorder(4, 12, 8, 8))
    south.add(info, BorderLayout.CENTER)
    south.add(_row(action_buttons[0], action_buttons[1],
                   _btn("Close", dlg.dispose)), BorderLayout.EAST)
    c.add(south, BorderLayout.SOUTH)
    dlg.pack()
    dlg.setLocationRelativeTo(win)
    dlg.setVisible(True)


def start_export(win, o, jobs, secs, parent):
    bad = [j for j in jobs if j['status'].startswith("name collision")]
    if bad:
        JOptionPane.showMessageDialog(parent, "%d file name collision(s). Add "
                                      "{NNN} / {channel} to the template." % len(bad))
        return
    nover = sum(1 for j in jobs if j['status'].startswith("exists"))
    # ask only when existing files would be replaced
    if nover and JOptionPane.showConfirmDialog(
            parent, "%d existing file(s) in\n%s\nwill be overwritten." % (nover, o['out']),
            "Export", JOptionPane.OK_CANCEL_OPTION) != JOptionPane.OK_OPTION:
        return
    if win.dirty and not win.save():
        return
    st = win.proj['settings']
    if st.get('filename_template') != o['template']:
        st['filename_template'] = o['template']
        win.save()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prog = ProgressDialog(win, "Exporting sections", len(secs))
    def work():
        t0 = time.time()
        done, errors = run_export(win, o, jobs, secs, prog)
        cancelled = prog.cancelled
        try:
            write_records(win, o, jobs, secs, stamp, done, errors, cancelled)
        except Exception as e:
            errors.append("records: %s" % e)
            IJ.log(traceback.format_exc())
        def fin():
            prog.dispose()
            nr = o.get('_roi_files')
            JOptionPane.showMessageDialog(
                win, "%s: %d image file(s)%s in %.0f s%s\n%s" % (
                    "Cancelled" if cancelled else "Export finished", done,
                    (", %d ROI set(s)" % nr) if nr else "",
                    time.time() - t0,
                    ("\n%d error(s), see Log" % len(errors)) if errors else "",
                    o['out']))
        on_edt(fin)
    t = threading.Thread(target=work)
    t.setDaemon(True)
    t.start()
    prog.setVisible(True)


def export_rois_only(win, o, parent):
    """ROI sets without image files: section outlines only, no crop
    planning (fast). A small md in ROIs/ records it; the folder does not
    become a step-2 export (no sections.csv)."""
    if win.dirty and not win.save():
        return
    t0 = time.time()
    rows = scoped_rows(win.proj, o)
    if not rows:
        JOptionPane.showMessageDialog(parent, "No sections in the export scope.", "ROI sets",
                                      JOptionPane.INFORMATION_MESSAGE)
        return
    try:
        n = write_roi_sets(win, o, rows)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        f = codecs.open(os.path.join(o['out'], "ROIs", "ROI sets %s.md" % stamp), 'w', 'utf-8')
        try:
            f.write(u"# Section Cropper ROI sets\n\n- **Slide Image Processor**: %s\n"
                    u"- **Timestamp**: %s\n- **Project root**: `%s`\n- **Sections**: %d on %d slide(s)\n"
                    u"- Outlines (no padding) in original slide-image pixels, named NNN_sample\n"
                    % (spp.VERSION, stamp, win.root, len(rows), n))
        finally:
            f.close()
    except Exception as e:
        IJ.log(traceback.format_exc())
        JOptionPane.showMessageDialog(parent, u"ROI sets failed: %s" % e, "ROI sets",
                                      JOptionPane.ERROR_MESSAGE)
        return
    JOptionPane.showMessageDialog(
        parent, u"ROI sets written: %d slide(s), %d section(s) in %.1f s\n%s" % (
            n, len(rows), time.time() - t0, os.path.join(o['out'], "ROIs")),
        "ROI sets", JOptionPane.INFORMATION_MESSAGE)


class _ROModel(DefaultTableModel):
    def isCellEditable(self, r, c):
        return False


def show_job_table(win, o, jobs, secs, parent):
    dlg = JDialog(parent, "Job list - %d file(s), %d section(s)" % (len(jobs), len(secs)), False)
    dlg.setSize(1250, 650)
    dlg.setLocationRelativeTo(parent)
    model = _ROModel(JOB_COLS, 0)
    for j in jobs:
        model.addRow([j.get(c, "") for c in JOB_COLS])
    t = JTable(model)
    t.setAutoCreateRowSorter(True)
    widths = {'NNN': 44, 'sample': 70, 'slide': 110, 'sec': 30, 'channel': 60,
              'kind': 120, 'format': 46, 'rect': 140, 'pad': 40, 'rotation': 50,
              'flip_h': 44, 'file': 330, 'status': 150, 'sec_id': 80}
    for i, ccol in enumerate(JOB_COLS):
        t.getColumnModel().getColumn(i).setPreferredWidth(widths.get(ccol, 60))
    c = dlg.getContentPane()
    c.setLayout(BorderLayout())
    c.add(JScrollPane(t), BorderLayout.CENTER)
    counts = {}
    for j in jobs:
        k = j['status'].split(' (')[0]
        counts[k] = counts.get(k, 0) + 1
    summary = JLabel("  " + "   ".join("%s: %d" % kv for kv in sorted(counts.items())))
    def save_csv():
        ch = JFileChooser(win.root)
        ch.setSelectedFile(java_file(os.path.join(win.root, "job_list.csv")))
        if ch.showSaveDialog(dlg) == JFileChooser.APPROVE_OPTION:
            write_csv(ch.getSelectedFile().getAbsolutePath(), jobs, JOB_COLS)
    bottom = JPanel(BorderLayout())
    bottom.add(summary, BorderLayout.CENTER)
    bottom.add(_row(_btn("Save CSV...", save_csv),
                    _btn("Export these", lambda: (dlg.dispose(),
                                                  start_export(win, o, jobs, secs, parent))),
                    _btn("Close", dlg.dispose)), BorderLayout.EAST)
    c.add(bottom, BorderLayout.SOUTH)
    dlg.setVisible(True)


def java_file(p):
    from java.io import File
    return File(p)


def _row(*comps):
    p = JPanel(FlowLayout(FlowLayout.LEFT, 5, 3))
    for x in comps:
        p.add(x)
    return p


def _btn(text, fn):
    b = JButton(text)
    b.addActionListener(_Act(fn))
    return b
