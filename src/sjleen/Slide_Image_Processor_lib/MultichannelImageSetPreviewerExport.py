# ============================================================
# MultichannelImageSetPreviewerExport.py
#
# Export logic split out of MultichannelImageSetPreviewer_.py to keep each
# file below the Jython parser's BufferedReader mark limit.
#
# This file is loaded from MultichannelImageSetPreviewer_.py via execfile().
# It assumes MainWindow and helper names are already defined
# in the enclosing scope.
#
# After loading, call:  register_export_methods(MainWindow)
# which attaches all _on_merge / _do_export / etc. methods to
# MainWindow.
# ============================================================

import os, datetime, traceback
import codecs
from jarray import zeros

from java.awt  import Color
from java.lang import Runnable, Thread as JThread, System as JSystem
from javax.swing import JOptionPane, SwingUtilities

from ij         import IJ, ImagePlus
from ij.io      import FileSaver
from ij.process import ColorProcessor


# Raised inside worker functions when cancel flag is detected,
# so the outer loop can cleanly break out.
class _ExportCancelled(Exception):
    pass


# ---------------------------------------------------------------
def _ex_on_merge(self):
    """Run export based on current UI settings. Background thread."""
    sc_on = self.sc_enable_cb.isSelected()
    mg_on = self.mg_enable_cb.isSelected()

    sc_opts = []
    if sc_on:
        if self.sc_orig_gray.isSelected():  sc_opts.append(('orig', 'gray'))
        if self.sc_orig_color.isSelected(): sc_opts.append(('orig', 'color'))
        if self.sc_8bit_gray.isSelected():  sc_opts.append(('8bit', 'gray'))
        if self.sc_8bit_color.isSelected(): sc_opts.append(('8bit', 'color'))

    mg_opts = []
    if mg_on:
        if self.mg_cb_8bit.isSelected(): mg_opts.append('8bit')
        if self.mg_cb_orig.isSelected(): mg_opts.append('orig')

    fmts = []
    if self.fmt_tif.isSelected(): fmts.append('TIF')
    if self.fmt_jpg.isSelected(): fmts.append('JPG')
    if self.fmt_png.isSelected(): fmts.append('PNG')

    if not fmts:
        JOptionPane.showMessageDialog(self,
            "Select at least one output format.",
            "Nothing to export", JOptionPane.WARNING_MESSAGE)
        return
    if not sc_opts and not mg_opts:
        JOptionPane.showMessageDialog(self,
            "Enable Single Channel and/or Merged, and select at "
            "least one option.",
            "Nothing to export", JOptionPane.WARNING_MESSAGE)
        return

    if self.ws_rb_checked.isSelected():
        target_sets = [sid for sid in self.set_ids
                       if self.set_checked.get(sid, False)]
        if not target_sets:
            JOptionPane.showMessageDialog(self,
                "No sets are checked. Check sets or choose 'All'.",
                "Nothing to export", JOptionPane.WARNING_MESSAGE)
            return
    else:
        target_sets = list(self.set_ids)

    ch_params = {}
    for ch in self.channels:
        ch_params[ch] = self.contrast_panels[ch].current_params()
    self._ex_flat = bool(getattr(self, "flat_cb", None) and self.flat_cb.isSelected())

    base_out = self.path_field.getText().strip()
    if not base_out:
        base_out = os.path.join(self.folder_path, "merged_output")
    stamp    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = os.path.basename(base_out)
    parent   = os.path.dirname(base_out)
    if not parent:
        parent = self.folder_path
    folder_name = u"{}_{}".format(base_dir, stamp)
    out_root    = os.path.join(parent, folder_name)

    # Total task count for progress (skip unsupported combos)
    def _sc_ok(depth, lut_mode, fmt):
        # JPG only supports 8-bit
        if depth == 'orig' and fmt == 'JPG':
            return False
        # orig+color needs TIF (uses composite metadata)
        if depth == 'orig' and lut_mode == 'color' and fmt != 'TIF':
            return False
        return True
    def _mg_ok(depth, fmt):
        if depth == 'orig' and fmt != 'TIF':
            return False
        return True
    total = 0
    for sid in target_sets:
        for (d, lm) in sc_opts:
            for f in fmts:
                if _sc_ok(d, lm, f):
                    total += len(self.channels)
        for d in mg_opts:
            for f in fmts:
                if _mg_ok(d, f):
                    total += 1
    if total == 0:
        return

    progress = ProgressDialog("Exporting", total, cancellable=True)
    progress.setVisible(True)

    win = self
    class Runner(Runnable):
        def run(_self):
            win._export_errors = []
            try:
                win._do_export(out_root, folder_name, stamp,
                               target_sets, sc_opts, mg_opts,
                               fmts, ch_params, progress)
            except Exception as ex:
                IJ.log("[ERROR] export: " + unicode(ex))
                win._export_errors.append(unicode(ex))
                IJ.log(traceback.format_exc())
            finally:
                class Closer(Runnable):
                    def run(__self):
                        was_cancelled = progress.is_cancelled()
                        progress.dispose()
                        if was_cancelled:
                            JOptionPane.showMessageDialog(win,
                                "Export cancelled.\n"
                                "Partially written files may remain in:\n"
                                + out_root,
                                "Cancelled",
                                JOptionPane.WARNING_MESSAGE)
                        elif win._export_errors:
                            errs = win._export_errors
                            JOptionPane.showMessageDialog(win,
                                u"Export finished with %d error(s) (see the Log window).\n"
                                u"First: %s\nOutput: %s" % (len(errs), errs[0], out_root),
                                "Done with errors", JOptionPane.WARNING_MESSAGE)
                        else:
                            JOptionPane.showMessageDialog(win,
                                "Export finished.\nOutput: " + out_root,
                                "Done",
                                JOptionPane.INFORMATION_MESSAGE)
                SwingUtilities.invokeLater(Closer())
    t = JThread(Runner())
    t.setDaemon(True)
    t.start()


