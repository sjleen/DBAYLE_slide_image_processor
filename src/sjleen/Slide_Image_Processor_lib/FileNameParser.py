# ==============================================================
#  Multichannel Image Set Previewer - Phase 1: FileNameParser
#  Fiji/ImageJ Jython Script
#
#  Role  : Scan a folder, auto-detect the channel token position
#           in filenames, and group images into channel sets.
#  Usage : Run directly in Fiji Script Editor (Language: Python)
#          OR import from MultichannelImageSetPreviewer_.py
# ==============================================================

import os
import re
from collections import defaultdict

SUPPORTED_EXTENSIONS = ['.tif', '.tiff', '.jpg', '.jpeg', '.png']
# '_' (each one: '__' holds an empty token) and spaces split names; '-' never
# does (it is the only safe joiner inside a token, e.g. 's1-2', 'slice-vmhvl')
DELIMITER_PATTERN    = r'(_|[ \t]+)'

# One channel of a multi-channel / RGB-merge file is addressed as
# "<file path>|C2" or "<file path>|G". '|' cannot occur in Windows file
# names, so it never clashes with a real path.
COMP_SEP = "|"


def split_component(path):
    """'a.tif|C2' -> ('a.tif', 'C2'); 'a.tif' -> ('a.tif', None)."""
    if COMP_SEP in path:
        p, c = path.rsplit(COMP_SEP, 1)
        return p, c
    return path, None


def real_path(path):
    """The file on disk behind a (possibly component) path."""
    return split_component(path)[0]


def file_stem(path):
    """File name without extension, plus '_C2' for a component."""
    p, c = split_component(path)
    s = os.path.splitext(os.path.basename(p))[0]
    return (s + "_" + c) if c else s


# --------------------------------------------------------------
# Utility
# --------------------------------------------------------------

def _is_mergepreviewer_export_folder(folder_path):
    """Return True if folder looks like a Multichannel Image Set Previewer
    export output.

    Marker: contains a file ending in '_export parameters.md'.
    """
    try:
        for entry in os.listdir(folder_path):
            if entry.endswith('_export parameters.md'):
                return True
    except Exception:
        pass
    return False


def get_image_files(folder_path):
    """Return sorted list of image filenames found at or below
    folder_path.  Recurses into subfolders (skipping '.misp_workspace',
    hidden directories, and any sub-folder that is a Multichannel Image
    Set Previewer export output).  Raises ValueError if two files at
    different paths share the same basename.
    """
    entries = []
    seen    = {}     # filename -> first-seen path
    for root, dirs, files in os.walk(folder_path):
        # Skip internal / hidden directories
        kept_dirs = []
        for d in dirs:
            if d.startswith('.'):
                continue
            if d == WORKSPACE_SKIP_NAME:
                continue
            full_sub = os.path.join(root, d)
            if _is_mergepreviewer_export_folder(full_sub):
                # Don't descend into Multichannel Image Set Previewer's
                # own export folders
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for f in files:
            _, ext = os.path.splitext(f)
            if ext.lower() not in SUPPORTED_EXTENSIONS:
                continue
            # the same name in several subfolders is fine (see _unique_ids)
            entries.append(os.path.join(root, f))
    entries.sort()
    return entries


# Name used by the main UI's workspace folder; kept in sync here so
# we don't recurse into it.
WORKSPACE_SKIP_NAME = ".misp_workspace"


def tokenize_with_delimiters(filename):
    """
    Split filename stem into tokens and delimiters.

    'Sample01_DAPI'  -> (['Sample01','DAPI'], ['_'])
    'Sample-01_GFP'  -> (['Sample-01','GFP'], ['_'])      ('-' does not split)
    's1_data__488'   -> (['s1','data','','488'], ['_','_','_'])  ('__': empty token)
    """
    stem  = os.path.splitext(filename)[0]
    parts = re.split(DELIMITER_PATTERN, stem)
    tokens     = parts[0::2]
    delimiters = parts[1::2]
    return tokens, delimiters


