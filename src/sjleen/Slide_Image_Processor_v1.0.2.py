# ============================================================
# Slide_Image_Processor_v1.0.2.py  -  launcher for the section pipeline (file name carries the version)
#
# Pick one folder, then open any stage with it:
#   1. Multichannel Image Set Previewer (exposure, slide rotation)
#   2. Section Cropper   (detect / edit / order / crop sections)
#   3. Section Orienter  (flip / rotate tags, oriented series)
#
# Under each button: what that stage has produced so far.
#   green = done, red = needed for the next stage but missing,
#   gray  = optional / not done yet.
#
# The tools themselves have no underscore in their file names, so
# they are not listed in the Fiji menu; this launcher is the entry.
# Each is run with LAUNCH_FOLDER set, so it skips its folder dialog.
# ============================================================

import os, sys, re, json, traceback, codecs
import jarray

from ij import IJ, Prefs
from java.lang import Object, Thread as JThread, Runnable
from java.awt import BorderLayout, GridLayout, Dimension, Color, Font
from java.awt.event import WindowAdapter
from javax.swing import (JFrame, JPanel, JButton, JComboBox, JLabel,
                         JFileChooser, SwingUtilities, BorderFactory, BoxLayout,
                         JScrollPane, JCheckBox)

PREF_RECENT = "sjleen.slide_image_processor.recent"
PREF_OLD    = "sjleen.section_pipeline.recent"
SKIP_L1_PREF = "sjleen.misp.pyramid_skip_l1"   # read by the pyramid builder
N_RECENT = 10

TOOLS = [("Exposure & rotation", "MultichannelImageSetPreviewer.py"),
         ("Section Cropper", "SectionCropper.py"),
         ("Section Orienter", "SectionOrienter.py")]

GREEN = Color(30, 130, 60)
RED   = Color(200, 40, 40)
GRAY  = Color(120, 120, 120)

IMG_EXT = ('.tif', '.tiff', '.jpg', '.jpeg', '.png')


# ------------------------------------------------------------
# helpers
# ------------------------------------------------------------

def find_file(name, parent=None):
    plugins = IJ.getDirectory("plugins")
    for root, dirs, files in os.walk(plugins):
        dirs[:] = [d for d in dirs if not d.startswith('.') and
                   d.lower() not in ('jars', 'java', 'lib', 'macros', 'luts')]
        if name in files and (parent is None or os.path.basename(root) == parent):
            return os.path.join(root, name)
    return None


_ws = [None]
def workspace_module():
    if _ws[0] is None:
        p = find_file("MultichannelImageSetPreviewerWorkspace.py",
                      "Slide_Image_Processor_lib")
        d = os.path.dirname(p)
        if d not in sys.path:
            sys.path.insert(0, d)
        import MultichannelImageSetPreviewerWorkspace as ws
        _ws[0] = ws
    return _ws[0]


def load_recent():
    s = Prefs.get(PREF_RECENT, "") or Prefs.get(PREF_OLD, "")
    return [p for p in s.split("|") if p]


def save_recent(folder):
    lst = [folder] + [p for p in load_recent() if p != folder]
    Prefs.set(PREF_RECENT, "|".join(lst[:N_RECENT]))
    Prefs.savePreferences()


def read_project(folder):
    pj = os.path.join(folder, ".section_pipeline", "project.json")
    if not os.path.isfile(pj):
        return None
    f = open(pj, "r")
    try:
        return json.load(f)
    finally:
        f.close()


def find_md(top, prefix, max_depth):
    """(folder, newest md) for folders at or below top that hold a
    '<prefix>*_export parameters.md'."""
    out = []
    top = os.path.abspath(top).rstrip("\\/")
    if not os.path.isdir(top):
        return out
    for d, dirs, files in os.walk(top):
        if d[len(top):].count(os.sep) >= max_depth:
            dirs[:] = []
        dirs[:] = [x for x in dirs if not x.startswith('.')]
        mds = [f for f in files if f.startswith(prefix) and f.endswith("_export parameters.md")]
        if mds:
            out.append((d, sorted(mds)[-1]))
    return out


def md_text(d, name):
    try:
        f = codecs.open(os.path.join(d, name), 'r', 'utf-8')
        try:
            return f.read()
        finally:
            f.close()
    except Exception:
        return ""


def stamp_of(name, prefix):
    m = re.match(re.escape(prefix) + r"(\d{8}_\d{6})", name)
    return m.group(1) if m else "?"