# ---------------------------------------------------------------
def _ex_do_export(self, out_root, folder_name, stamp,
                  target_sets, sc_opts, mg_opts, fmts,
                  ch_params, progress):
    """Actual export work, runs in background thread."""
    if getattr(self, "_export_errors", None) is None:
        self._export_errors = []
    if not os.path.exists(out_root):
        os.makedirs(out_root)

    # Unique prefix for every output file this run.  Lets the user
    # Explorer-search 'MISP__' (optionally plus timestamp) to collect
    # all files produced across runs.
    file_prefix = u"MISP__{}__".format(stamp)
    self._ex_used = {}                  # flat mode: names already written

    done = 0
    cancelled = False

    # time left from the pixels each output reads and writes
    def _px(sid, ch):
        p = self.sets[sid].get(ch)
        e = self.manifest.get(source_key(self.folder_path, p), {}) if p else {}
        try:
            return int(e.get('width', 0)) * int(e.get('height', 0))
        except ValueError:
            return 0
    def _single_ok(depth, lut_mode, fmt):
        return not (depth == 'orig' and (fmt == 'JPG' or (lut_mode == 'color' and fmt != 'TIF')))
    eta = getattr(progress, 'eta', None)
    if eta:
        work = 0
        for (depth, lut_mode) in sc_opts:
            for fmt in fmts:
                if _single_ok(depth, lut_mode, fmt):
                    work += sum(_px(s, c) for s in target_sets for c in self.channels)
        for depth in mg_opts:
            for fmt in fmts:
                if depth != 'orig' or fmt == 'TIF':
                    work += sum(_px(s, c) for s in target_sets for c in self.channels)
        eta.set_total(work)

    for (depth, lut_mode) in sc_opts:
        if progress.is_cancelled(): cancelled = True; break
        for fmt in fmts:
            if progress.is_cancelled(): cancelled = True; break
            if depth == 'orig' and fmt == 'JPG':
                IJ.log("[SKIP] single orig+JPG not supported; "
                       "lut_mode=" + lut_mode)
                continue
            if (depth == 'orig' and lut_mode == 'color'
                    and fmt != 'TIF'):
                IJ.log("[SKIP] single orig+color+{} not supported "
                       u"(TIF only).".format(fmt))
                continue
            sub = os.path.join(out_root,
                u"single_{}_{}_{}".format(depth, lut_mode, fmt))
            if not os.path.exists(sub):
                os.makedirs(sub)
            for sid in target_sets:
                if progress.is_cancelled(): cancelled = True; break
                ch_map = self.sets[sid]
                for ch in self.channels:
                    if progress.is_cancelled(): cancelled = True; break
                    if ch not in ch_map:
                        done += 1
                        progress.update(done,
                            u"Skipping missing {} / {}".format(sid, ch))
                        continue
                    src_name = os.path.basename(ch_map[ch])
                    try:
                        self._export_single(
                            src_name, sid, ch, depth, lut_mode, fmt,
                            ch_params[ch], sub, file_prefix, progress)
                    except _ExportCancelled:
                        cancelled = True; break
                    except Exception as ex:
                        IJ.log(u"[ERROR] single {}/{}: {}".format(
                            sid, ch, ex))
                        self._export_errors.append(u"single %s/%s: %s" % (sid, ch, ex))
                    if eta:
                        eta.add(_px(sid, ch))
                    done += 1
                    progress.update(done,
                        u"single/{}/{}: {}".format(depth, lut_mode, src_name))
                if cancelled: break
            if cancelled: break
        if cancelled: break

    if not cancelled:
        for depth in mg_opts:
            if progress.is_cancelled(): cancelled = True; break
            for fmt in fmts:
                if progress.is_cancelled(): cancelled = True; break
                if depth == 'orig' and fmt != 'TIF':
                    IJ.log("[SKIP] merged orig+{} not supported "
                           u"(TIF only).".format(fmt))
                    continue
                sub = os.path.join(out_root,
                    u"merged_{}_{}".format(depth, fmt))
                if not os.path.exists(sub):
                    os.makedirs(sub)
                for sid in target_sets:
                    if progress.is_cancelled(): cancelled = True; break
                    try:
                        self._export_merged(sid, depth, fmt,
                                            ch_params, sub,
                                            file_prefix, progress)
                    except _ExportCancelled:
                        cancelled = True; break
                    except Exception as ex:
                        IJ.log(u"[ERROR] merged {}: {}".format(sid, ex))
                        self._export_errors.append(u"merged %s: %s" % (sid, ex))
                    if eta:
                        eta.add(sum(_px(sid, c) for c in self.channels))
                    done += 1
                    progress.update(done,
                        u"merged/{}: set {}".format(depth, sid))
                if cancelled: break
            if cancelled: break

    if cancelled:
        IJ.log("[INFO] Export cancelled by user.")

    try:
        md_name = file_prefix + folder_name + "_export parameters.md"
        md_path = os.path.join(out_root, md_name)
        self._write_export_md(md_path, stamp, out_root,
                              target_sets, sc_opts, mg_opts,
                              fmts, ch_params)
    except Exception as ex:
        IJ.log("[WARN] md write failed: " + unicode(ex))


