# ============================================================
# SectionCropper.py  -  stage 2 of the section pipeline
#
# slide images  ->  polygon section ROIs (auto-detect + review)
#               ->  per-section crops, numbered 001.. in order
#
# Reads / writes <root>/.section_pipeline/project.json (shared with
# Multichannel Image Set Previewer = stage 1) and reuses its pyramid
# workspace. Without a project, a quick setup scans the folder and
# sets default exposure.
#
# Code lives in Slide_Image_Processor_lib/ (SectionCropper*.py).
# ============================================================

import os, sys, traceback

from ij import IJ
from java.lang import Thread as JThread, Runnable
from javax.swing import JFileChooser, JOptionPane, SwingUtilities


def _setup_path():
    plugins = IJ.getDirectory("plugins")
    want = {"Slide_Image_Processor_lib": "SectionPipelineProject.py"}
    found = {}
    for root, dirs, files in os.walk(plugins):
        dirs[:] = [d for d in dirs if not d.startswith('.') and
                   d.lower() not in ('jars', 'java', 'lib', 'macros', 'luts')]
        name = os.path.basename(root)
        if name in want and want[name] in files:
            found[name] = root
    for name in want:
        if name not in found:
            raise RuntimeError("%s/%s not found under %s" % (name, want[name], plugins))
        if found[name] not in sys.path:
            sys.path.insert(0, found[name])


_setup_path()

# Fresh code on every run (Jython keeps modules between plugin runs).
import SectionPipelineProject;              reload(SectionPipelineProject)
import FileNameParser;                      reload(FileNameParser)
import MultichannelImageSetPreviewerWorkspace; reload(MultichannelImageSetPreviewerWorkspace)
import SectionCropperCore;                  reload(SectionCropperCore)
import SectionCropperCanvas;                reload(SectionCropperCanvas)
import SectionCropperExport;                reload(SectionCropperExport)
import SectionRename;                       reload(SectionRename)
import SectionCropperWindow;                reload(SectionCropperWindow)

spp  = SectionPipelineProject
fnp  = FileNameParser
ws   = MultichannelImageSetPreviewerWorkspace
core = SectionCropperCore


class _Run(Runnable):
    def __init__(self, fn):
        self.fn = fn
    def run(self):
        self.fn()


def _quick_setup(root):
    """No stage-1 project: scan the folder, register slides."""
    r = JOptionPane.showConfirmDialog(
        None, "No section-pipeline project in\n%s\n\nScan this folder and create "
        "one with default exposure?\n(For exposure / rotation / image selection, "
        "run Multichannel Image Set Previewer first and Save to project.)" % root,
        "Section Cropper", JOptionPane.OK_CANCEL_OPTION)
    if r != JOptionPane.OK_OPTION:
        return None
    res = fnp.parse_folder(root, allow_no_channel=True)
    if res['status'] == 'AMBIGUOUS':
        cands = res['ambiguous_candidates']
        opts = ["Position %d: [%s]" % (c['pos'], ", ".join(sorted(c['unique_vals'])))
                for c in cands]
        ch = JOptionPane.showInputDialog(None, "Which filename token is the channel?",
                                         "Channel position", JOptionPane.QUESTION_MESSAGE,
                                         None, opts, opts[0])
        if ch is None:
            return None
        res = fnp.parse_folder(root, forced_channel_pos=cands[opts.index(ch)]['pos'],
                               allow_no_channel=True)
    if res['status'] != 'OK':
        IJ.error("Section Cropper", "Parse failed:\n" + "\n".join(res['errors']))
        return None
    res = ws.expand_components(res, root, IJ.log)   # multi-channel files -> channels
    proj = spp.new_project(root)
    proj["stage1"]["default_luts"] = res.get("channel_luts", {})   # colors stated by the files
    spp.sync_slides(proj, res, root)
    return proj


def _parse_result(root, proj):
    sets = {}
    for sid in spp.ordered_slide_ids(proj, include_excluded=True):
        s = proj['slides'][sid]
        sets[sid] = dict((ch, spp.abs_path(root, rel)) for ch, rel in s['channels'].items())
    return {'sets': sets}


def main(root=None):
    if root is None:            # not started from the Section Pipeline launcher
        ch = JFileChooser()
        ch.setFileSelectionMode(JFileChooser.DIRECTORIES_ONLY)
        ch.setDialogTitle("Section Cropper: select the project (top) folder")
        if ch.showOpenDialog(None) != JFileChooser.APPROVE_OPTION:
            return
        root = ch.getSelectedFile().getAbsolutePath()
    proj = spp.load_project(root, create=False)
    fresh = False
    if proj is None or not [s for s in proj['slides'].values() if not s.get('missing')]:
        proj = _quick_setup(root)
        if proj is None:
            return
        fresh = True
    pr = _parse_result(root, proj)
    missing = [p for chs in pr['sets'].values() for p in chs.values()
               if not os.path.exists(fnp.real_path(p))]
    if missing:
        IJ.log("[Cropper] %d image file(s) not found:\n  %s" % (len(missing), "\n  ".join(missing[:20])))
    n = sum(len(v) for v in pr['sets'].values())
    prog = SectionCropperExport.ProgressDialog(None, "Section Cropper: preparing pyramids", n)

    class Work(Runnable):
        def run(_s):
            try:
                man = ws.prepare_workspace(root, pr, prog)
                if prog.cancelled:
                    IJ.log("[Cropper] cancelled while preparing pyramids")
                    SwingUtilities.invokeLater(_Run(prog.dispose))
                    return
                for sid, s in proj['slides'].items():
                    e = None
                    for rel in s['channels'].values():
                        e = man.get(ws.source_key(root, spp.abs_path(root, rel))) or e
                    if e and not s.get('width'):
                        s['width'], s['height'] = int(e['width']), int(e['height'])
                # default exposure for every channel without saved settings
                # (all of them on a fresh project, new ones added after step 1)
                saved = proj['stage1'].get('channel_params') or {}
                chans = set(ch for s in proj['slides'].values() for ch in s['channels'])
                if fresh or chans - set(saved):
                    prog.update(n, "Default exposure...")
                    core.auto_contrast(root, proj, man, ws)
                if fresh:
                    spp.save_project(root, proj)
                class Open(Runnable):
                    def run(__s):
                        # closing the progress hands the focus back to the
                        # window that was active before (the launcher), a
                        # moment later: bring the Cropper forward after that
                        w = SectionCropperWindow.CropperWindow(root, proj, man, ws)
                        prog.dispose()
                        SectionCropperExport.to_front_later(w)
                SwingUtilities.invokeLater(Open())
            except Exception as e:
                IJ.log(traceback.format_exc())
                class Fail(Runnable):
                    def run(__s):
                        prog.dispose()
                        IJ.error("Section Cropper", unicode(e))
                SwingUtilities.invokeLater(Fail())
    t = JThread(Work())
    t.setDaemon(True)
    t.start()
    prog.setVisible(True)


main(globals().get("LAUNCH_FOLDER"))