def md_when(d, md, prefix):
    s = stamp_of(md, prefix)
    if s != "?":
        return short_stamp(s)
    import time
    return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(os.path.join(d, md))))


def short_stamp(s):
    # 20261006_153012 -> 10-06 15:30
    if len(s) >= 13 and s[8] == '_':
        return "%s-%s %s:%s" % (s[4:6], s[6:8], s[9:11], s[11:13])
    return s


# ------------------------------------------------------------
# per-stage status: list of (text, color)
# ------------------------------------------------------------

def source_files(folder, proj):
    if proj and proj.get("slides"):
        files = []
        for s in proj["slides"].values():
            if s.get("missing") or s.get("excluded"):
                continue
            for rel in s.get("channels", {}).values():
                files.append(os.path.join(folder, rel))
        return files
    # no project yet: images anywhere below, as step 1 finds them (hidden
    # folders and export folders left out)
    out = []
    for d, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        if any(f.endswith("_export parameters.md") for f in files):
            dirs[:] = []
            continue
        out.extend(os.path.join(d, f) for f in files if f.lower().endswith(IMG_EXT))
    return out


def l1_missing(folder, files, man=None):
    """Images whose pyramid was built without L1."""
    ws = workspace_module()
    if man is None:
        man = ws.read_manifest(os.path.join(ws.workspace_dir(folder), ws.MANIFEST_NAME))
    n = 0
    for p in files:
        e = man.get(ws.source_key(folder, p))
        if e and e.get("l1_skipped") == "1":
            n += 1
    return n


def stage1_status(folder, proj):
    out = []
    files = source_files(folder, proj)
    if not files:
        return [("No images in folder", RED)]

    # pyramid
    try:
        ws = workspace_module()
        man = ws.read_manifest(os.path.join(ws.workspace_dir(folder), ws.MANIFEST_NAME))
        ok = 0
        for p in files:
            name = ws.source_key(folder, p)
            e = man.get(name)
            if e and ws.entry_is_complete(e, folder, name, p):
                ok += 1
        out.append(("Pyramid %d / %d images" % (ok, len(files)),
                    GREEN if ok == len(files) else RED))
        n_no = l1_missing(folder, files, man)
        if ws.skip_l1_enabled():
            out.append(("L1 not built / not used (option on)", GRAY))
        elif n_no:
            out.append(("L1 missing for %d images: step 1 builds L1 only" % n_no, RED))
    except Exception as e:
        out.append(("Pyramid: check failed (%s)" % e, GRAY))

    # project: exposure + rotation
    params = (proj or {}).get('stage1', {}).get('channel_params', {})
    if params:
        out.append(("Exposure saved: " + ", ".join(sorted(params.keys())), GREEN))
    else:
        out.append(("Exposure not saved to project", RED))
    if proj is not None:
        sl = [s for s in proj.get('slides', {}).values() if not s.get('missing')]
        n_ex = len([s for s in sl if s.get('excluded')])
        n_rot = len([s for s in sl if s.get('rotation') or s.get('flip_h')])
        out.append(("Slides %d (%d excluded), %d rotated/flipped"
                    % (len(sl) - n_ex, n_ex, n_rot), GREEN if sl else RED))

    # optional MISP exports (default output sits in the folder; a custom
    # one may be next to it)
    top = folder.rstrip("\/")
    dirs = [d for d, _m in find_md(top, "MISP__", 2)]
    parent = os.path.dirname(top)
    if parent:
        dirs += [d for d, _m in find_md(parent, "MISP__", 1)
                 if not norm(d).startswith(norm(top))]
    exps = runs(dirs, "MISP__")
    if not exps:
        out.append(("No image exports (optional)", GRAY))
    for n, (d, mds) in enumerate(exps):
        txt = md_text(d, mds[-1])
        head(out, "Export", n, d, mds, "MISP__", folder)
        m = re.search(r"## Sets exported \((\d+)\)", txt)
        if m:
            out.append(("   Sets " + m.group(1), GREEN))
        ch = re.findall(r"^- \*\*([^*]+)\*\*: min=(\S+), max=(\S+), LUT=(\S+)", txt, re.M)
        if ch:
            out.append(("   Exposure:", GREEN))
            for c in ch:
                out.append(("      %s  %s - %s  %s" % c, GREEN))
        out.extend(subdir_lines(d))
    return out


def norm(p):
    return os.path.normcase(os.path.abspath(p))


