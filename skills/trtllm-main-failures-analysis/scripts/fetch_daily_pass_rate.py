#!/usr/bin/env python3
"""Pull the running execution history for one or more test-kind entities from
the stability dashboard's `/api/test-detail` endpoint (the same data backing
the test-history page and used by fetch_latest_status.py) and compute a
per-UTC-day pass rate, for questions like "what's this case's daily pass rate
over the last N days" that fetch_latest_status.py (last-3-runs-in-24h only)
and fetch_execution_details.py (FAILED executions only, no denominator) can't
answer on their own.

Each hit's `ts` is a raw epoch-millisecond timestamp (unambiguous UTC - unlike
the dashboard's ISO `...Z` window-bound fields elsewhere in this skill, which
are actually US-Pacific; see SKILL.md's "Time zones" ground rule). Calendar
days are bucketed in UTC.

SKIPPED runs (waived branch, deselected, "reused from previous pipeline")
carry no pass/fail signal and are excluded from the pass-rate denominator,
matching fetch_latest_status.py's convention, but are still counted and
reported per day for visibility.

Entities:
  --entity NAME                 repeatable; report every platform seen in the
                                 hits for NAME, plus an "ALL" combined row.
  --entity-platform NAME PLAT   repeatable; restrict NAME to one platform.
At least one of --entity/--entity-platform is required.

`NAME` is the dashboard test-detail entity name: a file path
(`unittest/_torch/attention`), a `file::Class::test` id, or a bare
`file.py::test` id - whatever `entity_name` fetch_failures.py / the
test-history page uses for that case. A `-k <expr>` pytest filter is not
representable in this API; pass the roll-up path and use --out plus a
downstream grep/jq if you need the filtered subset's rate specifically.

Output JSON: {"generated_from_hours": N, "results": [
  {"entity_name", "platform" ("ALL" or a gpu value),
   "daily": [{"date": "YYYY-MM-DD", "passed", "failed", "error", "skipped",
              "other", "total_executed", "pass_rate"}, ...],  # oldest first
   "overall": {"passed", "failed", "error", "skipped", "other",
               "total_executed", "pass_rate"}}, ...]}
`pass_rate` is null (not 0.0) for a day/entity with zero executed
(PASSED/FAILED/ERROR) runs, to distinguish "no data" from "100% failing".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

STABILITY_BASE = "http://trtllm-infra.nvidia.com/trtllm-stability-report"
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
EXECUTED_STATUSES = ("PASSED", "FAILED", "ERROR")
MAX_HOURS = 30 * 24  # dashboard test-history is capped at 30 days


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


def day_of(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def summarize(hits: list) -> dict:
    daily: dict[str, dict] = defaultdict(lambda: {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "other": 0})
    for h in hits:
        ts = h.get("ts")
        if not ts:
            continue
        d = daily[day_of(ts)]
        status = h.get("status")
        if status == "PASSED":
            d["passed"] += 1
        elif status == "FAILED":
            d["failed"] += 1
        elif status == "ERROR":
            d["error"] += 1
        elif status == "SKIPPED":
            d["skipped"] += 1
        else:
            d["other"] += 1

    def finalize(d: dict) -> dict:
        executed = d["passed"] + d["failed"] + d["error"]
        rate = (d["passed"] / executed) if executed else None
        return {**d, "total_executed": executed, "pass_rate": rate}

    daily_list = [{"date": date, **finalize(d)} for date, d in sorted(daily.items())]
    overall_counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "other": 0}
    for d in daily.values():
        for k in overall_counts:
            overall_counts[k] += d[k]
    overall = finalize(overall_counts)
    return {"daily": daily_list, "overall": overall}


def render_md(results: list) -> str:
    lines = ["| Entity | Platform | Overall pass rate | Days w/ data | Total executed |",
             "|---|---|---|---|---|"]
    for r in results:
        rate = r["overall"]["pass_rate"]
        rate_s = f"{rate * 100:.1f}%" if rate is not None else "n/a"
        lines.append(f"| {r['entity_name']} | {r['platform']} | {rate_s} | "
                      f"{len(r['daily'])} | {r['overall']['total_executed']} |")
    lines.append("")
    for r in results:
        lines.append(f"### {r['entity_name']} ({r['platform']})")
        lines.append("| Date | Passed | Failed | Error | Skipped | Pass rate |")
        lines.append("|---|---|---|---|---|---|")
        for d in r["daily"]:
            rate_s = f"{d['pass_rate'] * 100:.1f}%" if d["pass_rate"] is not None else "n/a"
            lines.append(f"| {d['date']} | {d['passed']} | {d['failed']} | {d['error']} | "
                          f"{d['skipped']} | {rate_s} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entity", action="append", default=[], help="repeatable; all platforms + an ALL row")
    ap.add_argument("--entity-platform", action="append", nargs=2, metavar=("NAME", "PLATFORM"),
                     default=[], help="repeatable; restrict NAME to one platform")
    ap.add_argument("--hours", type=int, default=MAX_HOURS,
                     help=f"lookback window in hours (default {MAX_HOURS} = 30 days, the dashboard's cap)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--out", required=True)
    ap.add_argument("--md-out")
    args = ap.parse_args()

    if not args.entity and not args.entity_platform:
        ap.error("give at least one of --entity / --entity-platform")
    if args.hours > MAX_HOURS:
        print(f"note: dashboard test-history is capped at 30 days; clamping --hours "
              f"{args.hours} -> {MAX_HOURS}", file=sys.stderr)
        args.hours = MAX_HOURS

    platform_restrict: dict[str, str] = {name: plat for name, plat in args.entity_platform}
    all_names = sorted(set(args.entity) | set(platform_restrict))

    hits_by_name: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_test_detail, name, args.hours): name for name in all_names}
        for fi, fut in enumerate(as_completed(futures), 1):
            name = futures[fut]
            try:
                hits_by_name[name] = fut.result()
                print(f"[{fi}/{len(all_names)}] {name}: {len(hits_by_name[name])} hit(s)", file=sys.stderr)
            except Exception as e:
                print(f"[{fi}/{len(all_names)}] warning: test-detail fetch failed for {name}: {e}", file=sys.stderr)
                hits_by_name[name] = []

    results = []
    for name in all_names:
        hits = hits_by_name.get(name, [])
        if name in platform_restrict:
            plat = platform_restrict[name]
            plat_hits = [h for h in hits if h.get("gpu") == plat]
            results.append({"entity_name": name, "platform": plat, **summarize(plat_hits)})
            continue
        platforms = sorted({h.get("gpu") for h in hits if h.get("gpu")})
        for plat in platforms:
            plat_hits = [h for h in hits if h.get("gpu") == plat]
            results.append({"entity_name": name, "platform": plat, **summarize(plat_hits)})
        results.append({"entity_name": name, "platform": "ALL", **summarize(hits)})

    out = {"generated_from_hours": args.hours, "results": results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {args.out}: {len(results)} entity/platform row(s)", file=sys.stderr)
    if args.md_out:
        Path(args.md_out).write_text(render_md(results))
        print(f"wrote {args.md_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
