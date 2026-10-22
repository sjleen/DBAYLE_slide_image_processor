# Section file renaming after the export (Section Pipeline, step 2)
#
# Sample names or the section order were changed after the section files
# were written: rename the files already on disk (no new crops) so their
# names follow the project again, in every step-2 export folder and in
# the step-3 folders made from them. sections.csv / jobs.csv /
# orientation.csv are rewritten to match; the md gets a note.
#
# Plan  : plan_renames(root, proj)  -> [folder plan]
# Apply : apply_renames(plans, stamp) -> (renamed, errors)
# Check : stale_exports(root, proj)   -> [folders whose names are out of date]

import os, csv, codecs, re

import SectionPipelineProject as spp

EXT = {'TIF': '.tif', 'JPG': '.jpg', 'PNG': '.png'}
TMP_SUFFIX = ".renaming"


def _read_csv(path):
    f = open(path, 'rb')
    try:
        rows = [dict((k, v.decode("utf-8") if isinstance(v, str) else v) for k, v in r.items())
                for r in csv.DictReader(f)]
        f.seek(0)
        head = [h.decode("utf-8") for h in (csv.reader(f).next() or [])]
    finally:
        f.close()
    return head, rows


def _write_csv(path, head, rows):
    f = open(path, 'wb')
    try:
        w = csv.writer(f)
        w.writerow([h.encode("utf-8") for h in head])
        for r in rows:
            w.writerow([unicode(r.get(h, "")).encode("utf-8") for h in head])
    finally:
        f.close()


def _md_template(d):
    """Filename template of the newest Section Cropper md in d."""
    mds = sorted(f for f in os.listdir(d)
                 if f.startswith("SectionCropper__") and f.endswith(".md"))
    for m in reversed(mds):
        f = codecs.open(os.path.join(d, m), 'r', 'utf-8')
        try:
            t = re.search(r"\*\*Filename template\*\*: *`([^`]*)`", f.read())
        finally:
            f.close()
        if t:
            return t.group(1)
    return None


def section_dirs(root, max_depth=3):
    """Folders below root holding a sections.csv (step 2 and step 3 output)."""
    out = []
    top = os.path.abspath(root).rstrip("\\/")
    for d, dirs, files in os.walk(top):
        if d[len(top):].count(os.sep) >= max_depth:
            dirs[:] = []
        dirs[:] = sorted(x for x in dirs if not x.startswith('.'))
        if "sections.csv" in files:
            out.append(d)
    return out


def _new_rel(rel, name):
    parts = rel.replace("\\", "/").split("/")
    return "/".join(parts[:-1] + [name])


def plan_renames(root, proj):
    """One plan per folder: {'dir', 'renames': [(old rel, new rel)],
    'rows': {sec_id: project row}, 'error': text or None}."""
    rows = dict((r['sec_id'], r) for r in spp.numbered_sections(proj))
    dirs = section_dirs(root)
    maps, plans = {}, []
    # step-2 folders (they keep jobs.csv: kind / channel / format per file)
    for d in dirs:
        jp = os.path.join(d, "jobs.csv")
        if not os.path.exists(jp):
            continue
        tmpl = _md_template(d)
        if not tmpl:
            continue
        _h, jobs = _read_csv(jp)
        m = {}
        for j in jobs:
            r = rows.get(j.get('sec_id'))
            rel = j.get('file') or ""
            if r is None or not rel or j.get('format') not in EXT:
                continue
            new = _new_rel(rel, spp.render_filename(tmpl, r, j.get('channel'), EXT[j['format']]))
            m[rel] = new
        maps[d] = m
    # every folder: the map of its step-2 source (itself, or the step-2 folder
    # whose files it mirrors)
    for d in dirs:
        _h, srows = _read_csv(os.path.join(d, "sections.csv"))
        pairs = [(s.get('sec_id'), f) for s in srows for f in (s.get('files') or "").split(";") if f]
        m = maps.get(d)
        if m is None:
            for src, sm in maps.items():
                if pairs and all(f in sm for _sid, f in pairs):
                    m = sm
                    break
        if m is None:
            continue
        ren = [(f, m[f]) for _sid, f in pairs
               if f in m and m[f] != f and os.path.exists(os.path.join(d, f))]
        plan = {'dir': d, 'renames': ren, 'rows': rows, 'map': m, 'error': None}
        news = [n for _o, n in ren]
        olds = set(o for o, _n in ren)
        if len(set(news)) != len(news):
            plan['error'] = "two files would get the same name (add {NNN} or {channel} to the template)"
        else:
            taken = [n for n in news if n not in olds and os.path.exists(os.path.join(d, n))]
            if taken:
                plan['error'] = "%s already exists" % taken[0]
        if ren or plan['error'] or _csv_stale(srows, rows):
            plans.append(plan)
    return plans