def mds_in(d, prefix):
    try:
        return sorted(f for f in os.listdir(d) if f.startswith(prefix)
                      and f.endswith("_export parameters.md"))
    except Exception:
        return []


def runs(dirs, prefix):
    """[(folder, its md files)], oldest first."""
    seen, out = set(), []
    for d in dirs:
        if norm(d) in seen:
            continue
        seen.add(norm(d))
        mds = mds_in(d, prefix)
        if mds:
            out.append((d, mds))
    out.sort(key=lambda x: os.path.getmtime(os.path.join(x[0], x[1][-1])))
    return out


def head(out, word, n, d, mds, prefix, folder):
    out.append((" ", GRAY))
    out.append(("%s %d  -  %s%s" % (word, n + 1, md_when(d, mds[-1], prefix),
                "  (%d runs here)" % len(mds) if len(mds) > 1 else ""), GREEN))
    rel = os.path.relpath(d, folder) if norm(d).startswith(norm(folder)) else d
    out.append(("   Folder: " + rel, GREEN))


def subdir_lines(d):
    subs = sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))
    if not subs:
        return []
    out = [("   Outputs:", GREEN)]
    for s in subs:
        k = s.rsplit('_', 1)
        nf = sum(len(fs) for _r, _ds, fs in os.walk(os.path.join(d, s)))   # incl. subfolders
        out.append(("      %s  %s  -  %d files" % (k[0], k[1] if len(k) > 1 else "", nf), GREEN))
    return out


def md_field(txt, name):
    m = re.search(r"\*\*" + re.escape(name) + r"\*\*: *(.*)", txt)
    return m.group(1).strip().strip('`') if m else None


def cropper_runs(folder, proj):
    """Every Section Cropper export: on disk under the folder, plus any
    recorded in the project (may sit elsewhere). Oldest first."""
    dirs = [d for d, _m in find_md(folder, "SectionCropper__", 3)]
    for r in (proj or {}).get("exports", []):
        d = os.path.abspath(os.path.join(folder, r.get("folder", "")))
        if os.path.isdir(d):
            dirs.append(d)
    dirs = [d for d in dirs if os.path.exists(os.path.join(d, "sections.csv"))]
    return runs(dirs, "SectionCropper__")


def stage2_status(folder, proj):
    out = []
    secs = []
    if proj is not None:
        secs = [s for s in proj.get('sections', {}).values() if not s.get('excluded')]
    if secs:
        out.append(("Sections %d on %d slides" % (
            len(secs), len(set(s.get('slide') for s in secs))), GREEN))
    else:
        out.append(("No sections yet", RED))
    exps = cropper_runs(folder, proj)
    if not exps:
        out.append(("No crop export yet", RED))
        return out
    for n, (d, mds) in enumerate(exps):
        txt = md_text(d, mds[-1])
        head(out, "Export", n, d, mds, "SectionCropper__", folder)
        sf = md_field(txt, "Sections")
        if sf:
            out.append(("   Sections " + sf.replace(", files written:", ",  files"),
                        RED if "CANCELLED" in sf else GREEN))
        for label, key in (("Name", "Filename template"),
                           ("Mask outside", "Mask outside padded polygon"),
                           ("Padding", "Default padding")):
            v = md_field(txt, key)
            if v:
                out.append(("   %s: %s" % (label, v), GREEN))
        out.extend(subdir_lines(d))
    if secs:
        m = re.match(r"(\d+)", md_field(md_text(exps[-1][0], exps[-1][1][-1]), "Sections") or "")
        if m and int(m.group(1)) != len(secs):
            out.insert(1, ("Sections changed since last export", RED))
        # sample names / numbering changed after files were written
        try:
            workspace_module()                  # puts the lib folder on sys.path
            import SectionRename
            stale = SectionRename.stale_exports(folder, proj)
        except Exception as e:
            stale = []
            IJ.log("[Launcher] name check failed: %s" % e)
        if stale:
            out.insert(1, ("File names out of date in %d folder(s): sample / numbering changed"
                           % len(stale), RED))
            out.insert(2, ("   (Section Cropper: Files > Update names...)", RED))
    return out


