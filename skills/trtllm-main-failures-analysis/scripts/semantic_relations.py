#!/usr/bin/env python3
"""Semantic-relationship graph between commits in a git window (not only conflicts).

Reuses diff_cache.json produced by semantic_conflict_scan2.py (per-commit parsed diffs).
For an earlier commit A and a later commit B it emits an edge when B's change relates to
something A changed:
  uses-added-symbol        B adds a use/import of a module-level def/class/var that A added
  uses-removed-symbol      B adds a use/import of a symbol A removed/renamed            (break risk)
  imports-moved-module     B adds an import of a module path A renamed/deleted         (break risk)
  calls-resigned-function  A re-signed F, B adds a call to F (or the reverse order)    (interface)
  calls-return-changed     A changed F's return statements, B adds a call to F         (contract)
  calls-body-changed       A edited F's body, B adds a call to F                        (behaviour)
  edits-same-function      A and B both edit F's body                                  (overlap)
  edits-added-symbol       B edits a def/class that A added                             (extension)
Annotations: stale_branch (B's PR branch finished before A merged, from pr_last_commit.json; None if
unknown), unresolvable_at_ref (removed symbol / moved module no longer resolves at <ref>), same_pr_author.
"""
from __future__ import annotations
import argparse, collections, csv, datetime as dt, json, re, subprocess, sys
from pathlib import Path
GENERIC = {'main','run','test','setup','forward','init','call','get','set','update','create','build','load','save','parse','start','stop','close','open','reset','step','add','remove','apply','check','validate','process','execute','handle','register','configure','prepare','finalize','cleanup','shutdown','wait','send','recv','read','write','put','pop','push','copy','clone','new','make','from_dict','to_dict','items','keys','values','warmup','_','__init__','__call__','__repr__','__str__','__eq__','__hash__','__len__','__iter__','__getitem__','__setitem__','__enter__','__exit__','__del__','__post_init__','shape','size','device','dtype'}
BUILTINS = set(dir(__builtins__)) if not isinstance(__builtins__, dict) else set(__builtins__)
def git(args, cwd, check=True):
    r = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode: raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r.stdout
def modpath(f): return f[:-3].replace('/', '.') if f.endswith('.py') else f.replace('/', '.')
_src = {}
def src(sha, f, cwd):
    k = (sha, f)
    if k not in _src:
        try: _src[k] = git(['show', f'{sha}:{f}'], cwd)
        except subprocess.CalledProcessError: _src[k] = ''
    return _src[k]
