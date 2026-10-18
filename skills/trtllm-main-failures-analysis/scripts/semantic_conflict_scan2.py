#!/usr/bin/env python3
"""Semantic-conflict scan v2 over a git window (interface, rename/move, return-value, body-change rules).

For every first-parent non-merge commit on <ref> since <since>, parse the Python diff
(with rename detection) and record:
  removed module-level defs/classes/variables, re-signed defs, renamed/deleted files,
  functions whose body / return statements changed, added uses (calls, attribute calls,
  imports with module paths).
Pair an earlier commit A with a later commit B:
  R1 removed-symbol-use : A removes/renames module-level NAME; B adds use/import of NAME.
  R4 moved-module-import: A renames/deletes module M; B adds `import M` / `from M import`.
  R2 signature-vs-caller: A re-signs F; B adds a call to F imported from A's module (either order).
  R5 return-value-change: A changes `return` statements of F; B adds a use of F's result.
  R6 body-change-vs-new-caller: A edits F's body; B (branch finished before A merged) adds a call to F.
  R3 same-function-edit : A and B edit the same function; B's branch predates A's merge.
`stale_branch` = B's PR last branch commit (GitHub) < A's merge time. For R1/R4 the scan also
checks whether the symbol / module still resolves at <ref> (`unresolvable_at_ref`).
"""
from __future__ import annotations
import argparse, collections, datetime as dt, json, re, subprocess, sys
from pathlib import Path

DEF_RE = re.compile(r'^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\((.*)$')
CLASS_RE = re.compile(r'^(\s*)class\s+([A-Za-z_]\w*)')
VAR_RE = re.compile(r'^([A-Za-z_]\w*)\s*(?::\s*[^=]+)?=\s*[^=]')
CALL_RE = re.compile(r'(?<![\w.])([A-Za-z_]\w*)\s*\(')
ATTRCALL_RE = re.compile(r'\.([A-Za-z_]\w*)\s*\(')
ASSIGN_CALL_RE = re.compile(r'(?:=|return|yield|in|,|\()\s*(?:await\s+)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(')
FROM_IMPORT_RE = re.compile(r'^\s*from\s+([\w.]+)\s+import\s+(.+)')
IMPORT_RE = re.compile(r'^\s*import\s+([\w.]+)')
HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(.*)$')
RETURN_RE = re.compile(r'^\s*return\b')
GENERIC = {'main','run','test','setup','forward','init','call','get','set','update','create','build','load','save','parse','start','stop','close','open','reset','step','add','remove','apply','check','validate','process','execute','handle','register','configure','prepare','finalize','cleanup','shutdown','wait','send','recv','read','write','put','pop','push','copy','clone','new','make','from_dict','to_dict','items','keys','values','warmup','_','__init__','__call__','__repr__','__str__','__eq__','__hash__','__len__','__iter__','__getitem__','__setitem__','__enter__','__exit__','__del__','__post_init__'}
BUILTINS = set(dir(__builtins__)) if not isinstance(__builtins__, dict) else set(__builtins__)

def git(args, cwd, check=True):
    r = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode: raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r.stdout

def modpath(f): return f[:-3].replace('/', '.') if f.endswith('.py') else f.replace('/', '.')