def stage3_status(folder, proj):
    out = []
    if not cropper_runs(folder, proj):
        out.append(("Needs section files from step 2", RED))
        out.append(("   (Section Cropper: Files > Export files...)", RED))
    all_done = False                 # every section validated and none turned:
    if proj is not None:             # the step-2 files are final, no output needed
        n_sec = len([s for s in proj.get('sections', {}).values() if not s.get('excluded')])
        o = proj.get('orientation', {})
        n_val = len([v for v in o.values() if v.get('validated')])
        n_mod = len([v for v in o.values() if v.get('rot') or v.get('flip_h')])
        if n_sec:
            out.append(("Validated %d / %d sections, %d re-oriented" % (n_val, n_sec, n_mod),
                        GREEN if n_val >= n_sec else GRAY))
        all_done = bool(n_sec) and n_val >= n_sec and n_mod == 0
    # outputs: inside the folder, or '<folder>_oriented_<stamp>' beside it
    top = folder.rstrip("\/")
    dirs = [d for d, _m in find_md(top, "SectionOrienter__", 3)]
    parent = os.path.dirname(top)
    if parent:
        leaf = os.path.basename(top)
        for x in os.listdir(parent):
            if x.startswith(leaf + "_oriented_"):
                dirs += [d for d, _m in find_md(os.path.join(parent, x), "SectionOrienter__", 2)]
    outs = runs(dirs, "SectionOrienter__")
    if not outs and not all_done:
        out.append(("No oriented output yet", GRAY))
    for n, (d, mds) in enumerate(outs):
        txt = md_text(d, mds[-1])
        head(out, "Output", n, d, mds, "SectionOrienter__", folder)
        src = md_field(txt, "Source")
        if src:
            out.append(("   Source: " + (os.path.relpath(src, folder)
                         if norm(src).startswith(norm(folder)) else src), GREEN))
        sf = md_field(txt, "Sections")
        if sf:
            out.append(("   Sections " + sf, GREEN))
        fw = md_field(txt, "Files written")
        if fw:
            out.append(("   Files " + fw, RED if "CANCELLED" in fw else GREEN))
        out.extend(subdir_lines(d))
    return out


def all_status(folder):
    if not folder or not os.path.isdir(folder):
        return [[("Folder not found", RED)], [], []]
    try:
        proj = read_project(folder)
    except Exception as e:
        return [[("project.json unreadable: %s" % e, RED)], [], []]
    res = []
    for fn in (stage1_status, stage2_status, stage3_status):
        try:
            res.append(fn(folder, proj))
        except Exception as e:
            IJ.log(traceback.format_exc())
            res.append([("Status check failed: %s" % e, GRAY)])
    # option off but some pyramids lack L1: warn on the later stages
    try:
        n_no = (0 if workspace_module().skip_l1_enabled()
                else l1_missing(folder, source_files(folder, proj)))
    except Exception:
        n_no = 0
    if n_no:
        warn = [("No L1 for %d images: this step runs without L1." % n_no, RED, True),
                ("To use L1, run step 1 again.", RED, True)]
        res[1] = warn + res[1]
        res[2] = warn + res[2]
    return res


# ------------------------------------------------------------
# launching
# ------------------------------------------------------------

def _titled(title):
    b = BorderFactory.createTitledBorder(title)
    return BorderFactory.createCompoundBorder(b, BorderFactory.createEmptyBorder(2, 4, 4, 4))


class _Run(Runnable):
    def __init__(self, fn):
        self.fn = fn
    def run(self):
        self.fn()


def launch(path, folder):
    def work():
        try:
            g = {'__name__': '__main__', '__file__': path,
                 'LAUNCH_FOLDER': folder}
            execfile(path, g)
        except SystemExit:
            pass
        except Exception as e:
            IJ.log(traceback.format_exc())
            IJ.error("Slide Image Processor", "%s failed:\n%s" % (os.path.basename(path), e))
    JThread(_Run(work), "SIP-" + os.path.basename(path)).start()


# ------------------------------------------------------------
# window
# ------------------------------------------------------------

