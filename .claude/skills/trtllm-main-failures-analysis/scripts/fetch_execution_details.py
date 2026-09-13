#!/usr/bin/env python3
"""For each test-kind entity in a condensed main-break JSON (fetch_failures.py
output), walk every FAILED execution recorded in its test-history window and
pull per-execution waive status / bug link / short error message from the
ci_report site's job API (the same data ci_report's UI shows under a failed
test's "failure section").

Pipeline per entity:
  1. GET {stability_base}/api/test-detail?name=<entity_name>&hours=<hours>
     -> list of executions (job, build, stage, status, ts, mr)
  2. Keep only status == FAILED.
  3. For each unique (job, build) across ALL entities (deduped globally,
     since many test cases fail together in the same CI build), fetch
     GET {ci_report_base}/api/job/<job>/<build> once and cache it.
  4. Within that job's tests_by_stage[stage], find the test record whose
     s_turtle_name matches the entity_name (exact match) and read
     is_waived / waive_reason / waive_bug_url / s_short_error_msg.
  5. Within that same job's categorized_stages.data, find the stage entry
     matching `stage` (by s_stage_name) and read its "Log" link - the same
     direct Jenkins Blue Ocean raw-log URL ci_report's own UI shows/links to
     for that stage (see find_stage_log_link()).

Stage-kind entities have no test-history page (their `links.test_history` is
null in the condensed data) and are skipped with a note.

Output: JSON list of {entity_name, platform, build_pr_map, executions: [...]}
per entity. `build_pr_map` is the deduped {build_id: pr_id} relationship
derived from each execution's build (the id in its ci_report link) and `mr`
(the PR that build ran) - the explicit build<->source-PR mapping, not just
implied by the two separate per-execution fields.

If --known-builds-json is given (see fetch_confluence_known_builds.py and/or
fetch_google_sheet_known_builds.py - pass the flag multiple times to union
more than one source, e.g. builds already on both Confluence and a Google
Sheet), any build id already in that set is skipped entirely: not fetched
from ci_report in step 3, and not included in the output `executions` list.
A build's test result is immutable once posted, so a build already recorded on the
Confluence case log has nothing new to learn and is just extra load on a
shared service to re-fetch. This makes the output an *incremental* set: only
executions for builds not already published.
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

STABILITY_BASE = "http://trtllm-infra.nvidia.com/trtllm-stability-report"
CI_REPORT_BASE = "http://tensorrt-llm.tensorrt-llm-ci-report.sc2-paas.nvidia.com"

# This host reaches trtllm-infra/ci-report directly over NVIDIA's internal
# network; routing many concurrent requests through the environment's local
# HTTP proxy (http_proxy=127.0.0.1:...) causes severe contention/stalling
# under this script's parallel fetches, so bypass it explicitly.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RateLimiter:
    """Enforces a minimum wall-clock gap between request *starts* across all
    threads, so N workers can't all fire at once - this is a shared internal
    service, not something we should hammer with a concurrency burst even
    with a small thread pool."""

    def __init__(self, min_interval_s: float):
        self.min_interval = min_interval_s
        self.lock = threading.Lock()
        self.next_ok_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            start_at = max(now, self.next_ok_at)
            self.next_ok_at = start_at + self.min_interval
        sleep_for = start_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)


def get_json(url: str, timeout: int = 30, retries: int = 2):
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # HTTPError is itself an open file-like response (e.g. the body of
            # a 503) - if it isn't closed, the underlying socket leaks in
            # CLOSE_WAIT, and enough of those exhaust file descriptors and
            # stall the whole pool.
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


def ci_report_url(job: str, build: str) -> str:
    return f"{CI_REPORT_BASE}/?job={quote(job, safe='')}&build={quote(build, safe='')}"


def fetch_job_data_uncached(job: str, build: str, limiter: "RateLimiter") -> dict | None:
    limiter.wait()
    url = f"{CI_REPORT_BASE}/api/job/{quote(job, safe='')}/{quote(build, safe='')}"
    try:
        data = get_json(url)
    except Exception as e:
        print(f"  warning: failed to fetch job data for {job}#{build}: {e}", file=sys.stderr)
        return None
    if not data.get("success"):
        return None
    return data.get("data", {})


def find_test_record(job_data: dict, stage: str, entity_name: str) -> dict | None:
    stage_tests = (job_data.get("tests_by_stage") or {}).get(stage) or []
    for t in stage_tests:
        if t.get("s_turtle_name") == entity_name:
            return t
    return None


def find_stage_log_link(job_data: dict, stage: str) -> str | None:
    """The direct Jenkins Blue Ocean REST API log URL for a stage - this is
    the same URL ci_report's own UI uses for that stage's "Log" link/button,
    found by searching categorized_stages.data (grouped by top-level Jenkins
    sub-pipeline, e.g. "L0_Test-x86_64-Single-GPU" - NOT keyed by the
    fine-grained stage name like "DGX_B200-PyTorch-9" itself) for the entry
    whose s_stage_name matches. Prefers s_blue_ocean_log_link (a direct
    "fetch this node's raw log" URL); falls back to the first nested
    attempt's s_log_link (same shape, used for retried stages), then to
    s_blue_ocean_link (the human-facing Blue Ocean page, not a raw-log API)
    if neither log-specific field is present."""
    categories = ((job_data.get("categorized_stages") or {}).get("data")) or {}
    for items in categories.values():
        for item in items:
            if item.get("s_stage_name") != stage:
                continue
            if item.get("s_blue_ocean_log_link"):
                return item["s_blue_ocean_log_link"]
            for attempt in item.get("nested_attempts") or []:
                if attempt.get("s_log_link"):
                    return attempt["s_log_link"]
            return item.get("s_blue_ocean_link")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--groups-json", required=True, help="Condensed main-break JSON from fetch_failures.py")
    parser.add_argument("--hours", type=int, default=168, help="Test-history lookback window (default 168 = 7 days, should match the --days used for fetch_failures.py)")
    parser.add_argument("--out", default=None, help="Output JSON path")
    parser.add_argument("--max-executions-per-entity", type=int, default=None, help="Cap on FAILED executions processed per entity (default: no cap, all of them)")
    parser.add_argument("--workers", type=int, default=16, help="Thread pool size for the test-detail phase (a different, less fragile service)")
    parser.add_argument("--job-workers", type=int, default=3, help="Thread pool size for the ci_report job-data phase (kept low - this is a shared service observed to 503 under bursty load)")
    parser.add_argument("--job-min-interval", type=float, default=0.5, help="Minimum seconds between ci_report job-data request starts, enforced across all job-workers")
    parser.add_argument(
        "--known-builds-json", action="append", default=[],
        help="Path to {\"known_builds\": [...]} (from fetch_confluence_known_builds.py and/or "
             "fetch_google_sheet_known_builds.py) - build ids already recorded there are skipped "
             "entirely: not fetched, not included in output. Repeatable to union multiple sources.",
    )
    args = parser.parse_args()

    known_builds: set[str] = set()
    for path in args.known_builds_json:
        source_builds = set(json.loads(Path(path).read_text()).get("known_builds", []))
        known_builds |= source_builds
        print(f"Loaded {len(source_builds)} known build id(s) from {path}", file=sys.stderr)
    if args.known_builds_json:
        print(f"{len(known_builds)} known build id(s) total to skip", file=sys.stderr)

    payload = json.loads(Path(args.groups_json).read_text())
    groups = payload.get("main_broken_groups", [])

    test_entities = [g for g in groups if g.get("entity_kind") == "test" and (g.get("links") or {}).get("test_history")]
    stage_entities = [g for g in groups if g not in test_entities]
    print(f"{len(test_entities)} test-kind entities with test-history, {len(stage_entities)} stage/other entities skipped", file=sys.stderr)

    # Phase 1: fetch test-detail (all hits, every platform) for every UNIQUE
    # entity_name, in parallel. /api/test-detail?name=<entity_name> returns
    # hits for every platform that test ran on - there's nothing
    # platform-specific about the request - so entity_name (not (entity_name,
    # platform)) is deduped here to avoid redundant fetches when the same
    # test name appears as multiple main_broken_groups on different
    # platforms (e.g. one entry per platform for a cross-platform test).
    # Per-platform filtering happens after, once per (entity_name, platform)
    # group, from this shared raw result.
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

    # Filter to FAILED + this specific group's platform, once per
    # (entity_name, platform) - keyed by id() of the group dict itself
    # since two groups can share (entity_name, platform) is never expected
    # but plain identity keeps this unambiguous either way.
    entity_hits: dict = {}
    for fi, g in enumerate(test_entities, 1):
        name = g["entity_name"]
        platform = g.get("platform")
        hits = raw_hits_by_name.get(name, [])
        failed_hits = [h for h in hits if h.get("status") == "FAILED"]
        if platform:
            failed_hits = [h for h in failed_hits if h.get("gpu") == platform]
        if args.max_executions_per_entity:
            failed_hits = failed_hits[: args.max_executions_per_entity]
        entity_hits[id(g)] = failed_hits
        print(f"[{fi}/{len(test_entities)}] {name} ({platform}): {len(failed_hits)} failed executions", file=sys.stderr)

    # Phase 2: dedupe (job, build) across ALL entities, fetch each once. This
    # hits the ci_report site's job API, a shared internal service that has
    # been observed to return widespread 503s under bursty concurrent load,
    # so it's deliberately paced (a handful of workers plus a minimum gap
    # between request starts, not just a small thread count) rather than
    # fired at full pool concurrency like Phase 1.
    all_unique_builds = {(h["job"], h["build"]) for hits in entity_hits.values() for h in hits if h.get("job") and h.get("build")}
    unique_builds = sorted(b for b in all_unique_builds if b[1] not in known_builds)
    skipped_known_count = len(all_unique_builds) - len(unique_builds)
    print(
        f"\n{len(unique_builds)} unique (job, build) pairs to fetch "
        f"({skipped_known_count} skipped, already known/published) "
        f"(paced: {args.job_workers} workers, {args.job_min_interval}s min gap)",
        file=sys.stderr,
    )
    job_cache: dict = {}
    lock = threading.Lock()
    limiter = RateLimiter(args.job_min_interval)
    with ThreadPoolExecutor(max_workers=args.job_workers) as pool:
        futures = {pool.submit(fetch_job_data_uncached, job, build, limiter): (job, build) for job, build in unique_builds}
        for fi, fut in enumerate(as_completed(futures), 1):
            job, build = futures[fut]
            try:
                data = fut.result()
            except Exception as e:
                print(f"  warning: job fetch failed for {job}#{build}: {e}", file=sys.stderr)
                data = None
            with lock:
                job_cache[(job, build)] = data
            if fi % 25 == 0 or fi == len(unique_builds):
                print(f"  fetched {fi}/{len(unique_builds)} builds", file=sys.stderr)

    # Phase 3: assemble per-entity execution records from the cache. Hits on
    # a known (already-published) build are skipped entirely here too - they
    # were never fetched in Phase 2, so there's no fresh data for them, and
    # they're already represented wherever the known-builds source(s) came from.
    total_skipped_known = 0
    results = []
    for g in test_entities:
        name = g["entity_name"]
        platform = g.get("platform")
        failed_hits = entity_hits.get(id(g), [])
        executions = []
        skipped_known = 0
        for h in failed_hits:
            job, build, stage = h.get("job"), h.get("build"), h.get("stage")
            if not (job and build and stage):
                continue
            if build in known_builds:
                skipped_known += 1
                total_skipped_known += 1
                continue
            job_data = job_cache.get((job, build))
            record = find_test_record(job_data, stage, name) if job_data else None
            executions.append({
                "job": job,
                "build": build,
                "stage": stage,
                "ts": h.get("ts"),
                "mr": h.get("mr"),
                "ci_report_url": ci_report_url(job, build),
                "is_waived": (record or {}).get("is_waived"),
                "waive_reason": (record or {}).get("waive_reason") or None,
                "waive_bug_url": (record or {}).get("waive_bug_url") or None,
                "short_error_msg": (record or {}).get("s_short_error_msg") or None,
                "log_link": find_stage_log_link(job_data, stage) if job_data else None,
                "found_in_job_data": record is not None,
            })
        # Explicit build_id -> source PR mapping, deduped across executions
        # (the ci_report link for a given execution is keyed by build_id;
        # `mr` on the underlying test-detail hit is the PR that build ran).
        build_pr_map = {}
        for e in executions:
            if e["build"] and e["mr"] and e["build"] not in build_pr_map:
                build_pr_map[e["build"]] = e["mr"]

        results.append({
            "entity_name": name,
            "platform": platform,
            "total_failed_hits": len(failed_hits),
            "skipped_known_builds": skipped_known,
            "build_pr_map": build_pr_map,
            "executions": executions,
        })

    for g in stage_entities:
        results.append({"entity_name": g["entity_name"], "platform": g.get("platform"), "skipped": "stage-kind entity, no test-history page", "executions": []})

    out_path = args.out or "./trtllm-execution-details.json"
    with open(out_path, "w") as f:
        json.dump({"generated_from": args.groups_json, "hours": args.hours, "results": results}, f, indent=2)

    print(f"\nWrote execution details for {len(results)} entities to {out_path}", file=sys.stderr)
    print(f"Unique (job, build) pairs fetched: {len(job_cache)}", file=sys.stderr)
    if known_builds:
        print(f"Executions skipped as already-known/published: {total_skipped_known}", file=sys.stderr)


if __name__ == "__main__":
    main()