# ---------------------------------------------------------------
def _ex_load_full_channel(self, ch, sid):
    """Load full-res (level 0) image for one channel of one set."""
    ch_map = self.sets[sid]
    name   = source_key(self.folder_path, ch_map[ch])
    level0 = workspace_level_path(self.folder_path, name, 0)

    # own reader: this runs on the export thread, the shared pool belongs
    # to the viewer
    reader = ImageReader()
    try:
        reader.setId(level0)
        pt   = reader.getPixelType()
        le   = reader.isLittleEndian()
        proc = read_plane(reader)          # strips: planes may exceed 2 GiB
    finally:
        reader.close()
    # Stage-1 slide orientation (project bar)
    rot, flip = getattr(self, "slide_orient", {}).get(sid, (0, False))
    if rot or flip:
        proc = spp.orient_processor(proc, rot, flip)
    return proc, pt, le


# ---------------------------------------------------------------
def _ex_colorize_byte(self, p8, color):
    """ByteProcessor 8-bit gray + Color -> ColorProcessor (RGB),
    (v * c) // 255 per component (Java ops, see colorize8)."""
    return colorize8(p8, color)


# ---------------------------------------------------------------
def _ex_out_path(win, sub_dir, sid, name, ch=None):
    """Output path inside a kind folder. Default: the source's subfolder
    (relative to the loaded folder) is kept below it. win._ex_flat: all in
    the kind folder; a name already used by another source then gets that
    source's subfolder in front."""
    m = win.sets[sid]
    src = m.get(ch) if ch else (m.get(win.channels[0]) or list(m.values())[0])
    rel = os.path.relpath(os.path.dirname(os.path.abspath(real_path(src))),
                          os.path.abspath(win.folder_path))
    rel = "" if rel == "." or rel.startswith("..") else rel
    if not getattr(win, "_ex_flat", False):
        d = os.path.join(sub_dir, rel) if rel else sub_dir
        if not os.path.isdir(d):
            os.makedirs(d)
        return os.path.join(d, name)
    used = win.__dict__.setdefault("_ex_used", {})
    p = os.path.join(sub_dir, name)
    who = (sid, ch)
    if used.get(p, who) != who and rel:
        p = os.path.join(sub_dir, rel.replace("\\", "/").replace("/", "-") + "_" + name)
    used[p] = who
    return p