class Launcher(object):
    def __init__(self):
        try:
            workspace_module()                  # puts the lib folder on sys.path
            import SectionPipelineProject
            ver = SectionPipelineProject.VERSION
        except Exception:
            ver = ""
        f = JFrame(("Slide Image Processor " + ver).strip())
        self.frame = f
        self._gen = 0
        root = JPanel(BorderLayout(0, 10))
        root.setBorder(BorderFactory.createEmptyBorder(12, 14, 12, 14))

        top = JPanel(BorderLayout(6, 0))
        top.add(JLabel("Folder"), BorderLayout.WEST)
        self.combo = JComboBox(jarray.array(load_recent(), Object))
        self.combo.setEditable(True)
        self.combo.addActionListener(lambda e: self.refresh())
        top.add(self.combo, BorderLayout.CENTER)
        top.add(JButton("Browse...", actionPerformed=lambda e: self.browse()), BorderLayout.EAST)
        north = JPanel(BorderLayout(0, 6))
        north.add(top, BorderLayout.NORTH)
        self.skip_cb = JCheckBox("Skip building and using L1  (pyramid build writes ~75% less for TIF "
                                 "sources, up to ~4x faster when the disk is the limit)",
                                 Prefs.get(SKIP_L1_PREF, "true") == "true")
        self.skip_cb.setToolTipText("L1 = half-resolution level. When skipped, views read L0 "
                                    "at that zoom. Toggling never rebuilds existing pyramids; "
                                    "with it off, step 1 builds only the missing L1.")
        def _toggle(e):
            Prefs.set(SKIP_L1_PREF, "true" if self.skip_cb.isSelected() else "false")
            Prefs.savePreferences()
            self.refresh()
        self.skip_cb.addActionListener(_toggle)
        north.add(self.skip_cb, BorderLayout.SOUTH)
        north.setBorder(_titled("Project folder"))
        root.add(north, BorderLayout.NORTH)

        cols = JPanel(GridLayout(1, len(TOOLS), 12, 0))
        self.boxes = []
        for k, (label, fname) in enumerate(TOOLS):
            col = JPanel(BorderLayout(0, 6))
            col.setBorder(_titled("Step %d" % (k + 1)))
            b = JButton(label)
            b.addActionListener(lambda e, fn=fname: self.open_tool(fn))
            col.add(b, BorderLayout.NORTH)
            box = JPanel()
            box.setLayout(BoxLayout(box, BoxLayout.Y_AXIS))
            sp = JScrollPane(box)
            sp.setBorder(None)
            sp.setPreferredSize(Dimension(300, 340))
            sp.getVerticalScrollBar().setUnitIncrement(16)
            col.add(sp, BorderLayout.CENTER)
            self.boxes.append(box)
            cols.add(col)
        root.add(cols, BorderLayout.CENTER)

        launcher = self
        class _Act(WindowAdapter):
            def windowActivated(_s, e):
                launcher.refresh()     # tools may have written new results
        f.addWindowListener(_Act())

        f.getContentPane().add(root)
        f.pack()
        f.setLocationRelativeTo(None)
        f.setVisible(True)

    def folder(self):
        v = self.combo.getEditor().getItem()
        return (unicode(v).strip().strip('"') if v is not None else u"")

    def browse(self):
        ch = JFileChooser(self.folder() or None)
        ch.setFileSelectionMode(JFileChooser.DIRECTORIES_ONLY)
        ch.setDialogTitle("Slide Image Processor: select the image (project) folder")
        if ch.showOpenDialog(self.frame) == JFileChooser.APPROVE_OPTION:
            save_recent(ch.getSelectedFile().getAbsolutePath())
            self.combo.removeAllItems()
            for p in load_recent():
                self.combo.addItem(p)
            self.combo.setSelectedIndex(0)

    def refresh(self):
        self._gen += 1
        gen = self._gen
        folder = self.folder()
        def work():
            try:
                res = all_status(folder)
            except Exception as e:
                res = [[("Status check failed: %s" % e, GRAY)], [], []]
            SwingUtilities.invokeLater(_Run(lambda: self._show(res, gen)))
        JThread(_Run(work), "SIP-status").start()

    def _show(self, res, gen):
        if gen != self._gen:        # a newer check is running
            return
        for box, lines in zip(self.boxes, res):
            box.removeAll()
            for line in lines:
                text, color = line[0], line[1]
                l = JLabel(text)
                l.setForeground(color)
                if len(line) > 2 and line[2]:      # emphasised warning
                    l.setFont(l.getFont().deriveFont(Font.BOLD))
                l.setToolTipText(text)
                box.add(l)
            box.revalidate(); box.repaint()

    def open_tool(self, fname):
        folder = self.folder()
        if not folder or not os.path.isdir(folder):
            IJ.error("Slide Image Processor", "Choose an existing folder first.")
            return
        path = find_file(fname)
        if path is None:
            IJ.error("Slide Image Processor", "%s not found under the plugins folder." % fname)
            return
        folder = os.path.abspath(folder)
        save_recent(folder)
        launch(path, folder)


SwingUtilities.invokeLater(_Run(lambda: Launcher()))
