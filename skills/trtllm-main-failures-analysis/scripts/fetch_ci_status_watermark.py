#!/usr/bin/env python3
"""Read the trtllm-infra stability dashboard's own "Latest update" timestamp -
the same value the ci-status page footer shows ("Latest update ..."), sourced
from `data.source.latest_ts_last_update` in the `/api/incidents` response.

This value does not depend on the requested window size, so this script
always uses the cheapest valid call (`days=1`) rather than a full days=7 (or
larger) fetch just to read one timestamp. It's the "current" watermark: read
once near the start of a run and, after a successful publish (see
sync_confluence_watermark.py / sync_sheet_watermark.py), written back to
whichever publish target(s) the case log was actually updated on.
"""
import argparse
import json
import sys
import urllib.request

DEFAULT_BASE_URL = "http://trtllm-infra.nvidia.com/trtllm-stability-report"


def fetch_source(base_url: str) -> dict:
    url = f"{base_url}/api/incidents?days=1"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    return payload.get("source") or {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Base URL of the stability report app")
    parser.add_argument("--out", default=None, help="Output JSON path (default: print to stdout only)")
    args = parser.parse_args()

    source = fetch_source(args.base_url)
    result = {
        "latest_ts_last_update": source.get("latest_ts_last_update"),
        "latest_ts_detected": source.get("latest_ts_detected"),
    }

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Wrote {args.out}: {result}")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
