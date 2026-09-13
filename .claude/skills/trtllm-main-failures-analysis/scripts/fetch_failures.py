#!/usr/bin/env python3
"""Fetch and condense the "Detection Details" (Main Break) data from the
TRT-LLM CI stability report so it's small enough to reason about directly.

The dashboard's `Detection Details` section is populated from
GET {base_url}/api/incidents?days={days}, which returns a large payload
(tens of MB) containing many things besides Main Break data. This script
pulls out just `main_broken_groups`, strips the duplicated/bulky fields in
each observation, and writes a compact JSON file plus prints a summary table.

The backend only accepts `days` in VALID_DAYS below (matching the
dashboard's own day-selector buttons) - any other integer (e.g. 3, 4, 5, 6)
returns HTTP 400, confirmed by direct testing. This matters beyond just
manual `--days` typos: Step 0.5's watermark-driven incremental window
computes `days = ceil(hours_since_watermark / 24)`, which can land on any
integer - so this script snaps a requested value UP to the next VALID_DAYS
entry (e.g. 3 -> 7) rather than sending it straight through and 400ing.
"""
import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE_URL = "http://trtllm-infra.nvidia.com/trtllm-stability-report"
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
VALID_DAYS = [1, 2, 7, 30]


def snap_to_valid_days(days: int) -> int:
    for v in VALID_DAYS:
        if days <= v:
            return v
    return VALID_DAYS[-1]


def fetch_incidents(base_url: str, days: int) -> dict:
    snapped = snap_to_valid_days(days)
    if snapped != days:
        print(f"note: {days} isn't a supported window ({VALID_DAYS}); using {snapped} instead", file=sys.stderr)
    url = f"{base_url}/api/incidents?days={snapped}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def fetch_waive_info(base_url: str, names: list) -> dict:
    """Batch-fetch waive status for entity names via the same API the
    dashboard's Detection Details / test-history UI uses. Returns
    {name: {"is_waived": bool, "bugs": [...]}}."""
    url = f"{base_url}/api/test-waive-info"
    body = json.dumps({"names": names}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp).get("results", {})


def condense_observation(obs: dict) -> dict:
    return {
        "confidence": obs.get("confidence"),
        "confidence_reason": obs.get("confidence_reason"),
        "detection_mode": obs.get("detection_mode"),
        "failure_count": obs.get("failure_count"),
        "pass_count": obs.get("pass_count"),
        "pass_rate": obs.get("pass_rate"),
        "failure_pr_count": obs.get("failure_pr_count"),
        "failure_pr_identifiers": obs.get("failure_pr_identifiers"),
        "postmerge_failure_count": obs.get("postmerge_failure_count"),
        "platform": obs.get("platform"),
        "severity": obs.get("severity"),
        "window": obs.get("window"),
        "ts_detected": obs.get("ts_detected"),
        "latest_failure_time": obs.get("latest_failure_time"),
        "source_build_url": obs.get("source_build_url"),
        "jenkins_urls": obs.get("jenkins_urls"),
        "nvdf_document_url": obs.get("nvdf_document_url"),
    }


def condense_group(group: dict, waive_info: dict) -> dict:
    observations = group.get("observations", [])
    # Sort observations newest-first by detection time; keep the latest in
    # full and just count the rest, since repeated detections of the same
    # entity mostly duplicate the same failure signal.
    observations_sorted = sorted(
        observations, key=lambda o: o.get("ts_detected") or "", reverse=True
    )
    latest = condense_observation(observations_sorted[0]) if observations_sorted else {}
    info = waive_info.get(group.get("entity_name"))
    waived = info.get("is_waived") if info is not None else None
    waive_bugs = [
        {"id": b.get("id"), "url": b.get("url")} for b in (info or {}).get("bugs", [])
    ]
    return {
        "entity_kind": group.get("entity_kind"),
        "entity_name": group.get("entity_name"),
        "platform": group.get("platform"),
        "confidence": group.get("confidence"),
        "window_count": group.get("window_count", len(observations)),
        "earliest_window_start": group.get("earliest_window_start"),
        "latest_window_end": group.get("latest_window_end"),
        "has_comment": group.get("has_comment"),
        "latest_comment": group.get("latest_comment"),
        "links": group.get("links"),
        "waived": waived,
        "waive_bugs": waive_bugs,
        "latest_observation": latest,
    }


def sort_key(g: dict):
    conf_rank = CONFIDENCE_ORDER.get(g.get("confidence"), 99)
    failure_count = (g.get("latest_observation") or {}).get("failure_count") or 0
    return (conf_rank, -failure_count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Base URL of the stability report app")
    parser.add_argument("--out", default=None, help="Output JSON path (default: ./trtllm-failures-<days>d-<date>.json)")
    parser.add_argument("--input-json", default=None, help="Skip the network fetch and condense an already-downloaded /api/incidents response (for testing)")
    parser.add_argument("--include-stages", action="store_true", help="Include entity_kind='stage' groups (excluded by default - they have no test-history page, so fetch_execution_details.py/fetch_latest_status.py can't investigate them anyway, and they tend to be platform-wide infra noise rather than individual code breaks)")
    args = parser.parse_args()

    if args.input_json:
        with open(args.input_json) as f:
            payload = json.load(f)
    else:
        payload = fetch_incidents(args.base_url, args.days)

    groups = payload.get("main_broken_groups", [])
    if not args.include_stages:
        groups = [g for g in groups if g.get("entity_kind") != "stage"]

    names = sorted({g.get("entity_name") for g in groups if g.get("entity_name")})
    waive_info = {}
    if names:
        try:
            waive_info = fetch_waive_info(args.base_url, names)
        except Exception as e:
            print(f"warning: failed to fetch waive info ({e}); waived status will be null", file=sys.stderr)

    condensed = sorted((condense_group(g, waive_info) for g in groups), key=sort_key)

    out_path = args.out or f"./trtllm-failures-{args.days}d-{datetime.now(timezone.utc):%Y%m%d}.json"
    result = {
        "generated_at": payload.get("generated_at"),
        "window": payload.get("window"),
        "counts": payload.get("counts"),
        "main_broken_groups": condensed,
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Wrote {len(condensed)} main-break groups to {out_path}\n")
    print(f"{'CONF':<8}{'WAIVED':<8}{'PLATFORM':<14}{'FAIL/PASS':<11}{'PRs':<5}{'ENTITY':<0}")
    for g in condensed:
        obs = g.get("latest_observation") or {}
        fp = f"{obs.get('failure_count', '?')}/{obs.get('pass_count', '?')}"
        waived = g.get("waived")
        waived_str = "?" if waived is None else ("Y" if waived else "N")
        print(
            f"{g.get('confidence', '?'):<8}"
            f"{waived_str:<8}"
            f"{(g.get('platform') or '?'):<14}"
            f"{fp:<11}"
            f"{str(obs.get('failure_pr_count', '?')):<5}"
            f"{g.get('entity_name')}"
        )


if __name__ == "__main__":
    sys.exit(main())
