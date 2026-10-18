#!/usr/bin/env python3
"""For each (earlier A, later B) commit pair: find B's PR last L0_MergeRequest_PR build (OpenSearch stage_info),
its head commit, the merge-base of that head with main (GitHub compare) and the base's commit date.
Exclude pairs whose base date is later than A's merge time; keep the others (B's last CI base predates A)."""
import json, csv, collections, datetime as dt, subprocess, sys, urllib.request
from pathlib import Path
R = sys.argv[1]; out = Path(R) / 'semantic_relations60'
edges = json.load(open(out / 'relations.json'))['edges']
OS = 'http://gpuwa.nvidia.com/opensearch/df-swdl-trtllm-infra-ci-prod-stage_info-*/_search'
def q(body):
    req = urllib.request.Request(OS, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=60))['hits']['hits']
def gh(p):
    r = subprocess.run(['gh', 'api', p], capture_output=True, text=True, timeout=90)
    return json.loads(r.stdout) if r.returncode == 0 else None
cache_p = out / 'pr_last_ci_base.json'; cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
prs = sorted({e['late_pr'] for e in edges if e['late_pr']} | {e['early_pr'] for e in edges if e['early_pr']})
print(f'{len(prs)} PRs to resolve ({sum(1 for p in prs if p in cache)} cached)', file=sys.stderr)
for n, pr in enumerate(prs):
    if pr in cache: continue
    rec = {'pr': pr, 'last_build': None, 'last_build_ts': None, 'head': None, 'base': None, 'base_date': None, 'note': None}
    try:
        hits = q({'size': 1, 'query': {'bool': {'must': [{'term': {'s_trigger_mr_id': pr}}, {'term': {'s_job_name': 'LLM/main/L0_MergeRequest_PR'}}, {'term': {'s_stage_type': 'TopParallelBranch'}}]}}, 'sort': [{'ts_created': {'order': 'desc'}}], '_source': ['s_build_id', 's_trigger_mr_commit', 'ts_created']})
        if hits:
            s = hits[0]['_source']; rec['last_build'] = s['s_build_id']; rec['head'] = s.get('s_trigger_mr_commit')
            rec['last_build_ts'] = dt.datetime.fromtimestamp(s['ts_created'] / 1000, dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
            if rec['head']:
                c = gh(f"repos/NVIDIA/TensorRT-LLM/compare/main...{rec['head']}")
                if c and c.get('merge_base_commit'):
                    rec['base'] = c['merge_base_commit']['sha']; rec['base_date'] = c['merge_base_commit']['commit']['committer']['date']
                else: rec['note'] = 'compare failed (head not on GitHub?)'
        else: rec['note'] = 'no L0_MergeRequest_PR build in OpenSearch'
    except Exception as ex: rec['note'] = f'error: {ex}'[:120]
    cache[pr] = rec
    if n % 20 == 0: cache_p.write_text(json.dumps(cache, indent=1))
cache_p.write_text(json.dumps(cache, indent=1))
ts = lambda s: dt.datetime.fromisoformat(s.replace('Z', '+00:00')) if s else None
human = lambda sec: f'{int(sec)//86400}d {int(sec)%86400//3600}h {int(sec)%3600//60}m'
pairs = collections.OrderedDict()
for e in edges:
    k = (e['early_commit'], e['late_commit'])
    p = pairs.setdefault(k, {k2: e[k2] for k2 in ('early_commit','early_pr','early_merged','early_author','early_subject','late_commit','late_pr','late_merged','late_author','late_subject')} | {'time_diff': e['gap_early_to_late'], 'time_diff_hours': e['gap_hours'], 'relations': collections.Counter(), 'symbols': collections.defaultdict(set), 'edges': 0})
    p['relations'][e['relation']] += 1; p['symbols'][e['relation']].add(e['symbol']); p['edges'] += 1
kept, excluded, unknown = [], [], []
for p in pairs.values():
    rec = cache.get(p['late_pr'] or '', {}); bd = ts(rec.get('base_date')); am = ts(p['early_merged'])
    row = dict(p); row['relations'] = ' ; '.join(f'{k} x{v}' for k, v in sorted(p['relations'].items(), key=lambda kv: -kv[1])); row['symbols'] = ' ; '.join(f"{k}: " + ', '.join(sorted(v)[:6]) for k, v in p['symbols'].items())
    row.update({'late_pr_last_ci_build': rec.get('last_build'), 'late_pr_last_ci_build_ts': rec.get('last_build_ts'), 'late_pr_last_ci_head': (rec.get('head') or '')[:12], 'late_pr_last_ci_base': (rec.get('base') or '')[:12], 'late_pr_last_ci_base_date': rec.get('base_date'), 'note': rec.get('note')})
    if bd is None: row['base_vs_early'] = 'unknown'; unknown.append(row); continue
    delta = (am - bd).total_seconds()
    row['base_vs_early'] = 'base before early merge' if delta > 0 else 'base after early merge'; row['early_merge_minus_base'] = human(abs(delta)) + (' (base older)' if delta > 0 else ' (base newer)')
    (kept if delta > 0 else excluded).append(row)
for name, rows in (('relation_pairs_ci_base_kept', kept), ('relation_pairs_ci_base_excluded', excluded), ('relation_pairs_ci_base_unknown', unknown)):
    rows.sort(key=lambda r: (-r['edges'], r['time_diff_hours']))
    json.dump(rows, open(out / f'{name}.json', 'w'), indent=1)
    if rows:
        with open(out / f'{name}.csv', 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
md = [f'# Commit pairs filtered by the later PR\'s last CI base commit', '', f'{len(pairs)} pairs · kept {len(kept)} (base of the later PR\'s last `L0_MergeRequest_PR` build is older than the earlier commit\'s merge, i.e. the earlier change was never in the later PR\'s CI) · excluded {len(excluded)} (base newer than the earlier commit: the later PR\'s CI already contained it) · unknown {len(unknown)} (no CI build / head not resolvable)', '',
      '| # | Early (A) merged | Late (B) merged | B PR last CI build (UTC) | B last-CI base (date) | A merge − B base | Relationships |', '|---|---|---|---|---|---|---|']
for i, r in enumerate(kept, 1):
    md.append(f"| {i} | {r['early_commit'][:10]} #{r['early_pr']} {r['early_merged']} | {r['late_commit'][:10]} #{r['late_pr']} {r['late_merged']} | {r['late_pr_last_ci_build']} {r['late_pr_last_ci_build_ts']} | {r['late_pr_last_ci_base']} {(r['late_pr_last_ci_base_date'] or '')[:16]} | {r['early_merge_minus_base']} | {r['relations'][:110]} |")
(out / 'relation_pairs_ci_base_kept.md').write_text('\n'.join(md) + '\n')
print(f'pairs {len(pairs)} kept {len(kept)} excluded {len(excluded)} unknown {len(unknown)}', file=sys.stderr)