def reconstruct_name(tokens, delimiters):
    """
    Rebuild filename stem from token and delimiter lists.
    (['Sample01','Merge'], ['_']) -> 'Sample01_Merge'
    """
    result = tokens[0]
    for delim, tok in zip(delimiters, tokens[1:]):
        result += delim + tok
    return result


# --------------------------------------------------------------
# Channel position detection
# --------------------------------------------------------------

def detect_channel_position(token_matrix):
    """
    Auto-detect which token position encodes the channel name.

    A position qualifies when:
      (a) it has >= 2 unique values  (multiple channels exist)
      (b) each unique value appears the same number of times
          (channels are represented uniformly across all sets)

    Among qualifying positions, pick the one with fewest unique
    values. Ties -> AMBIGUOUS, user must choose manually.

    Returns: (info_or_candidates, status_code)
    status_code: 'OK' | 'AMBIGUOUS' | 'NO_CANDIDATE' | 'TOKEN_COUNT_MISMATCH'
    """
    if not token_matrix:
        return None, 'NO_CANDIDATE'

    n_positions = len(token_matrix[0])
    if not all(len(row) == n_positions for row in token_matrix):
        return None, 'TOKEN_COUNT_MISMATCH'

    position_stats = []
    for pos in range(n_positions):
        values      = [row[pos] for row in token_matrix]
        unique_vals = list(set(values))
        n_unique    = len(unique_vals)
        counts      = {v: values.count(v) for v in unique_vals}
        count_vals  = list(counts.values())
        is_uniform  = (len(set(count_vals)) == 1)
        position_stats.append({
            'pos'        : pos,
            'unique_vals': unique_vals,
            'n_unique'   : n_unique,
            'is_uniform' : is_uniform,
        })

    candidates = [p for p in position_stats
                  if p['n_unique'] > 1 and p['is_uniform']]

    if not candidates:
        return None, 'NO_CANDIDATE'
    if len(candidates) == 1:
        return candidates[0], 'OK'

    # Several positions fit (a full slide x channel grid fits both ways, e.g.
    # 3 slides x 4 channels): take the one whose values all look like
    # channel names; otherwise the user chooses (never a silent guess).
    chlike = [p for p in candidates
              if all(_CHANNEL_LIKE.match(v) for v in p['unique_vals'])]
    if len(chlike) == 1:
        return chlike[0], 'OK'
    candidates.sort(key=lambda p: (p not in chlike, p['n_unique']))
    return candidates, 'AMBIGUOUS'


# token values that name a channel: dyes / proteins, wavelengths, C1 / ch2 / w1
_CHANNEL_LIKE = re.compile(
    r"^(dapi|hoechst|gfp|egfp|yfp|eyfp|cfp|rfp|tdtomato|tomato|mcherry|mscarlet|fitc|tritc|"
    r"txred|texasred|cy[2357](\.5)?|alexa\d*|af\d{3}|a\d{3}|[1-9]\d{2}(nm)?|c\d{1,2}|ch\d{1,2}|"
    r"w\d{1,2}|bf|brightfield|phase|dic|red|green|blue|farred|far-red|nir|merge|merged)$",
    re.IGNORECASE)


# --------------------------------------------------------------
# Main parsing function
# --------------------------------------------------------------