# ---------------------------------------------------------------
def _ex_save_image(self, imp, path, fmt):
    fs = FileSaver(imp)
    ok = True
    if fmt == 'TIF':
        ok = fs.saveAsTiff(path)
    elif fmt == 'JPG':
        FileSaver.setJpegQuality(95)     # same JPG quality in all three steps
        ok = fs.saveAsJpeg(path)
    elif fmt == 'PNG':
        ok = fs.saveAsPng(path)
    if not ok:
        raise IOError(u"could not write " + path)


# ---------------------------------------------------------------
def _ex_export_single(self, src_name, sid, ch, depth, lut_mode, fmt,
                      params, sub_dir, file_prefix="", progress=None):
    """Export one single-channel file."""
    if progress is not None and progress.is_cancelled():
        raise _ExportCancelled()

    cmin = float(params['min']); cmax = float(params['max'])
    lut_name = params['lut']

    if depth == 'orig' and lut_mode == 'color' and fmt != 'TIF':
        IJ.log("[SKIP] single orig+color+{} not supported "
               u"(TIF only).".format(fmt))
        return

    stem = file_stem(src_name)          # "a_C2" for one channel of a multi-channel file
    out_name = u"{}{}_{}_{}.{}".format(
        file_prefix, stem, depth, lut_mode, fmt.lower())
    out_path = _ex_out_path(self, sub_dir, sid, out_name, ch)

    proc, pt, le = self._load_full_channel(ch, sid)
    proc.setMinAndMax(cmin, cmax)

    if progress is not None and progress.is_cancelled():
        raise _ExportCancelled()

    if depth == '8bit':
        p8 = scaled8(proc, cmin, cmax)          # also right for 8-bit sources
        if lut_mode == 'gray':
            out_imp = ImagePlus(out_name, p8)
        else:
            color = LUT_BY_NAME.get(lut_name, Color.WHITE)
            out_imp = ImagePlus(out_name,
                self._colorize_byte(p8, color))
        self._save_image(out_imp, out_path, fmt)
        out_imp.close()
    else:
        if lut_mode == 'gray':
            out_imp = ImagePlus(out_name, proc)
            self._save_image(out_imp, out_path, fmt)
            out_imp.close()
        else:
            color = LUT_BY_NAME.get(lut_name, Color.WHITE)
            _save_single_orig_composite(
                out_name, out_path, proc, color, cmin, cmax)

    # If cancelled during this file's save, remove the partial output
    if progress is not None and progress.is_cancelled():
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
                IJ.log("[CANCEL] removed partial: " + out_path)
        except Exception:
            pass
        raise _ExportCancelled()

    JSystem.gc()


# ---------------------------------------------------------------
def _save_single_orig_composite(out_name, out_path, proc, color,
                                 cmin, cmax):
    """Save a single-channel image preserving original bit depth,
    with a gradient LUT (black -> color) applied as ImageJ metadata
    so that Fiji displays it colored while keeping raw pixel data."""
    # A 1-channel CompositeImage loses its LUT when saved; a plain
    # image with a processor LUT keeps it (verified 2026-10-04).
    from ij.process import LUT

    ip = proc.duplicate()
    ip.setLut(LUT.createLutFromColor(color))
    imp = ImagePlus(out_name, ip)
    imp.setDisplayRange(cmin, cmax)
    FileSaver(imp).saveAsTiff(out_path)
    imp.close()


