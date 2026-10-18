#!/usr/bin/env python3
"""Test-leak check: did the culprit PR's own pre-merge CI run a given test, and what happened?

Walks EVERY LLM/main/L0_MergeRequest_PR build recorded for the PR in OpenSearch stage_info,
and for each build reads test_info for (a) the test itself and (b) its unittest roll-up
`test_unittests_v2[<turtle>]` when the test lives under tests/unittest. Also lists the PR's
`/bot …` comments (GitHub) so reruns / `/bot skip` can be matched to builds.

Output: one row per build with stage status, the test's status, the roll-up's rows
(FAILED + PASSED duplicates mean an isolated rerun), and `Reused from previous pipeline`
skips - the signature of the reuse gate letting a verified-red case through.

Usage:
  check_pr_test_history.py --pr 20024 \
    --test 'unittest/scripts/test_subprocess_timeouts.py::test_subprocess_run_calls_have_timeouts' \
    [--stage-substring CPU-Generic] --out <run_dir>/pr<PR>_ci_history.json [--md-out …]
"""
import argparse, datetime as dt, json, re, subprocess, sys, urllib.request

OS = 'http://gpuwa.nvidia.com/opensearch/df-swdl-trtllm-infra-ci-prod-%s-*/_search'
REPO = 'NVIDIA/TensorRT-LLM'