def parse_folder(folder_path, forced_channel_pos=None, allow_no_channel=False):
    """
    Scan folder and group images by channel and set ID.

    Args:
      folder_path        : path to image folder
      forced_channel_pos : override auto-detection (int); use when AMBIGUOUS

    Returns dict:
      status            : 'OK' | 'AMBIGUOUS' | 'ERROR'
      channel_position  : int
      channels          : sorted list of channel name strings
      sets              : { set_id: { channel_name: full_file_path } }
      token_info        : { set_id: { reconstruction metadata } }
      ambiguous_candidates : list (only present when AMBIGUOUS)
      errors            : list of warning/error strings
    """
    result = {
        'status'          : None,
        'channel_position': None,
        'channels'        : [],
        'sets'            : {},
        'token_info'      : {},
        'errors'          : []
    }

    # 0. Reject Multichannel Image Set Previewer's own export folders early
    if _is_mergepreviewer_export_folder(folder_path):
        result['status'] = 'ERROR'
        result['errors'].append(
            "Selected folder is a Multichannel Image Set Previewer "
            "export output (contains '*_export parameters.md').\n"
            "Please select the ORIGINAL source folder instead.")
        return result

    # 1. Collect files (full paths, recursively)
    try:
        files = get_image_files(folder_path)
    except ValueError as ex:
        result['status'] = 'ERROR'
        result['errors'].append(unicode(ex))
        return result
    return parse_files(files, forced_channel_pos, allow_no_channel)


def _dir_tag(path, common):
    """Subfolder of path below common, as one token ('01/1-6x16' -> '01-1-6x16')."""
    rel = os.path.relpath(os.path.dirname(os.path.abspath(real_path(path))), common)
    rel = "" if rel == "." else rel
    return rel.replace("\\", "/").strip("/").replace("/", "-")


def _common_dir(paths):
    dirs = [os.path.dirname(os.path.abspath(real_path(p))) for p in paths]
    if not dirs:
        return ""
    c = os.path.commonprefix([d + os.sep for d in dirs])
    return c[:c.rfind(os.sep)] if os.sep in c else c


def _unique_ids(pairs, channels=None):
    """pairs: [(set_id, path)] -> {(set_id, path): final id}. When the same
    set + channel turns up in several subfolders (same file name in
    different folders), that set id gets the subfolder in front, so they
    stay separate sets. A set whose channels sit in different folders
    (one folder per channel) is left alone."""
    channels = channels or [""] * len(pairs)
    seen, clash = {}, set()
    for (sid, p), ch in zip(pairs, channels):
        if (sid, ch) in seen:
            clash.add(sid)
        seen[(sid, ch)] = p
    common = _common_dir([p for _s, p in pairs]) if clash else ""
    out = {}
    for sid, p in pairs:
        tag = _dir_tag(p, common) if sid in clash else ""
        out[(sid, p)] = (tag + "_" + sid) if tag else sid
    return out


def _one_set_per_file(files, result):
    """No channel token in the names: every file is its own set with a
    blank channel (filled in when its channels are split out)."""
    sets = {}
    pairs = [(os.path.splitext(os.path.basename(real_path(f)))[0], f) for f in files]
    ids = _unique_ids(pairs)
    for sid, full in pairs:
        sets.setdefault(ids[(sid, full)], {})[""] = full
    result.update({'status': 'OK', 'channel_position': None, 'channels': [""],
                   'sets': sets, 'token_info': {}, 'no_channel_token': True})
    return result