def parse_commit(sha, cwd):
    diff = git(['show', '--format=', '--unified=0', '-M', '--no-color', sha, '--', '*.py', ':(exclude)*_pb2.py', ':(exclude)*_pb2_grpc.py', ':(exclude)*/_generated/*', ':(exclude)3rdparty/*'], cwd)
    cur = None; curfn = None
    rem = collections.defaultdict(dict); add = collections.defaultdict(dict)
    uses = collections.defaultdict(set); imports = collections.defaultdict(set); mods = collections.defaultdict(set)
    funcs = collections.defaultdict(set); retchg = collections.defaultdict(set); touched = set(); renamed = {}; deleted = set(); pending_from = None
    for line in diff.splitlines():
        if line.startswith('rename from '): pending_from = line[12:]; continue
        if line.startswith('rename to ') and pending_from: renamed[pending_from] = line[10:]; pending_from = None; continue
        if line.startswith('deleted file mode'): deleted.add('__pending__'); continue
        if line.startswith('--- a/'):
            if '__pending__' in deleted: deleted.discard('__pending__'); deleted.add(line[6:])
            continue
        if line.startswith('+++ b/'): cur = line[6:]; touched.add(cur); curfn = None; continue
        if line.startswith('+++ /dev/null'): continue
        if cur is None: continue
        m = HUNK_RE.match(line)
        if m:
            d = DEF_RE.match(m.group(1)); curfn = d.group(2) if d else None
            if curfn: funcs[cur].add(curfn)
            continue
        if line[:1] not in '+-': continue
        body = line[1:]; sign = line[0]
        d = DEF_RE.match(body)
        if d:
            lvl = 'L' if d.group(1) == '' else 'M'
            (rem if sign == '-' else add)[cur][d.group(2)] = (lvl, d.group(3).strip())
            if sign == '+': curfn = d.group(2); funcs[cur].add(curfn)
            continue
        c = CLASS_RE.match(body)
        if c:
            (rem if sign == '-' else add)[cur][c.group(2)] = ('L' if c.group(1) == '' else 'M', '<class>'); continue
        v = VAR_RE.match(body)
        if v and not body.startswith((' ', '\t')):
            (rem if sign == '-' else add)[cur][v.group(1)] = ('L', '<var>')
        if RETURN_RE.match(body) and curfn: retchg[cur].add(curfn)
        if sign == '+':
            fi = FROM_IMPORT_RE.match(body)
            if fi:
                mods[cur].add(fi.group(1))
                for name in re.split(r'[,\s()]+', fi.group(2)):
                    if name and name != 'as' and (name[0].isalpha() or name[0] == '_'): imports[cur].add((fi.group(1), name)); uses[cur].add(name)
                continue
            im = IMPORT_RE.match(body)
            if im: mods[cur].add(im.group(1)); continue
            for name in CALL_RE.findall(body) + ATTRCALL_RE.findall(body):
                if name not in BUILTINS and name not in GENERIC and len(name) > 3: uses[cur].add(name)
    out = {'touched': sorted(touched), 'renamed': renamed, 'deleted': sorted(deleted), 'funcs': {f: sorted(v) for f, v in funcs.items()}, 'retchg': {f: sorted(v) for f, v in retchg.items()},
           'uses': {f: sorted(v) for f, v in uses.items()}, 'imports': {f: sorted(v) for f, v in imports.items()}, 'mods': {f: sorted(v) for f, v in mods.items()}, 'removed': {}, 'resigned': {}, 'added': {}}
    for f in set(rem) | set(add):
        rd, ad = rem.get(f, {}), add.get(f, {})
        for name, (lvl, sig) in rd.items():
            if name not in ad:
                if lvl == 'L' and f not in deleted: out['removed'].setdefault(f, {})[name] = sig
            elif ad[name][1] != sig and sig not in ('<class>', '<var>'): out['resigned'].setdefault(f, {})[name] = (sig, ad[name][1])
        for name, (lvl, sig) in ad.items():
            if name not in rd: out['added'].setdefault(f, {})[name] = sig
    return out

def imports_from(sha, use_file, def_file, name, cwd):
    if use_file == def_file: return True
    tail = modpath(def_file).split('.')[-1]
    try: src = git(['show', f'{sha}:{use_file}'], cwd)
    except subprocess.CalledProcessError: return False
    return bool(re.search(r'from\s+[\w.]*' + re.escape(tail) + r'\s+import[^\n]*\b' + re.escape(name) + r'\b', src)) or bool(re.search(r'\b' + re.escape(tail) + r'\.' + re.escape(name) + r'\b', src))