def _csv_stale(srows, rows):
    for s in srows:
        r = rows.get(s.get('sec_id'))
        if r and (s.get('sample') != r['sample'] or s.get('NNN') != r['NNN']):
            return True
    return False


def stale_exports(root, proj):
    """Section folders whose sample / numbering no longer match the project."""
    rows = dict((r['sec_id'], r) for r in spp.numbered_sections(proj))
    out = []
    for d in section_dirs(root):
        try:
            _h, srows = _read_csv(os.path.join(d, "sections.csv"))
        except Exception:
            continue
        if _csv_stale(srows, rows):
            out.append(d)
    return out


def apply_renames(plans, stamp):
    """Rename (two passes, so swapped names never clash) and rewrite the
    csv files. Returns (files renamed, errors)."""
    n, errors = 0, []
    for p in plans:
        if p['error']:
            errors.append("%s: %s" % (os.path.basename(p['dir']), p['error']))
            continue
        d, done = p['dir'], []
        try:
            for o, _n in p['renames']:
                os.rename(os.path.join(d, o), os.path.join(d, o) + TMP_SUFFIX)
                done.append(o)
            for o, nw in p['renames']:
                dst = os.path.join(d, nw)
                if not os.path.isdir(os.path.dirname(dst)):
                    os.makedirs(os.path.dirname(dst))
                os.rename(os.path.join(d, o) + TMP_SUFFIX, dst)
                done.remove(o)
                n += 1
        except OSError as e:
            errors.append("%s: %s" % (os.path.basename(d), e))
            for o in done:                    # put back what is still parked
                try:
                    os.rename(os.path.join(d, o) + TMP_SUFFIX, os.path.join(d, o))
                except OSError:
                    pass
            continue
        _rewrite_records(p, stamp)
    return n, errors


def _rewrite_records(p, stamp):
    d, m, rows = p['dir'], p['map'], p['rows']
    mp = lambda f: m.get(f, f)
    head, srows = _read_csv(os.path.join(d, "sections.csv"))
    for s in srows:
        r = rows.get(s.get('sec_id'))
        if r:
            s['NNN'], s['sample'] = r['NNN'], r['sample']
        s['files'] = ";".join(mp(f) for f in (s.get('files') or "").split(";") if f)
    _write_csv(os.path.join(d, "sections.csv"), head, srows)
    jp = os.path.join(d, "jobs.csv")
    if os.path.exists(jp):
        head, jobs = _read_csv(jp)
        for j in jobs:
            r = rows.get(j.get('sec_id'))
            if r:
                j['NNN'], j['sample'] = r['NNN'], r['sample']
            j['file'] = mp(j.get('file') or "")
        _write_csv(jp, head, jobs)
    op = os.path.join(d, "orientation.csv")
    if os.path.exists(op):
        base = dict((os.path.basename(o), os.path.basename(nw)) for o, nw in m.items())
        head, orows = _read_csv(op)
        for o in orows:
            r = rows.get(o.get('key'))
            if r:
                o['sample'] = r['sample']
                o['label'] = "%s %s" % (r['NNN'], r['sample'])
            o['files'] = ";".join(base.get(f, f) for f in (o.get('files') or "").split(";") if f)
        _write_csv(op, head, orows)
    mds = sorted(f for f in os.listdir(d) if f.endswith("_export parameters.md"))
    if mds:
        f = codecs.open(os.path.join(d, mds[-1]), 'a', 'utf-8')
        try:
            f.write(u"\n## Names updated %s\n\n- %d file(s) renamed to the project's current "
                    u"sample names / numbering (no new crops)\n" % (stamp, len(p['renames'])))
        finally:
            f.close()
