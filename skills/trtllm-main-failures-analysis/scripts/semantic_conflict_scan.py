#!/usr/bin/env python3
"""Scan a git history window for semantic-conflict candidates between commits.

For every non-merge commit on <ref> since <since>, parse its Python diff and
record (a) function/class definitions it removes, adds or re-signs, (b) call /
import sites it adds, (c) the enclosing functions its hunks edit. Then pair
commits A (merged earlier) and B (merged later):

  R1 removed-symbol-use : A removes/renames `def NAME`; B adds a use/import of NAME.
  R2 signature-vs-caller: one commit changes NAME's signature, the other adds a
                          call to NAME, and the signature commit does not touch
                          the caller's file (either order).
  R3 same-function-edit : both edit the body of the same (file, function) and
                          B's code was authored before A merged (author date of
                          B < committer date of A), i.e. B's branch likely did
                          not contain A's change.

Each pair gets a `stale_branch` flag = B authored before A merged (squash
merges keep the PR's last author date), which is the cheapest local proxy for
"B's CI never saw A". Output: JSON + Markdown under --out-dir.
"""
from __future__ import annotations
import argparse, collections, datetime as dt, json, re, subprocess, sys
from pathlib import Path

DEF_RE = re.compile(r'^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\((.*)$')
CLASS_RE = re.compile(r'^(\s*)class\s+([A-Za-z_]\w*)')
CALL_RE = re.compile(r'(?<![\w.])([A-Za-z_]\w*)\s*\(')
IMPORT_RE = re.compile(r'^\s*from\s+[\w.]+\s+import\s+(.+)')
HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(.*)$')
KEYWORDS = {'if','for','while','return','print','len','isinstance','range','int','str','float','list','dict','set','tuple','super','type','getattr','setattr','hasattr','min','max','sum','any','all','sorted','enumerate','zip','map','filter','abs','bool','bytes','open','repr','format','assert','elif','with','not','and','or','in','is','lambda','yield','raise','except','del','pass','def','class','self','cls','torch','np','pytest','logger','await','async','object','property','staticmethod','classmethod','Exception','ValueError','RuntimeError','TypeError','KeyError','AssertionError','NotImplementedError','Optional','List','Dict','Tuple','Union','Any','Callable','Mock','MagicMock','patch','SimpleNamespace','dataclass','field','nn','F','os','sys','json','time','math','re','Path','partial','warnings','copy','deepcopy','iter','next','round','divmod','hash','id','vars','dir','input','slice','frozenset','memoryview','bytearray','complex','chr','ord','hex','bin','oct','pow','zip','reversed','callable','globals','locals','exec','eval','compile','issubclass','__import__','tensor','empty','zeros','ones','arange','cat','stack','device','dtype','shape','size','view','reshape','permute','contiguous','to','cuda','cpu','item','numpy','tolist','clone','detach','float16','bfloat16','float32'}

def git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True).stdout

def parse_commit(sha, cwd):
    diff = git(['show', '--format=', '--unified=0', '--no-color', sha, '--', '*.py', ':(exclude)*_pb2.py', ':(exclude)*_pb2_grpc.py', ':(exclude)*/_generated/*'], cwd)
    files = {}
    cur = None; removed = collections.defaultdict(set); added_defs = collections.defaultdict(dict); removed_defs = collections.defaultdict(dict)
    uses = collections.defaultdict(set); funcs = collections.defaultdict(set); touched = set()
    for line in diff.splitlines():
        if line.startswith('+++ b/'):
            cur = line[6:]; touched.add(cur); continue
        if line.startswith('--- ') or cur is None: continue
        m = HUNK_RE.match(line)
        if m:
            ctx = m.group(1)
            d = DEF_RE.match(ctx)
            if d: funcs[cur].add(d.group(2))
            continue
        if line.startswith('-'):
            body = line[1:]
            d = DEF_RE.match(body)
            if d: removed_defs[cur][d.group(2)] = ('L' if d.group(1)=='' else 'M', d.group(3).strip())
            c = CLASS_RE.match(body)
            if c: removed_defs[cur][c.group(2)] = ('L' if c.group(1)=='' else 'M', '<class>')
        elif line.startswith('+'):
            body = line[1:]
            d = DEF_RE.match(body)
            if d:
                added_defs[cur][d.group(2)] = ('L' if d.group(1)=='' else 'M', d.group(3).strip()); continue
            c = CLASS_RE.match(body)
            if c:
                added_defs[cur][c.group(2)] = ('L' if c.group(1)=='' else 'M', '<class>'); continue
            im = IMPORT_RE.match(body)
            if im:
                for name in re.split(r'[,\s()]+', im.group(1)):
                    name = name.strip()
                    if name and name != 'as' and name[0].isalpha() or (name and name[0] == '_'):
                        uses[cur].add(name)
            for name in CALL_RE.findall(body):
                if name not in KEYWORDS and len(name) > 3: uses[cur].add(name)
    out = {'touched': sorted(touched), 'funcs': {f: sorted(v) for f, v in funcs.items()}, 'uses': {f: sorted(v) for f, v in uses.items()},
           'removed': {}, 'resigned': {}, 'added': {}}
    for f in set(removed_defs) | set(added_defs):
        rd, ad = removed_defs.get(f, {}), added_defs.get(f, {})
        for name, (lvl, sig) in rd.items():
            if name not in ad:
                if lvl == 'L': out['removed'].setdefault(f, {})[name] = sig
            elif ad[name][1] != sig and sig != '<class>': out['resigned'].setdefault(f, {})[name] = (sig, ad[name][1])
        for name, (lvl, sig) in ad.items():
            if name not in rd: out['added'].setdefault(f, {})[name] = sig
    return out