def still_exported(ref, def_file, name, cwd):
    pkg = def_file.rsplit('/', 1)[0]
    return bool(git(['grep', '-l', '-w', name, ref, '--', pkg], cwd, check=False).strip())

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--repo', required=True); ap.add_argument('--ref', default='origin/main'); ap.add_argument('--since', required=True); ap.add_argument('--out-dir', required=True); ap.add_argument('--max-def-count', type=int, default=3)
    a = ap.parse_args(); cwd = a.repo; out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    log = git(['log', '--no-merges', '--first-parent', f'--since={a.since}', '--format=%H%x09%ct%x09%s', a.ref], cwd).strip().splitlines()
    commits = []
    for l in log:
        sha, ct, subj = l.split('\t', 2); pr = re.search(r'\(#(\d+)\)\s*$', subj)
        commits.append({'sha': sha, 'short': sha[:12], 'commit_ts': int(ct), 'subject': subj, 'pr': pr.group(1) if pr else None})
    commits.sort(key=lambda c: c['commit_ts']); print(f'{len(commits)} commits', file=sys.stderr)
    cache_d = out / 'diff_cache.json'; dcache = json.loads(cache_d.read_text()) if cache_d.exists() else {}
    for i, c in enumerate(commits):
        if c['sha'] not in dcache: dcache[c['sha']] = parse_commit(c['sha'], cwd)
        c['d'] = dcache[c['sha']]
        if i % 200 == 0: cache_d.write_text(json.dumps(dcache))
    cache_d.write_text(json.dumps(dcache))
    defcount = collections.Counter()
    for l in git(['grep', '-h', '-P', r'^\s*(def|class)\s+[A-Za-z_][A-Za-z0-9_]*', a.ref, '--', '*.py'], cwd).splitlines():
        m = re.search(r'(def|class)\s+([A-Za-z_]\w*)', l)
        if m: defcount[m.group(2)] += 1
    ref_files = set(git(['ls-tree', '-r', '--name-only', a.ref], cwd).splitlines())
    ok = lambda n: defcount[n] <= a.max_def_count and len(n) > 3 and n not in BUILTINS and n not in GENERIC
    raw = []
    for i, A in enumerate(commits):
        dA = A['d']
        if not any([dA['removed'], dA['renamed'], dA['deleted'], dA['resigned'], dA['retchg'], dA['funcs']]): continue
        for B in commits[i+1:]:
            dB = B['d']
            if not dB['uses'] and not dB['mods'] and not dB['funcs']: continue
            for f, names in dA['removed'].items():
                for name in names:
                    if not ok(name): continue
                    for bf, bu in dB['uses'].items():
                        if name in bu and name not in dB['added'].get(bf, {}) and (defcount[name] == 0 or imports_from(B['sha'], bf, f, name, cwd)):
                            raw.append(dict(rule='R1 removed-symbol-use', symbol=name, A=A, B=B, A_file=f, B_file=bf, unresolvable=not still_exported(a.ref, f, name, cwd)))
            oldmods = {modpath(o): n for o, n in dA['renamed'].items()} | {modpath(dl): None for dl in dA['deleted']}
            for om, nm in oldmods.items():
                if om.endswith('.__init__'): om = om[:-9]
                for bf, bm in dB['mods'].items():
                    if any(m == om or m.startswith(om + '.') for m in bm):
                        raw.append(dict(rule='R4 moved-module-import', symbol=om, A=A, B=B, A_file=f'{om} -> {modpath(nm) if nm else "<deleted>"}', B_file=bf, unresolvable=(om.replace('.', '/') + '.py') not in ref_files and (om.replace('.', '/') + '/__init__.py') not in ref_files))
            for X, Y, order in ((A, B, 'sig-first'), (B, A, 'caller-first')):
                for f, names in X['d']['resigned'].items():
                    for name, (old, new) in names.items():
                        if not ok(name): continue
                        for yf, yu in Y['d']['uses'].items():
                            if name in yu and yf not in X['d']['touched'] and yf != f and name not in Y['d']['added'].get(yf, {}) and imports_from(Y['sha'], yf, f, name, cwd):
                                raw.append(dict(rule=f'R2 signature-vs-caller ({order})', symbol=name, A=A, B=B, A_file=f if X is A else yf, B_file=yf if X is A else f, old_sig=old, new_sig=new))
            for f, fns in dA['retchg'].items():
                for name in fns:
                    if not ok(name): continue
                    for bf, bu in dB['uses'].items():
                        if name in bu and bf not in dA['touched'] and name not in dB['added'].get(bf, {}) and imports_from(B['sha'], bf, f, name, cwd):
                            raw.append(dict(rule='R5 return-value-change', symbol=name, A=A, B=B, A_file=f, B_file=bf))
            for f, fns in dA['funcs'].items():
                for name in fns:
                    if not ok(name): continue
                    if name in dB['funcs'].get(f, []): raw.append(dict(rule='R3 same-function-edit', symbol=name, A=A, B=B, A_file=f, B_file=f, needs_stale=True))
                    for bf, bu in dB['uses'].items():
                        if name in bu and bf != f and bf not in dA['touched'] and name not in dB['added'].get(bf, {}) and name not in dA['retchg'].get(f, []) and imports_from(B['sha'], bf, f, name, cwd):
                            raw.append(dict(rule='R6 body-change-vs-new-caller', symbol=name, A=A, B=B, A_file=f, B_file=bf, needs_stale=True))
    print(f'{len(raw)} raw pairs; resolving PR branch dates', file=sys.stderr)
    cache_p = out / 'pr_last_commit.json'; pr_last = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    prs = {p['B']['pr'] for p in raw if p['B']['pr']}
    for pr in sorted(prs):
        if pr in pr_last: continue
        r = subprocess.run(['gh', 'pr', 'view', pr, '--repo', 'NVIDIA/TensorRT-LLM', '--json', 'commits,createdAt', '--jq', '[.createdAt, (.commits|map(.committedDate)|max)]|@tsv'], capture_output=True, text=True, timeout=60)
        try: created, last = r.stdout.strip().split('\t'); pr_last[pr] = {'created': created, 'last_commit': last}
        except ValueError: pr_last[pr] = {'created': None, 'last_commit': None}
        cache_p.write_text(json.dumps(pr_last, indent=1))
    ts = lambda sv: int(dt.datetime.fromisoformat(sv.replace('Z', '+00:00')).timestamp()) if sv else None
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    human = lambda s: f'{s//86400} days {s%86400//3600} hours {s%3600//60} minutes'
    seen = set(); rows = []
    for p in raw:
        A, B = p['A'], p['B']; k = (p['rule'], p['symbol'], A['sha'], B['sha'], p['A_file'], p['B_file'])
        if k in seen: continue
        seen.add(k)
        last = ts(pr_last.get(B['pr'] or '', {}).get('last_commit')); stale = last is not None and last < A['commit_ts']
        if p.get('needs_stale') and not stale: continue
        rows.append({'rule': p['rule'], 'symbol': p['symbol'], 'stale_branch': stale, 'unresolvable_at_ref': p.get('unresolvable'),
                     'early_commit': A['short'], 'early_pr': A['pr'], 'early_merged': iso(A['commit_ts']), 'early_subject': A['subject'], 'early_file': p['A_file'],
                     'late_commit': B['short'], 'late_pr': B['pr'], 'late_merged': iso(B['commit_ts']), 'late_subject': B['subject'], 'late_file': p['B_file'],
                     'late_branch_last_commit': iso(last) if last else None, 'early_to_late': human(B['commit_ts'] - A['commit_ts']),
                     'late_branch_predates_early_merge_by': human(A['commit_ts'] - last) if stale else None, 'old_sig': p.get('old_sig'), 'new_sig': p.get('new_sig')})
    order = {'R1': 0, 'R4': 0, 'R2': 1, 'R5': 2, 'R6': 3, 'R3': 4}
    rows.sort(key=lambda r: (order[r['rule'][:2]], not r.get('unresolvable_at_ref'), not r['stale_branch'], -ts(r['late_merged'].replace('Z','+00:00')) if False else 0))
    (out / 'semantic_scan.json').write_text(json.dumps({'ref': a.ref, 'since': a.since, 'commits': len(commits), 'pairs': rows}, indent=1))
    md = [f'# Semantic-conflict scan v2 — {a.ref} since {a.since} ({len(commits)} commits, {len(rows)} candidate pairs)', '', '| Rule | Symbol | Early (A) | Late (B) | B branch predates A merge | Unresolvable at ref | Gap A→B | Files |', '|---|---|---|---|---|---|---|---|']
    for r in rows:
        md.append(f"| {r['rule']} | `{r['symbol']}` | {r['early_commit']} #{r['early_pr']} {r['early_merged']} | {r['late_commit']} #{r['late_pr']} {r['late_merged']} | {('yes, by ' + r['late_branch_predates_early_merge_by']) if r['stale_branch'] else 'no'} | {r.get('unresolvable_at_ref')} | {r['early_to_late']} | `{r['early_file']}` / `{r['late_file']}` |")
    (out / 'semantic_scan.md').write_text('\n'.join(md) + '\n')
    print(dict(collections.Counter(r['rule'] for r in rows)), 'stale:', sum(r['stale_branch'] for r in rows), 'unresolvable:', sum(1 for r in rows if r.get('unresolvable_at_ref')), file=sys.stderr)

if __name__ == '__main__':
    main()
