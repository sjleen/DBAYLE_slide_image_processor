# ============================================================
# SectionOrienter.py  -  stage 3 of the section pipeline
#
# Browse a series of section images, tag sections that are flipped
# left-right or need rotation (on top of stage-1 slide rotation),
# then write a re-oriented copy of the series.
#
# Open
#   - a Section Cropper export folder (has sections.csv)  -> tags go
#     to the project (<root>/.section_pipeline/project.json)
#   - a project root with exports                          -> pick one
#   - any other image folder                               -> tags in
#     <folder>/orientation.json
#
# Code lives in Slide_Image_Processor_lib/ (SectionOrienter*.py).
# ============================================================

import os, sys, traceback

from ij import IJ
from java.lang import Runnable
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

import SectionPipelineProject;  reload(SectionPipelineProject)
import FileNameParser;          reload(FileNameParser)
import SectionCropperCore;      reload(SectionCropperCore)
import SectionCropperExport;    reload(SectionCropperExport)
import SectionOrienterCore;     reload(SectionOrienterCore)
import SectionOrienterWindow;   reload(SectionOrienterWindow)

spp = SectionPipelineProject
oc  = SectionOrienterCore


def main(base=None):
    if base is None:            # not started from the Section Pipeline launcher
        ch = JFileChooser()
        ch.setFileSelectionMode(JFileChooser.DIRECTORIES_ONLY)
        ch.setDialogTitle("Section Orienter: select a project, an export folder, or any image folder")
        if ch.showOpenDialog(None) != JFileChooser.APPROVE_OPTION:
            return
        base = ch.getSelectedFile().getAbsolutePath()
    root = proj = None
    # every Section Cropper export at or below the chosen folder, merged
    if os.path.exists(os.path.join(base, "sections.csv")):
        dirs = [base]
    else:
        dirs = oc.find_exports(base)
    staged = dirs == [base] and not oc.is_cropper_export(base)
    if staged:
        # a step-3 output (sections.csv copied, files already re-oriented):
        # never apply or overwrite the project's orientation tags here
        IJ.log("[Orienter] %s is a step-3 output: opened without project tags" % base)
    if dirs:
        root = None if staged else oc.find_project_root(dirs[0])
        proj = spp.load_project(root) if root else None
        items = oc.load_exports(proj, dirs, IJ.log)
        mode = 'project' if proj is not None else 'folder'
        if mode == 'folder':
            oc.apply_folder_tags(items, base)
        IJ.log("[Orienter] %d export folder(s): %s" % (
            len(dirs), ", ".join(os.path.relpath(d, base) for d in dirs)))
    elif oc.find_project_root(base):
        # a section-pipeline project without Cropper exports: the slide
        # images themselves are not sections (and far too big to load)
        JOptionPane.showMessageDialog(None,
            "No Section Cropper export found in\n%s\n\n"
            "In step 2 (Section Cropper) use Files > Export files... first;\n"
            "Save to project alone does not write section images." % base,
            "Section Orienter", JOptionPane.INFORMATION_MESSAGE)
        return
    else:
        items = oc.load_folder(base)
        mode = 'folder'
    if not items:
        IJ.error("Section Orienter", "No images found in\n" + base)
        return
    IJ.log("[Orienter] %s mode: %d section(s) from %s" % (mode, len(items), base))
    # loading ran on the launcher thread; Swing has to be built on the EDT
    class Open(Runnable):
        def run(_s):
            try:
                SectionOrienterWindow.OrienterWindow(items, base, mode, root, proj)
            except Exception as e:
                IJ.log(traceback.format_exc())
                IJ.error("Section Orienter", unicode(e))
    SwingUtilities.invokeLater(Open())


try:
    main(globals().get("LAUNCH_FOLDER"))
except Exception as e:
    IJ.log(traceback.format_exc())
    IJ.error("Section Orienter", unicode(e))
