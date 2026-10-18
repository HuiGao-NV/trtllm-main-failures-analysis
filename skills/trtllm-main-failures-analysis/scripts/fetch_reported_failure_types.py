#!/usr/bin/env python3
"""Count the dashboard's own per-run failure classification (`reason` /
`reason_long`) for test-kind entities over a window, for the Slack
"Reported failure type" field (SKILL.md Step 10 field rules).

Reuses fetch_daily_pass_rate.fetch_test_detail (same `/api/test-detail`
endpoint, same pacing). Only FAILED hits carry `reason_long`.

Usage:
  fetch_reported_failure_types.py --groups-json <fetch_failures.py output> --hours 168 --out <json>
  fetch_reported_failure_types.py --entity NAME [--entity NAME ...] --hours 168 --out <json>
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_daily_pass_rate import fetch_test_detail  # noqa: E402


def summarize(hits: list) -> dict:
    failed = [h for h in hits if (h.get("status") or "").upper() == "FAILED"]
    c = Counter((h.get("reason_long") or h.get("reason") or "?") for h in failed)
    total = len(failed)
    ranked = c.most_common()
    if not ranked:
        label = "n/a (no failing runs in window)"
    elif ranked[0][1] * 2 > total:
        label = f"{ranked[0][0]} ({ranked[0][1]}/{total} failing runs)"
    else:
        label = " / ".join(f"{k} ({n})" for k, n in ranked[:2]) + f" of {total} failing runs"
    return {"failed_hits": total, "reason_long": dict(c), "label": label}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups-json")
    ap.add_argument("--entity", action="append", default=[])
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    names = list(a.entity)
    if a.groups_json:
        d = json.load(open(a.groups_json))
        for g in d.get("main_broken_groups", []):
            if g.get("entity_kind") == "test" and g["entity_name"] not in names:
                names.append(g["entity_name"])
    results = []
    for name in names:
        try:
            hits = fetch_test_detail(name, a.hours)
        except Exception as e:  # noqa: BLE001
            print(f"warning: {name}: {e}", file=sys.stderr)
            hits = []
        plats = sorted({h.get("gpu") for h in hits if h.get("gpu")})
        for p in plats:
            results.append({"entity_name": name, "platform": p, **summarize([h for h in hits if h.get("gpu") == p])})
        results.append({"entity_name": name, "platform": "ALL", **summarize(hits)})
    Path(a.out).write_text(json.dumps({"hours": a.hours, "results": results}, indent=1))
    for r in results:
        print(f"{r['platform']:10} {r['label']:55} {r['entity_name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