# ---------------------------------------------------------------
# ---------------------------------------------------------------
def _ex_build_merge_filename(self, sid, suffix, ext):
    """Generate merged output filename. Uses build_output_filename
    from FileNameParser if available (which replaces channel token
    with 'Merge'). Falls back to stripping channel from first file."""
    ch_map = self.sets.get(sid, {})
    present = [ch for ch in self.channels if ch in ch_map]
    if not present:
        return u"Merge_{}{}".format(sid, ext)

    try:
        token_info = self.parse_result.get('token_info', {})
        if sid in token_info and 'build_output_filename' in globals():
            return build_output_filename(
                sid, token_info, ext, suffix=suffix)
    except Exception as ex:
        IJ.log("[WARN] build_output_filename failed: " + unicode(ex))

    stem = os.path.splitext(
        os.path.basename(real_path(ch_map[present[0]])))[0]
    # Try to remove channel token (present[0]) from stem
    for ch in present:
        stem = stem.replace("_" + ch, "")
        stem = stem.replace(ch + "_", "")
    return u"{}_Merge{}{}".format(stem, suffix, ext)


# ---------------------------------------------------------------
def _ex_export_merged(self, sid, depth, fmt, ch_params, sub_dir,
                      file_prefix="", progress=None):
    """Export a merged image."""
    def _ck():
        if progress is not None and progress.is_cancelled():
            raise _ExportCancelled()

    _ck()

    if depth == 'orig' and fmt != 'TIF':
        IJ.log(u"[SKIP] merged orig+{} not supported (TIF only).".format(fmt))
        return

    ch_map = self.sets[sid]
    present = [ch for ch in self.channels if ch in ch_map]
    if not present:
        return

    if depth == 'orig':
        _export_merged_orig_composite(self, sid, present, ch_map,
                                       ch_params, sub_dir, fmt,
                                       file_prefix, progress)
        return

    # ------------- 8-bit additive RGB path -----------------------
    max_w = 0; max_h = 0
    srcs  = []
    for ch in present:
        _ck()
        proc, pt, le = self._load_full_channel(ch, sid)
        w = proc.getWidth(); h = proc.getHeight()
        if w > max_w: max_w = w
        if h > max_h: max_h = h
        srcs.append((ch, proc, pt, le, w, h))

    # Channels tinted ((v * c) // 255) and added with saturation at 255,
    # each placed at the top-left like before (Java ops, see add_tinted).
    acc = new_rgb_acc(max_w, max_h)
    for (ch, proc, pt, le, w, h) in srcs:
        _ck()
        p = ch_params[ch]
        cmin = float(p['min']); cmax = float(p['max'])
        color = LUT_BY_NAME.get(p['lut'], Color.WHITE)
        proc.setMinAndMax(cmin, cmax)
        add_tinted(acc, scaled8(proc, cmin, cmax), color)
    _ck()
    cp = rgb_from(acc)

    out_name = file_prefix + self._build_merge_filename(
        sid, "_" + depth, "." + fmt.lower())
    out_path = _ex_out_path(self, sub_dir, sid, out_name)

    _ck()
    out_imp = ImagePlus(out_name, cp)
    self._save_image(out_imp, out_path, fmt)
    out_imp.close()

    # If cancellation snuck in during save, delete partial output
    if progress is not None and progress.is_cancelled():
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
                IJ.log("[CANCEL] removed partial: " + out_path)
        except Exception:
            pass
        srcs = None
        JSystem.gc()
        raise _ExportCancelled()

    srcs = None
    JSystem.gc()