def parse_files(files, forced_channel_pos=None, allow_no_channel=False):
    """Same as parse_folder, for an explicit list of full paths
    (e.g. the files a user checked in the discovery list).
    allow_no_channel: when no filename token varies like a channel, treat
    each file as one set (all channels inside the file) instead of failing."""
    result = {
        'status'          : None,
        'channel_position': None,
        'channels'        : [],
        'sets'            : {},
        'token_info'      : {},
        'errors'          : []
    }
    files = sorted(files)
    # the same file name may sit in several subfolders: such sets get their
    # subfolder in front of the set id (_unique_ids)
    if not files:
        result['status'] = 'ERROR'
        result['errors'].append(
            u"No supported image files found. (Supported: {})".format(
                ', '.join(SUPPORTED_EXTENSIONS)))
        return result

    # 2. Tokenize (by basename; full path stored alongside)
    parsed = []
    for full_path in files:
        fname = os.path.basename(full_path)
        tokens, delims = tokenize_with_delimiters(fname)
        _, ext = os.path.splitext(fname)
        if len(tokens) < 2:
            if allow_no_channel and forced_channel_pos is None:
                return _one_set_per_file(files, result)
            result['status'] = 'ERROR'
            result['errors'].append(
                u"'{}': no delimiter found, cannot parse channel.".format(fname))
            return result
        parsed.append((fname, tokens, delims, ext, full_path))

    token_matrix = [p[1] for p in parsed]

    # 3. Detect channel position
    if forced_channel_pos is not None:
        n_pos = len(token_matrix[0])
        if not (0 <= forced_channel_pos < n_pos):
            result['status'] = 'ERROR'
            result['errors'].append(
                u"forced_channel_pos={} out of range (0-{}).".format(
                    forced_channel_pos, n_pos - 1))
            return result
        ch_pos = forced_channel_pos

    else:
        ch_info, status_code = detect_channel_position(token_matrix)

        if status_code == 'TOKEN_COUNT_MISMATCH':
            result['status'] = 'ERROR'
            result['errors'].append(
                "Token count differs across files. "
                "Check that all filenames follow the same pattern.")
            return result

        if status_code == 'NO_CANDIDATE':
            if allow_no_channel:
                return _one_set_per_file(files, result)
            result['status'] = 'ERROR'
            result['errors'].append(
                "Cannot auto-detect channel position. "
                "All tokens are either identical or all unique.")
            return result

        if status_code == 'AMBIGUOUS':
            result['status'] = 'AMBIGUOUS'
            result['ambiguous_candidates'] = ch_info
            result['errors'].append(
                "Multiple channel position candidates. "
                "Manual selection required. "
                u"Candidates: {}".format([p['pos'] for p in ch_info]))
            return result

        ch_pos = ch_info['pos']

    # 4. Group into sets
    sets     = defaultdict(dict)
    tok_info = {}
    # set ids join the pieces with '_' and turn '-' into '_' as well, so they
    # stay the same as when '-' still split names (saved projects keep their slides)
    sid_of = lambda toks: "_".join(toks).replace("-", "_")
    raw = [(sid_of([t for i, t in enumerate(tokens) if i != ch_pos]), full_path)
           for _f, tokens, _d, _e, full_path in parsed]
    ids = _unique_ids(raw, [tokens[ch_pos] for _f, tokens, _d, _e, _p in parsed])

    for fname, tokens, delims, ext, full_path in parsed:
        channel_name = tokens[ch_pos]
        set_tokens   = [t for i, t in enumerate(tokens) if i != ch_pos]
        set_delims   = []
        for i, d in enumerate(delims):
            if i != ch_pos and i != ch_pos - 1:
                set_delims.append(d)
            elif len(set_delims) < len(set_tokens) - 1:
                if i < ch_pos - 1 or i > ch_pos:
                    set_delims.append(d)

        set_id = ids[(sid_of(set_tokens), full_path)]
        # full_path already holds the full path returned by os.walk
        sets[set_id][channel_name] = full_path

        if set_id not in tok_info:
            ch_delim = delims[ch_pos - 1] if ch_pos > 0 else \
                       (delims[ch_pos] if ch_pos < len(delims) else '_')
            tok_info[set_id] = {
                'set_tokens'        : set_tokens,
                'set_delims'        : set_delims,
                'ch_delim'          : ch_delim,
                'ch_pos_in_original': ch_pos,
                'original_delims'   : delims,
                'original_n_tokens' : len(tokens),
                'extension'         : ext
            }

    result['channel_position'] = ch_pos
    result['channels']         = sorted(set(
        tokens[ch_pos] for _, tokens, _, _, _ in parsed))
    result['sets']             = dict(sets)
    result['token_info']       = tok_info
    result['status']           = 'OK'

    # 5. Warn about missing channels
    for set_id, ch_map in result['sets'].items():
        missing = [ch for ch in result['channels'] if ch not in ch_map]
        if missing:
            result['errors'].append(
                u"WARNING: set '{}' is missing channel(s): {}".format(
                    set_id, missing))

    return result


# --------------------------------------------------------------
# Output filename builder
# --------------------------------------------------------------