def imports_from(sha, use_file, def_file, name, cwd):
    if use_file == def_file: return True
    tail = modpath(def_file).split('.')[-1]; s = src(sha, use_file, cwd)
    if not s: return False
    return bool(re.search(r'from\s+[\w.]*' + re.escape(tail) + r'\s+import[^\n]*\b' + re.escape(name) + r'\b', s)) or bool(re.search(r'\b' + re.escape(tail) + r'\.' + re.escape(name) + r'\b', s))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--repo', required=True); ap.add_argument('--ref', default='origin/main'); ap.add_argument('--since', required=True); ap.add_argument('--cache-dir', required=True); ap.add_argument('--out-dir', required=True); ap.add_argument('--max-def-count', type=int, default=3); ap.add_argument('--fetch-pr-dates', action='store_true')
    a = ap.parse_args(); cwd = a.repo; out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    dcache = json.loads((Path(a.cache_dir) / 'diff_cache.json').read_text())
    pcache_p = Path(a.cache_dir) / 'pr_last_commit.json'; pr_last = json.loads(pcache_p.read_text()) if pcache_p.exists() else {}
    log = git(['log', '--no-merges', '--first-parent', f'--since={a.since}', '--format=%H%x09%ct%x09%an%x09%s', a.ref], cwd).strip().splitlines()
    commits = []
    for l in log:
        sha, ct, an, subj = l.split('\t', 3); pr = re.search(r'\(#(\d+)\)\s*$', subj)
        if sha not in dcache: continue
        commits.append({'sha': sha, 'short': sha[:12], 'ts': int(ct), 'author': an, 'subject': subj, 'pr': pr.group(1) if pr else None, 'd': dcache[sha]})
    commits.sort(key=lambda c: c['ts']); n = len(commits); print(f'{n} commits with cached diffs', file=sys.stderr)
    defcount = collections.Counter()
    for l in git(['grep', '-h', '-P', r'^\s*(def|class)\s+[A-Za-z_][A-Za-z0-9_]*', a.ref, '--', '*.py'], cwd).splitlines():
        m = re.search(r'(def|class)\s+([A-Za-z_]\w*)', l)
        if m: defcount[m.group(2)] += 1
    ref_files = set(git(['ls-tree', '-r', '--name-only', a.ref], cwd).splitlines())
    ok = lambda s: defcount[s] <= a.max_def_count and len(s) > 3 and s not in BUILTINS and s not in GENERIC
    # inverted indexes over later commits
    use_idx = collections.defaultdict(list)      # symbol -> [(j, file)]
    mod_idx = collections.defaultdict(list)      # module path -> [(j, file)]
    func_idx = collections.defaultdict(list)     # (file, func) -> [j]
    for j, c in enumerate(commits):
        for f, names in c['d']['uses'].items():
            for s in names: use_idx[s].append((j, f))
        for f, mods in c['d']['mods'].items():
            for m in mods: mod_idx[m].append((j, f))
        for f, fns in c['d']['funcs'].items():
            for fn in fns: func_idx[(f, fn)].append(j)
    edges = []
    def edge(rel, i, j, sym, af, bf, **kw):
        edges.append(dict(rel=rel, i=i, j=j, symbol=sym, A_file=af, B_file=bf, **kw))
    for i, A in enumerate(commits):
        d = A['d']
        # symbols A added / removed / re-signed / return-changed / body-changed
        for f, names in d['added'].items():
            for s, sig in names.items():
                if not ok(s): continue
                for j, bf in use_idx.get(s, []):
                    if j > i and s not in commits[j]['d']['added'].get(bf, {}) and imports_from(commits[j]['sha'], bf, f, s, cwd):
                        edge('uses-added-symbol', i, j, s, f, bf)
                for j in func_idx.get((f, s), []):
                    if j > i: edge('edits-added-symbol', i, j, s, f, f)
        for f, names in d['removed'].items():
            for s in names:
                if not ok(s): continue
                for j, bf in use_idx.get(s, []):
                    if j > i and s not in commits[j]['d']['added'].get(bf, {}) and imports_from(commits[j]['sha'], bf, f, s, cwd):
                        edge('uses-removed-symbol', i, j, s, f, bf, unresolvable_at_ref=not bool(git(['grep', '-l', '-w', s, a.ref, '--', f.rsplit('/', 1)[0]], cwd, check=False).strip()))
        oldmods = {modpath(o): modpath(nw) for o, nw in d['renamed'].items()} | {modpath(dl): None for dl in d['deleted']}
        for om, nm in oldmods.items():
            om2 = om[:-9] if om.endswith('.__init__') else om
            for m, lst in mod_idx.items():
                if m == om2 or m.startswith(om2 + '.'):
                    for j, bf in lst:
                        if j > i: edge('imports-moved-module', i, j, om2, f'{om2} -> {nm or "<deleted>"}', bf, unresolvable_at_ref=(om2.replace('.', '/') + '.py') not in ref_files and (om2.replace('.', '/') + '/__init__.py') not in ref_files)
        for f, names in d['resigned'].items():
            for s, (old, new) in names.items():
                if not ok(s): continue
                for j, bf in use_idx.get(s, []):
                    if j > i and bf != f and bf not in d['touched'] and s not in commits[j]['d']['added'].get(bf, {}) and imports_from(commits[j]['sha'], bf, f, s, cwd):
                        edge('calls-resigned-function', i, j, s, f, bf, old_sig=old, new_sig=new)
        for f, fns in d['retchg'].items():
            for s in fns:
                if not ok(s): continue
                for j, bf in use_idx.get(s, []):
                    if j > i and bf != f and bf not in d['touched'] and s not in commits[j]['d']['added'].get(bf, {}) and imports_from(commits[j]['sha'], bf, f, s, cwd):
                        edge('calls-return-changed', i, j, s, f, bf)
        for f, fns in d['funcs'].items():
            for s in fns:
                if not ok(s): continue
                for j in func_idx.get((f, s), []):
                    if j > i and s not in d['added'].get(f, {}): edge('edits-same-function', i, j, s, f, f)
                if s in d['retchg'].get(f, []) or s in d['added'].get(f, {}): continue
                for j, bf in use_idx.get(s, []):
                    if j > i and bf != f and bf not in d['touched'] and s not in commits[j]['d']['added'].get(bf, {}) and imports_from(commits[j]['sha'], bf, f, s, cwd):
                        edge('calls-body-changed', i, j, s, f, bf)
        # reverse-order signature: B re-signs F after A added callers
        for f, names in d['uses'].items():
            pass
    for j, B in enumerate(commits):
        for f, names in B['d']['resigned'].items():
            for s, (old, new) in names.items():
                if not ok(s): continue
                for i in range(j):
                    A = commits[i]
                    for af, au in A['d']['uses'].items():
                        if s in au and af != f and af not in B['d']['touched'] and s not in A['d']['added'].get(af, {}) and imports_from(A['sha'], af, f, s, cwd):
                            edge('calls-resigned-function (caller-first)', i, j, s, af, f, old_sig=old, new_sig=new)
    # dedupe
    seen = set(); uniq = []
    for e in edges:
        k = (e['rel'], e['symbol'], e['i'], e['j'], e['A_file'], e['B_file'])
        if k not in seen: seen.add(k); uniq.append(e)
    edges = uniq; print(f'{len(edges)} edges', file=sys.stderr)
    # PR branch dates (annotation)
    if a.fetch_pr_dates:
        need = sorted({commits[e['j']]['pr'] for e in edges if commits[e['j']]['pr'] and commits[e['j']]['pr'] not in pr_last})
        print(f'fetching {len(need)} PR dates', file=sys.stderr)
        for k, pr in enumerate(need):
            r = subprocess.run(['gh', 'pr', 'view', pr, '--repo', 'NVIDIA/TensorRT-LLM', '--json', 'commits,createdAt', '--jq', '[.createdAt, (.commits|map(.committedDate)|max)]|@tsv'], capture_output=True, text=True, timeout=60)
            try: created, last = r.stdout.strip().split('\t'); pr_last[pr] = {'created': created, 'last_commit': last}
            except ValueError: pr_last[pr] = {'created': None, 'last_commit': None}
            if k % 25 == 0: pcache_p.write_text(json.dumps(pr_last, indent=1))
        pcache_p.write_text(json.dumps(pr_last, indent=1))
    ts = lambda sv: int(dt.datetime.fromisoformat(sv.replace('Z', '+00:00')).timestamp()) if sv else None
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    human = lambda s: f'{s//86400}d {s%86400//3600}h {s%3600//60}m'
    rows = []
    for e in edges:
        A, B = commits[e['i']], commits[e['j']]
        last = ts(pr_last.get(B['pr'] or '', {}).get('last_commit')); stale = None if last is None else last < A['ts']
        rows.append({'relation': e['rel'], 'symbol': e['symbol'], 'early_commit': A['short'], 'early_pr': A['pr'], 'early_author': A['author'], 'early_merged': iso(A['ts']), 'early_subject': A['subject'], 'early_file': e['A_file'],
                     'late_commit': B['short'], 'late_pr': B['pr'], 'late_author': B['author'], 'late_merged': iso(B['ts']), 'late_subject': B['subject'], 'late_file': e['B_file'],
                     'gap_early_to_late': human(B['ts'] - A['ts']), 'gap_hours': round((B['ts'] - A['ts']) / 3600, 1), 'stale_branch': stale, 'late_branch_predates_early_merge_by': human(A['ts'] - last) if stale else None,
                     'unresolvable_at_ref': e.get('unresolvable_at_ref'), 'same_author': A['author'] == B['author'], 'old_sig': e.get('old_sig'), 'new_sig': e.get('new_sig')})
    order = ['uses-removed-symbol', 'imports-moved-module', 'calls-resigned-function', 'calls-resigned-function (caller-first)', 'calls-return-changed', 'calls-body-changed', 'uses-added-symbol', 'edits-added-symbol', 'edits-same-function']
    rows.sort(key=lambda r: (order.index(r['relation']), r['stale_branch'] is not True, r['early_merged']))
    (out / 'relations.json').write_text(json.dumps({'ref': a.ref, 'since': a.since, 'commits': n, 'edges': rows}, indent=1))
    with (out / 'relations.csv').open('w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ['relation']); w.writeheader(); w.writerows(rows)
    # summary
    by_rel = collections.Counter(r['relation'] for r in rows); stale_by_rel = collections.Counter(r['relation'] for r in rows if r['stale_branch'])
    dep_in = collections.Counter(r['early_commit'] for r in rows); dep_out = collections.Counter(r['late_commit'] for r in rows)
    md = [f'# Semantic relationships between commits — {a.ref} since {a.since}', '', f'{n} commits · {len(rows)} relationship edges · {sum(1 for r in rows if r["stale_branch"])} with the later PR branch finished before the earlier commit merged · {sum(1 for r in rows if r.get("unresolvable_at_ref"))} unresolvable at ref', '',
          '| Relation | Edges | of which stale-branch |', '|---|---|---|'] + [f'| {k} | {by_rel[k]} | {stale_by_rel[k]} |' for k in order if by_rel[k]]
    subj = {c['short']: c for c in commits}
    md += ['', '## Most depended-on earlier commits (in-degree)', '', '| Commit | PR | Merged | Edges | Subject |', '|---|---|---|---|---|'] + [f"| {s} | #{subj[s]['pr']} | {iso(subj[s]['ts'])} | {c} | {subj[s]['subject'][:90]} |" for s, c in dep_in.most_common(15)]
    md += ['', '## Later commits with most dependencies on earlier changes (out-degree)', '', '| Commit | PR | Merged | Edges | Subject |', '|---|---|---|---|---|'] + [f"| {s} | #{subj[s]['pr']} | {iso(subj[s]['ts'])} | {c} | {subj[s]['subject'][:90]} |" for s, c in dep_out.most_common(15)]
    md += ['', '## Break-risk relations (removed symbol / moved module still referenced, or interface change against a caller on a stale branch)', '', '| Relation | Symbol | Early | Late | Stale | Unresolvable | Files |', '|---|---|---|---|---|---|---|']
    for r in rows:
        if r['relation'] in ('uses-removed-symbol', 'imports-moved-module') or (r['relation'].startswith('calls-resigned') and r['stale_branch']) or (r['relation'] == 'calls-return-changed' and r['stale_branch']):
            md.append(f"| {r['relation']} | `{r['symbol']}` | {r['early_commit']} #{r['early_pr']} {r['early_merged']} | {r['late_commit']} #{r['late_pr']} {r['late_merged']} | {r['stale_branch']} | {r.get('unresolvable_at_ref')} | `{r['early_file']}` / `{r['late_file']}` |")
    (out / 'relations_summary.md').write_text('\n'.join(md) + '\n')
    print(dict(by_rel), file=sys.stderr)
if __name__ == '__main__':
    main()