# ---------------------------------------------------------------
def _export_merged_orig_composite(self_win, sid, present, ch_map,
                                    ch_params, sub_dir, fmt,
                                    file_prefix="", progress=None):
    """Build a multi-channel CompositeImage preserving per-channel
    original bit-depth data, attach an appropriate LUT to each
    channel, set display range, and save as TIFF."""
    def _ck():
        if progress is not None and progress.is_cancelled():
            raise _ExportCancelled()

    from ij       import ImageStack, CompositeImage
    from ij.process import LUT

    _ck()

    # Determine uniform size = max dimensions across channels
    max_w = 0; max_h = 0
    procs = []
    for ch in present:
        proc, pt, le = self_win._load_full_channel(ch, sid)
        if proc.getWidth()  > max_w: max_w = proc.getWidth()
        if proc.getHeight() > max_h: max_h = proc.getHeight()
        procs.append((ch, proc))

    # Build stack. Each channel becomes one slice.
    stack = ImageStack(max_w, max_h)
    ch_luts   = []
    ch_ranges = []
    for (ch, proc) in procs:
        # If channel smaller than max, pad into a new processor.
        if proc.getWidth() == max_w and proc.getHeight() == max_h:
            padded = proc
        else:
            # Create a same-type processor at max dims and insert
            padded = proc.createProcessor(max_w, max_h)
            padded.insert(proc, 0, 0)
        stack.addSlice(ch, padded)

        p = ch_params[ch]
        color = LUT_BY_NAME.get(p['lut'], Color.WHITE)
        # black-to-color gradient (a jarray 'b' rejects values > 127, so let
        # ImageJ build it, as the single-channel export does)
        ch_luts.append(LUT.createLutFromColor(color))
        ch_ranges.append((float(p['min']), float(p['max'])))

    imp = ImagePlus("merged", stack)
    # Tell ImagePlus it's a multi-channel image (not a z-stack):
    imp.setDimensions(len(procs), 1, 1)   # channels, slices, frames
    cimp = CompositeImage(imp, CompositeImage.COMPOSITE)
    # Apply per-channel LUT and display range
    for i, lut in enumerate(ch_luts):
        cimp.setPosition(i + 1, 1, 1)
        cimp.setChannelLut(lut)
        cmin, cmax = ch_ranges[i]
        cimp.setDisplayRange(cmin, cmax)

    out_name = file_prefix + self_win._build_merge_filename(
        sid, "_orig", ".tif")
    out_path = _ex_out_path(self_win, sub_dir, sid, out_name)
    _ck()
    fs = FileSaver(cimp)
    fs.saveAsTiff(out_path)
    imp.close()
    cimp.close()

    if progress is not None and progress.is_cancelled():
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
                IJ.log("[CANCEL] removed partial: " + out_path)
        except Exception:
            pass
        JSystem.gc()
        raise _ExportCancelled()
    JSystem.gc()


# ---------------------------------------------------------------
def _ex_write_export_md(self, md_path, stamp, out_root,
                        target_sets, sc_opts, mg_opts, fmts, ch_params):
    f = codecs.open(md_path, 'w', 'utf-8')
    try:
        f.write("# Merge & Export Parameters\n\n")
        try:
            import SectionPipelineProject as _spp
            f.write(u"- **Slide Image Processor**: {}\n".format(_spp.VERSION))
        except Exception:
            pass
        f.write(u"- **Timestamp**: {}\n".format(stamp))
        f.write(u"- **Source folder**: `{}`\n".format(self.folder_path))
        f.write(u"- **Output folder**: `{}`\n\n".format(out_root))

        f.write("## Channels\n\n")
        for ch in self.channels:
            p = ch_params[ch]
            f.write(u"- **{}**: min={}, max={}, LUT={}\n".format(
                ch, p['min'], p['max'], p['lut']))

        f.write("\n## Output options\n\n")
        f.write(u"- **Formats**: {}\n".format(", ".join(fmts)))
        f.write(u"- **Subfolders**: {}\n".format(
            "ignored (one folder per type)" if getattr(self, "_ex_flat", False)
            else "kept below each type folder"))
        if sc_opts:
            f.write("- **Single Channel**:\n")
            for (d, lm) in sc_opts:
                f.write(u"  - depth={}, LUT={}\n".format(d, lm))
        if mg_opts:
            f.write("- **Merged** (color, additive):\n")
            for d in mg_opts:
                f.write(u"  - depth={}\n".format(d))

        f.write(u"\n## Sets exported ({})\n\n".format(len(target_sets)))
        for sid in target_sets:
            chs = [ch for ch in self.channels
                   if ch in self.sets[sid]]
            rot, flip = getattr(self, "slide_orient", {}).get(sid, (0, False))
            f.write(u"- `{}` (channels: {}; rotation={}, flip_h={})\n".format(
                sid, ", ".join(chs), rot, flip))
    finally:
        f.close()


# ---------------------------------------------------------------
def register_export_methods(MainWindowCls):
    """Attach all export functions to MainWindow as methods."""
    MainWindowCls._on_merge          = _ex_on_merge
    MainWindowCls._do_export         = _ex_do_export
    MainWindowCls._load_full_channel = _ex_load_full_channel
    MainWindowCls._colorize_byte     = _ex_colorize_byte
    MainWindowCls._save_image        = _ex_save_image
    MainWindowCls._export_single     = _ex_export_single
    MainWindowCls._export_merged     = _ex_export_merged
    MainWindowCls._build_merge_filename = _ex_build_merge_filename
    MainWindowCls._write_export_md   = _ex_write_export_md