def build_output_filename(set_id, token_info, output_ext='.tif', suffix=''):
    """
    Replace channel token with 'Merge', preserve original delimiters.

    suffix : '_8bit' | '_original' | '' (when only one option checked)
    e.g.   -> 'Sample01_Merge_8bit.tif'
    """
    info        = token_info[set_id]
    set_tokens  = info['set_tokens']
    ch_pos      = info['ch_pos_in_original']
    orig_delims = info['original_delims']
    n_orig      = info['original_n_tokens']

    full_tokens = []
    set_tok_idx = 0
    for i in range(n_orig):
        if i == ch_pos:
            full_tokens.append('Merge')
        else:
            full_tokens.append(set_tokens[set_tok_idx])
            set_tok_idx += 1

    stem = reconstruct_name(full_tokens, orig_delims)
    return stem + suffix + output_ext


# --------------------------------------------------------------
# Log output (Fiji Log window)
# --------------------------------------------------------------

def print_parse_result(result, show_paths=False):
    from ij import IJ
    log = IJ.log

    log("=" * 56)
    log(u"[FileNameParser]  Status: {}".format(result['status']))

    if result['errors']:
        log("")
        log("-- Warnings / Errors --")
        for e in result['errors']:
            log("  [!] " + e)

    if result['status'] == 'OK':
        log("")
        log(u"Channel Position  : {}".format(result['channel_position']))
        log(u"Channels Detected : {}".format(result['channels']))
        log(u"Image Sets        : {} total".format(len(result['sets'])))
        log("")
        for set_id in sorted(result['sets'].keys()):
            ch_map = result['sets'][set_id]
            log(u"  Set [{}]".format(set_id))
            for ch in sorted(ch_map.keys()):
                path_str = ch_map[ch] if show_paths \
                           else os.path.basename(ch_map[ch])
                log(u"    {:12s} -> {}".format(ch, path_str))
            name_8bit   = build_output_filename(
                set_id, result['token_info'], '.tif', '_8bit')
            name_orig   = build_output_filename(
                set_id, result['token_info'], '.tif', '_original')
            name_single = build_output_filename(
                set_id, result['token_info'], '.tif', '')
            log(u"    >> both    : {}  /  {}".format(name_8bit, name_orig))
            log(u"    >> single  : {}".format(name_single))

    elif result['status'] == 'AMBIGUOUS':
        log("")
        log("-- Channel position candidates --")
        for cand in result.get('ambiguous_candidates', []):
            log(u"  Position {}: values = {}".format(
                cand['pos'], sorted(cand['unique_vals'])))
        log("  => Re-run: parse_folder(path, forced_channel_pos=N)")

    log("=" * 56)


# --------------------------------------------------------------
# Entry point (standalone test run)
# --------------------------------------------------------------

def run_test():
    from javax.swing import JFileChooser, JOptionPane
    from ij import IJ

    chooser = JFileChooser()
    chooser.setFileSelectionMode(JFileChooser.DIRECTORIES_ONLY)
    chooser.setDialogTitle("Select image folder")
    ret = chooser.showOpenDialog(None)

    if ret != JFileChooser.APPROVE_OPTION:
        IJ.log("Cancelled.")
        return

    folder = chooser.getSelectedFile().getAbsolutePath()
    IJ.log("Selected: " + folder)
    result = parse_folder(folder)

    if result['status'] == 'AMBIGUOUS':
        candidates = result['ambiguous_candidates']
        options    = [u"Position {}: {}".format(
                          c['pos'], sorted(c['unique_vals']))
                      for c in candidates]
        choice = JOptionPane.showInputDialog(
            None,
            "Multiple channel position candidates.\nSelect the correct one:",
            "Select Channel Position",
            JOptionPane.QUESTION_MESSAGE,
            None, options, options[0])
        if choice is None:
            IJ.log("Cancelled.")
            return
        sel_pos = candidates[options.index(choice)]['pos']
        result  = parse_folder(folder, forced_channel_pos=sel_pos)

    print_parse_result(result, show_paths=False)


if __name__ == '__main__':
    run_test()
