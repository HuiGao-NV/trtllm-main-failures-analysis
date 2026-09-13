#!/usr/bin/env python3
"""For each test-kind entity in a condensed main-break JSON (fetch_failures.py
output), pull the last day's worth of executions from the stability
dashboard's `/api/test-detail` endpoint (the same "Executions" data backing
the test-history page) and determine whether the entity has since recovered:

  - Look at the most recent executions in the last 24h, newest first.
  - Take up to 3 of them (all of them if fewer than 3 runs happened in the
    last day).
  - If every one of those is PASSED, the entity is marked "Passed" (a
    continuous run of recent passes - any FAILED/SKIPPED/other status in
    that window breaks the streak).
  - Otherwise "Still Failing", with a note when 0 runs happened at all in
    the last day ("N/A (no runs in last 24h)").

It also flags a fresh regression from the same `recent` window: if the
single most recent run is FAILED but a more-recent-than-oldest-of-the-3 run
in that window was PASSED (i.e. it flipped from passing back to failing),
`regressed_since_pass` is True. This is the signal
build_confluence_cases.py uses to force the case log's "Analyzed" column
back to "False" for that case - a case that was reviewed and is now failing
again after passing needs a fresh look, regardless of whether someone had
already marked it analyzed.

This is a separate, smaller/faster pass than fetch_execution_details.py
(which pulls the full window's FAILED executions plus per-build ci_report
detail) - it only needs the last day, only needs status, and never touches
ci_report at all, so it's cheap enough to run for the full detected set
every time without the pacing/known-builds concerns that script has.

Like fetch_execution_details.py, entity_name is deduped for the fetch itself
(one /api/test-detail call per unique name, not per (name, platform) pair -
the endpoint returns all platforms' hits for a name in one response), then
filtered per (entity_name, platform) group afterwards - the same fix applied
there for the same reason: fetching/keying by name alone would silently
misattribute one platform's status to another for any entity_name that
recurs across platforms.

Output: JSON {"results": [{entity_name, platform, latest_status,
regressed_since_pass, recent_runs: [{build, status, ts}, ...]}, ...]}.
`recent_runs` is the up-to-3 executions the verdicts were based on, kept
for evidence/debugging.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

STABILITY_BASE = "http://trtllm-infra.nvidia.com/trtllm-stability-report"
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(url: str, timeout: int = 30, retries: int = 2):
    import time
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            e.close()
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise last_err


def fetch_test_detail(entity_name: str, hours: int) -> list:
    url = f"{STABILITY_BASE}/api/test-detail?name={quote(entity_name, safe='')}&hours={hours}"
    data = get_json(url)
    return data.get("hits", [])


def latest_status(hits: list) -> tuple[str, bool, list]:
    """hits: this entity's hits already filtered to the right platform, any
    status. Returns (verdict, regressed_since_pass, recent_runs_used)."""
    by_time = sorted(hits, key=lambda h: h.get("ts") or 0, reverse=True)
    recent = by_time[: min(3, len(by_time))]
    if not recent:
        return "N/A (no runs in last 24h)", False, []

    n = len(recent)
    passed = sum(1 for h in recent if h.get("status") == "PASSED")
    verdict = f"Passed ({n}/{n} recent runs)" if passed == n else f"Still Failing ({passed}/{n} recent passed)"

    # Flipped from passing back to failing: the single most recent run is
    # FAILED, but an earlier one in this same window was PASSED.
    regressed_since_pass = recent[0].get("status") == "FAILED" and any(h.get("status") == "PASSED" for h in recent[1:])

    recent_runs = [{"build": h.get("build"), "status": h.get("status"), "ts": h.get("ts")} for h in recent]
    return verdict, regressed_since_pass, recent_runs


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--groups-json", required=True, help="Condensed main-break JSON from fetch_failures.py")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window for 'recent' executions (default 24 = last day)")
    parser.add_argument("--workers", type=int, default=16, help="Thread pool size (this only hits the stability dashboard, not the fragile ci_report service)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    payload = json.loads(Path(args.groups_json).read_text())
    groups = payload.get("main_broken_groups", [])

    test_entities = [g for g in groups if g.get("entity_kind") == "test" and (g.get("links") or {}).get("test_history")]
    stage_entities = [g for g in groups if g not in test_entities]
    print(f"{len(test_entities)} test-kind entities with test-history, {len(stage_entities)} stage/other entities skipped", file=sys.stderr)

    unique_names = sorted({g["entity_name"] for g in test_entities})
    raw_hits_by_name: dict = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_test_detail, name, args.hours): name for name in unique_names}
        for fi, fut in enumerate(as_completed(futures), 1):
            name = futures[fut]
            try:
                raw_hits_by_name[name] = fut.result()
            except Exception as e:
                print(f"[{fi}/{len(unique_names)}] warning: test-detail fetch failed for {name}: {e}", file=sys.stderr)
                raw_hits_by_name[name] = []

    results = []
    for g in test_entities:
        name = g["entity_name"]
        platform = g.get("platform")
        hits = raw_hits_by_name.get(name, [])
        if platform:
            hits = [h for h in hits if h.get("gpu") == platform]
        verdict, regressed, recent_runs = latest_status(hits)
        results.append({
            "entity_name": name, "platform": platform, "latest_status": verdict,
            "regressed_since_pass": regressed, "recent_runs": recent_runs,
        })

    for g in stage_entities:
        results.append({
            "entity_name": g["entity_name"], "platform": g.get("platform"),
            "latest_status": "N/A (stage entity, no test-history)", "regressed_since_pass": False, "recent_runs": [],
        })

    Path(args.out).write_text(json.dumps({"generated_from": args.groups_json, "hours": args.hours, "results": results}, indent=2))
    passed_count = sum(1 for r in results if r["latest_status"].startswith("Passed"))
    regressed_count = sum(1 for r in results if r["regressed_since_pass"])
    print(f"Wrote latest status for {len(results)} entities to {args.out} ({passed_count} now passing, {regressed_count} freshly regressed)", file=sys.stderr)


if __name__ == "__main__":
    main()