def q(index, body):
    req = urllib.request.Request(OS % index, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    return [h['_source'] for h in json.load(urllib.request.urlopen(req, timeout=90))['hits']['hits']]


def ts(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')


def gh_json(path):
    r = subprocess.run(['gh', 'api', path], capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout) if r.returncode == 0 else []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pr', required=True)
    ap.add_argument('--test', required=True, help='dashboard entity_name, e.g. unittest/x/test_y.py::test_z')
    ap.add_argument('--stage-substring', default=None, help='only report stages containing this (default: stages where the test appears)')
    ap.add_argument('--out', required=True)
    ap.add_argument('--md-out')
    a = ap.parse_args()

    short = a.test.split('::')[-1]
    turtle = None
    m = re.match(r'(unittest/[^:]+)/[^/]+\.py', a.test)
    if m:
        turtle = m.group(1)
    rollup = f'test_unittests_v2[{turtle}]' if turtle else None

    # 1. every build + its TopParallelBranch stages
    stages = q('stage_info', {'size': 2000, 'sort': [{'ts_created': {'order': 'asc'}}],
                              '_source': ['s_build_id', 's_stage_name', 's_status', 'ts_created', 's_trigger_mr_commit', 's_failure_reason', 's_skipped_message'],
                              'query': {'bool': {'must': [{'term': {'s_trigger_mr_id': a.pr}}, {'term': {'s_job_name': 'LLM/main/L0_MergeRequest_PR'}},
                                                          {'term': {'s_stage_type': 'TopParallelBranch'}}]}}})
    builds = {}
    for s in stages:
        b = builds.setdefault(s['s_build_id'], {'build': s['s_build_id'], 'created': ts(s['ts_created']), 'head': (s.get('s_trigger_mr_commit') or '')[:10], 'stages': {}})
        b['stages'][s['s_stage_name']] = {'status': s['s_status'], 'failure_reason': s.get('s_failure_reason'), 'skipped_message': s.get('s_skipped_message')}
    # 1b. stage_info can lag/miss builds (PR 17495: builds 63408..63613 absent) - union builds seen in test_info
    extra = q('test_info', {'size': 2000, 'sort': [{'ts_created': {'order': 'asc'}}],
                            '_source': ['s_build_id', 's_job_name', 'ts_created', 's_trigger_mr_commit'],
                            'query': {'bool': {'must': [{'term': {'s_trigger_mr_id': a.pr}}, {'term': {'s_job_name': 'LLM/main/L0_MergeRequest_PR'}},
                                                        {'wildcard': {'s_test_name': f'*{short}*'}}]}}})
    # test_info ts_created has been Pacific-shifted on some days (2026-10-02) and plain UTC on others
    # (2026-10-08, 2026-10-16): calibrate against stage_info for builds present in both.
    shift_ms = 0
    stage_first = {b_id: min(int(s['ts_created']) for s in stages if s['s_build_id'] == b_id) for b_id in builds}
    for s in extra:
        if s['s_build_id'] in stage_first and int(s['ts_created']) < stage_first[s['s_build_id']] - 30 * 60 * 1000:
            shift_ms = 7 * 3600 * 1000  # test rows "before" their stage started -> the index is Pacific-shifted
            break
    n_extra = 0
    for s in extra:
        if s['s_build_id'] not in builds:
            builds[s['s_build_id']] = {'build': s['s_build_id'], 'created': ts(int(s['ts_created']) + shift_ms) + (' (from test_info, +7h)' if shift_ms else ' (from test_info)'),
                                       'head': (s.get('s_trigger_mr_commit') or '')[:10], 'stages': {}}
            n_extra += 1
    builds = dict(sorted(builds.items(), key=lambda kv: int(kv[0])))
    print(f'PR {a.pr}: {len(builds)} L0_MergeRequest_PR build(s) ({n_extra} only in test_info)', file=sys.stderr)

    # 2. per build: the test and its roll-up in test_info
    test_file = 'tests/' + a.test.split('::')[0] if '::' in a.test or a.test.endswith('.py') else None
    file_cache: dict[str, bool | None] = {}

    def file_on_head(head: str):
        """Does the test file exist on this tested head? None when unknown (no head / API error)."""
        if not test_file or not head:
            return None
        if head not in file_cache:
            r = subprocess.run(['gh', 'api', f'repos/{REPO}/contents/{test_file}?ref={head}'], capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                file_cache[head] = True
            elif 'HTTP 404' in (r.stderr + r.stdout) or 'Not Found' in (r.stderr + r.stdout):
                file_cache[head] = False
            else:
                file_cache[head] = None  # network/auth error: unknown, do not claim absence
        return file_cache[head]

    # hosting stages seen anywhere in this PR's builds (fallback when a build has no rows for the test)
    hosting: set[str] = set()
    for b in builds.values():
        should = [{'wildcard': {'s_test_name': f'*{short}*'}}]
        if rollup:
            # sharded roll-ups are named test_unittests_v2[unittest/llmapi -m "part0"]; match the prefix
            should.append({'wildcard': {'s_test_name': rollup[:-1] + '*'}})
        rows = q('test_info', {'size': 200, 'sort': [{'ts_created': {'order': 'asc'}}],
                               '_source': ['s_test_name', 's_status', 's_stage_name', 's_skipped_message', 's_short_error_msg', 's_stage_status', 'l_e2e_time_ms'],
                               'query': {'bool': {'must': [{'term': {'s_build_id': b['build']}}], 'should': should, 'minimum_should_match': 1}}})
        b['test_rows'] = [{'stage': r['s_stage_name'], 'status': r['s_status'], 'skip': r.get('s_skipped_message') or '', 'error': (r.get('s_short_error_msg') or '')[:160]}
                          for r in rows if short in r['s_test_name']]
        b['rollup_rows'] = [{'stage': r['s_stage_name'], 'status': r['s_status'], 'skip': r.get('s_skipped_message') or '', 'error': (r.get('s_short_error_msg') or '')[:160], 'e2e_ms': r.get('l_e2e_time_ms')}
                            for r in rows if rollup and (r['s_test_name'] == rollup or r['s_test_name'].startswith(rollup[:-1] + ' '))]
        hosting |= {x['stage'] for x in b['test_rows']}  # stages where the test itself ran at least once in this PR
    if not hosting:
        # the test never ran in this PR: learn its hosting stage(s) from any recent run on main
        rows = q('test_info', {'size': 50, 'sort': [{'ts_created': {'order': 'desc'}}], '_source': ['s_stage_name'],
                               'query': {'bool': {'must': [{'wildcard': {'s_test_name': f'*{short}*'}}, {'term': {'s_job_name': 'LLM/main/L0_MergeRequest_PR'}}]}}})
        hosting = {r['s_stage_name'] for r in rows}
    for b in builds.values():
        st = {x['stage'] for x in b['test_rows'] + b['rollup_rows']}
        if a.stage_substring:
            st |= {s for s in b['stages'] if a.stage_substring in s}
        if not st:
            st = {s for s in b['stages'] if s in hosting}  # stage ran in this build even though the test left no row
        b['relevant_stages'] = {s: b['stages'][s] for s in sorted(st) if s in b['stages']}
        # verdict per build
        tstat = {x['status'] for x in b['test_rows']}
        rstat = [x['status'] for x in b['rollup_rows']]
        reused = any('Reused from previous pipeline' in x['skip'] for x in b['rollup_rows'] + b['test_rows'])
        b['test_file_on_head'] = None if tstat else file_on_head(b['head'])
        if 'FAILED' in tstat:
            b['verdict'] = 'test FAILED'
        elif 'PASSED' in tstat:
            b['verdict'] = 'test PASSED'
        elif b['test_file_on_head'] is False:
            # no row for the test because the file is not in this branch: semantic-conflict signature,
            # decided before any roll-up/reuse reading (those rows belong to other tests of the turtle)
            ran = sorted(b['relevant_stages'] or {s for x in b['rollup_rows'] for s in [x['stage']]})
            ran_txt = ', '.join(f"{s}={b['stages'][s]['status']}" for s in ran if s in b['stages']) if len(ran) <= 4 else f'{len(ran)} stages ran'
            b['verdict'] = 'TEST FILE ABSENT FROM BRANCH' + (f' (stage ran: {ran_txt})' if ran_txt else ' (stage not scheduled)')
        elif reused:
            b['verdict'] = 'NOT RUN - roll-up reused (SKIPPED: Reused from previous pipeline)'
        elif tstat and tstat <= {'SKIPPED'}:
            skips = sorted({x['skip'] for x in b['test_rows'] if x['skip'] and 'Ray orchestrator' not in x['skip']})
            b['verdict'] = 'test SKIPPED (waived: ' + '; '.join(skips) + ')' if skips else 'test SKIPPED'
        elif rstat and 'FAILED' in rstat:
            b['verdict'] = 'roll-up FAILED (other culprit)'
        else:
            ran = ','.join(f"{s}={v['status']}" for s, v in b['relevant_stages'].items())
            if rstat and set(rstat) == {'PASSED'}:
                b['verdict'] = 'roll-up PASSED, test absent'
            elif not b['relevant_stages']:
                b['verdict'] = 'stage not scheduled'
            else:
                b['verdict'] = f'no test_info rows (stage {ran})'
            if b['test_file_on_head'] is None and test_file:
                b['verdict'] += ' [file presence on head unknown]'
        if 'FAILED' in rstat and 'PASSED' in rstat:
            b['verdict'] += ' + isolated-rerun PASSED row (reuse hazard)'
        del b['stages']

    # 3. /bot comments
    comments = gh_json(f'repos/{REPO}/issues/{a.pr}/comments?per_page=100')
    bot = [{'at': c['created_at'], 'user': c['user']['login'], 'cmd': c['body'].strip().split('\n')[0][:120]} for c in comments if c.get('body', '').lstrip().startswith('/bot')]
    pr = gh_json(f'repos/{REPO}/pulls/{a.pr}')
    merged_head = (pr.get('head') or {}).get('sha', '')[:10] if isinstance(pr, dict) else ''
    built_heads = {b['head'] for b in builds.values()}

    out = {'pr': a.pr, 'test': a.test, 'rollup': rollup, 'merged_at': pr.get('merged_at') if isinstance(pr, dict) else None,
           'merged_head': merged_head, 'merged_head_was_built': merged_head in built_heads,
           'builds': list(builds.values()), 'bot_commands': bot}
    failed = [b['build'] for b in out['builds'] if b['verdict'].startswith('test FAILED')]
    last = out['builds'][-1] if out['builds'] else None
    absent_last = bool(last) and last['verdict'].startswith('TEST FILE ABSENT FROM BRANCH')
    out['summary'] = {'builds_where_test_failed': failed, 'builds_where_test_passed': [b['build'] for b in out['builds'] if b['verdict'].startswith('test PASSED')],
                      'last_build': last['build'] if last else None, 'last_build_verdict': last['verdict'] if last else None,
                      'self_waived_in_pr': bool(last) and last['verdict'].startswith('test SKIPPED (waived'),
                      'test_file_absent_from_branch': absent_last,
                      'test_leak': bool(failed) and (last is None or not last['verdict'].startswith('test PASSED'))}
    if out['summary']['self_waived_in_pr'] and failed:
        out['summary']['note'] = 'case failed in this PR and the PR then waived it; a Regression hidden by its own waive, not a gating gap'
    elif absent_last and not failed:
        out['summary']['note'] = ('the test file did not exist on the PR branch, so no pre-merge build could have run it: this is the '
                                  'SEMANTIC CONFLICT signature (early = the main commit that added the test, late = this PR), not a test leak - '
                                  f'find it with: gh api "repos/{REPO}/commits?path={test_file}&sha=main"')
    json.dump(out, open(a.out, 'w'), indent=1)

    lines = [f"# PR #{a.pr} — pre-merge CI history of `{a.test}`", '',
             f"merged_at {out['merged_at']} · merged head {merged_head} built: {out['merged_head_was_built']} · roll-up `{rollup}`", '',
             '| build | created (UTC) | head | relevant stages | verdict |', '|---|---|---|---|---|']
    for b in out['builds']:
        st = ', '.join(f"{s}={v['status']}" for s, v in b['relevant_stages'].items()) or '-'
        lines.append(f"| {b['build']} | {b['created']} | {b['head']} | {st} | {b['verdict']} |")
    lines += ['', '/bot commands:'] + [f"- {c['at']} {c['user']}: `{c['cmd']}`" for c in bot]
    lines += ['', f"**Summary:** test failed in {len(failed)} build(s) {failed}; last build {out['summary']['last_build']}: {out['summary']['last_build_verdict']}; test_leak={out['summary']['test_leak']}; test_file_absent_from_branch={absent_last}"]
    if out['summary'].get('note'):
        lines += ['', f"**Note:** {out['summary']['note']}"]
    md = '\n'.join(lines)
    if a.md_out:
        open(a.md_out, 'w').write(md)
    print(md)


if __name__ == '__main__':
    main()