BUILTINS = set(dir(__builtins__)) if isinstance(__builtins__, dict) is False else set(__builtins__)

def still_exported(ref, def_file, name, cwd):
    """True if `name` still appears in def_file's package (e.g. re-exported from a C++ binding) at ref."""
    pkg = def_file.rsplit('/', 1)[0]
    try: out = git(['grep', '-l', '-w', name, ref, '--', pkg], cwd)
    except subprocess.CalledProcessError: return False
    return bool(out.strip())

def imports_from(sha, use_file, def_file, name, cwd):
    """True if use_file at sha imports `name` from def_file's module (or is the same file)."""
    if use_file == def_file: return True
    mod = def_file[:-3].replace('/', '.')
    try: src = git(['show', f'{sha}:{use_file}'], cwd)
    except Exception: return False
    tail = mod.split('.')[-1]
    return bool(re.search(r'from\s+[\w.]*' + re.escape(tail) + r'\s+import[^\n]*\b' + re.escape(name) + r'\b', src)) or bool(re.search(re.escape(tail) + r'\.' + re.escape(name) + r'\b', src))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', required=True); ap.add_argument('--ref', default='origin/main'); ap.add_argument('--since', required=True)
    ap.add_argument('--out-dir', required=True); ap.add_argument('--max-def-count', type=int, default=3, help='ignore symbols defined more than N times in the repo (too generic)')
    a = ap.parse_args(); cwd = a.repo; out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    log = git(['log', '--no-merges', '--first-parent', f'--since={a.since}', '--format=%H%x09%ct%x09%at%x09%an%x09%s', a.ref], cwd).strip().splitlines()
    commits = []
    for l in log:
        sha, ct, at, an, subj = l.split('\t', 4)
        pr = re.search(r'\(#(\d+)\)\s*$', subj)
        commits.append({'sha': sha, 'short': sha[:12], 'commit_ts': int(ct), 'author_ts': int(at), 'author': an, 'subject': subj, 'pr': pr.group(1) if pr else None})
    commits.sort(key=lambda c: c['commit_ts'])
    print(f'{len(commits)} commits on {a.ref} since {a.since}', file=sys.stderr)
    # repo-wide def counts at ref (to drop generic names)
    grep = git(['grep', '-h', '-P', r'^\s*(def|class)\s+[A-Za-z_][A-Za-z0-9_]*', a.ref, '--', '*.py'], cwd)
    defcount = collections.Counter()
    for l in grep.splitlines():
        m = re.search(r'(def|class)\s+([A-Za-z_]\w*)', l)
        if m: defcount[m.group(2)] += 1
    for c in commits:
        c['d'] = parse_commit(c['sha'], cwd)
    # PR branch last-commit dates (squash merges carry the merge time as author date, so ask GitHub)
    cache_p = out / 'pr_last_commit.json'
    pr_last = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    for c in commits:
        if c['pr'] and c['pr'] not in pr_last:
            try:
                r = subprocess.run(['gh', 'pr', 'view', c['pr'], '--repo', 'NVIDIA/TensorRT-LLM', '--json', 'commits,createdAt', '--jq', '[.createdAt, (.commits|map(.committedDate)|max)]|@tsv'], capture_output=True, text=True, timeout=60)
                created, last = r.stdout.strip().split('\t')
                pr_last[c['pr']] = {'created': created, 'last_commit': last}
            except Exception as e:
                pr_last[c['pr']] = {'created': None, 'last_commit': None}
            cache_p.write_text(json.dumps(pr_last, indent=1))
    def ts(sv):
        return int(dt.datetime.fromisoformat(sv.replace('Z', '+00:00')).timestamp()) if sv else None
    for c in commits:
        info = pr_last.get(c['pr'] or '', {})
        c['branch_last_ts'] = ts(info.get('last_commit')) or c['author_ts']
        c['pr_created_ts'] = ts(info.get('created'))
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    human = lambda s: f'{s//86400} days {s%86400//3600} hours {s%3600//60} minutes'
    pairs = []
    for i, A in enumerate(commits):
        for B in commits[i+1:]:
            stale = B['branch_last_ts'] < A['commit_ts']
            dA, dB = A['d'], B['d']
            # R1: A removed NAME; B adds use of NAME
            for f, names in dA['removed'].items():
                for name in names:
                    if defcount[name] > a.max_def_count or len(name) <= 3 or name in BUILTINS: continue
                    for bf, bu in dB['uses'].items():
                        if name in bu and name not in dB['added'].get(bf, {}) and (defcount[name] == 0 or imports_from(B['sha'], bf, f, name, cwd)):
                            pairs.append({'rule': 'R1 removed-symbol-use', 'symbol': name, 'A': A, 'B': B, 'A_file': f, 'B_file': bf, 'stale_branch': stale,
                                          'still_defined_at_ref': defcount[name] > 0, 'still_exported_at_ref': still_exported(a.ref, f, name, cwd)})
            # R2: signature change vs new caller (both orders)
            for X, Y, order in ((A, B, 'sig-first'), (B, A, 'caller-first')):
                for f, names in X['d']['resigned'].items():
                    for name, (old, new) in names.items():
                        if defcount[name] > a.max_def_count or len(name) <= 3: continue
                        for yf, yu in Y['d']['uses'].items():
                            if name in yu and yf not in X['d']['touched'] and yf != f and name not in Y['d']['added'].get(yf, {}) and imports_from(Y['sha'], yf, f, name, cwd):
                                pairs.append({'rule': f'R2 signature-vs-caller ({order})', 'symbol': name, 'A': A, 'B': B, 'A_file': f if X is A else yf, 'B_file': yf if X is A else f,
                                              'old_sig': old, 'new_sig': new, 'stale_branch': stale})
            # R3: same function edited, B authored before A merged
            if stale:
                for f, fs in dA['funcs'].items():
                    common = set(fs) & set(dB['funcs'].get(f, []))
                    for name in common:
                        pairs.append({'rule': 'R3 same-function-edit (stale branch)', 'symbol': name, 'A': A, 'B': B, 'A_file': f, 'B_file': f, 'stale_branch': True})
    # dedupe
    seen = set(); uniq = []
    for p in pairs:
        k = (p['rule'], p['symbol'], p['A']['sha'], p['B']['sha'], p['A_file'], p['B_file'])
        if k in seen: continue
        seen.add(k); uniq.append(p)
    rank = {'R1 removed-symbol-use': 0, 'R2 signature-vs-caller (sig-first)': 1, 'R2 signature-vs-caller (caller-first)': 1, 'R3 same-function-edit (stale branch)': 2}
    uniq.sort(key=lambda p: (rank[p['rule']], not p['stale_branch'], -p['B']['commit_ts']))
    rows = []
    for p in uniq:
        A, B = p['A'], p['B']
        rows.append({'rule': p['rule'], 'symbol': p['symbol'], 'stale_branch': p['stale_branch'],
                     'early_commit': A['short'], 'early_pr': A['pr'], 'early_merged': iso(A['commit_ts']), 'early_subject': A['subject'], 'early_file': p['A_file'],
                     'late_commit': B['short'], 'late_pr': B['pr'], 'late_merged': iso(B['commit_ts']), 'late_branch_last_commit': iso(B['branch_last_ts']), 'late_subject': B['subject'], 'late_file': p['B_file'],
                     'early_to_late': human(B['commit_ts'] - A['commit_ts']), 'late_branch_predates_early_merge_by': human(A['commit_ts'] - B['branch_last_ts']) if p['stale_branch'] else None,
                     'old_sig': p.get('old_sig'), 'new_sig': p.get('new_sig'), 'still_defined_at_ref': p.get('still_defined_at_ref'), 'still_exported_at_ref': p.get('still_exported_at_ref')})
    (out / 'semantic_scan.json').write_text(json.dumps({'ref': a.ref, 'since': a.since, 'commits': len(commits), 'pairs': rows}, indent=1))
    md = [f'# Semantic-conflict scan — {a.ref} since {a.since} ({len(commits)} commits, {len(rows)} candidate pairs)', '',
          '| Rule | Symbol | Early (A) | Late (B) | B branch predates A merge | Gap A→B | Files |', '|---|---|---|---|---|---|---|']
    for r in rows:
        note = ' (name still present in package at ref: likely re-export cleanup)' if r.get('still_exported_at_ref') else ''
        md.append(f"| {r['rule']}{note} | `{r['symbol']}` | {r['early_commit']} #{r['early_pr']} {r['early_merged']} | {r['late_commit']} #{r['late_pr']} {r['late_merged']} | {'yes, by ' + r['late_branch_predates_early_merge_by'] if r['stale_branch'] else 'no'} | {r['early_to_late']} | `{r['early_file']}` / `{r['late_file']}` |")
    (out / 'semantic_scan.md').write_text('\n'.join(md) + '\n')
    print(f'{len(rows)} candidate pairs -> {out}/semantic_scan.md', file=sys.stderr)
    counts = collections.Counter(r['rule'] for r in rows); print(dict(counts), file=sys.stderr)

if __name__ == '__main__':
    main()
