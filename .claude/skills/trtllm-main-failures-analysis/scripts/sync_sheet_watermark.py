#!/usr/bin/env python3
"""Read or write the ci-status "Latest update" watermark on the Google Sheet
case log - a standalone field tracking the last time this skill synced
against the dashboard's own `source.latest_ts_last_update` (see
fetch_ci_status_watermark.py), used to compute an incremental fetch window
instead of always re-fetching a fixed lookback.

Constraint that shapes where this lives: the Apps Script auth path
(apps_script/Code.gs) is hard-bound to one fixed tab (`SHEET_NAME = null` =
"the spreadsheet's first sheet"), so a separate "Meta" tab isn't reachable in
that auth mode - the watermark has to live on the SAME tab as the case grid.
It's stored as a label/value cell pair at a fixed column offset past the end
of DEFAULT_HEADERS (leaving one blank gap column), e.g. columns M/N when the
case table uses A-K - a location post_google_sheet_cases.py's sync_grid never
reads or writes (it only ever looks up specific header names like "Case
name"/"PR number"), so it's a genuinely standalone field, not a pseudo-row of
the case table.

IMPORTANT ordering: write the watermark AFTER publishing cases this run
(post_google_sheet_cases.py), not before. If the sheet has no case header row
yet at all, writing the watermark first would make the watermark's row look
like the sheet's only content, and a subsequent first-ever case publish would
misread it as the header row.

--read prints/writes {"latest_ts_last_update": "..."} or
{"latest_ts_last_update": null} if no watermark is recorded yet.
--write <timestamp> sets it, creating the label/value pair if absent.

Auth: same three paths as post_google_sheet_cases.py (Apps Script webhook,
service account, gcloud ADC).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from google_sheets_config import SPREADSHEET_ID  # noqa: E402
from post_confluence_cases import DEFAULT_HEADERS  # noqa: E402
from post_google_sheet_cases import (  # noqa: E402
    DEFAULT_APPS_SCRIPT_CREDS_PATH,
    DEFAULT_CREDS_PATH,
    get_backend,
)

WATERMARK_LABEL = "CI status watermark:"
# One blank gap column after the case table's own columns, then label, then value.
WATERMARK_LABEL_COL = len(DEFAULT_HEADERS) + 1
WATERMARK_VALUE_COL = WATERMARK_LABEL_COL + 1


def read_watermark(row0: list[str]) -> str | None:
    if len(row0) <= WATERMARK_LABEL_COL:
        return None
    if row0[WATERMARK_LABEL_COL].strip() != WATERMARK_LABEL:
        return None
    if len(row0) <= WATERMARK_VALUE_COL:
        return None
    value = row0[WATERMARK_VALUE_COL].strip()
    return value or None


def write_watermark(grid: list[list[str]], timestamp: str) -> list[list[str]]:
    grid = [list(r) for r in grid] if grid else [[]]
    row0 = grid[0]
    if len(row0) <= WATERMARK_VALUE_COL:
        row0 = row0 + [""] * (WATERMARK_VALUE_COL + 1 - len(row0))
    row0[WATERMARK_LABEL_COL] = WATERMARK_LABEL
    row0[WATERMARK_VALUE_COL] = timestamp
    grid[0] = row0
    return grid


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spreadsheet-id", default=SPREADSHEET_ID, help="Ignored in Apps Script mode - the deployed script is already bound to one spreadsheet")
    parser.add_argument("--sheet-name", default=None, help="Tab name (default: the spreadsheet's first sheet; ignored in Apps Script mode)")
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH), help="Service account JSON key path")
    parser.add_argument("--apps-script-credentials", default=str(DEFAULT_APPS_SCRIPT_CREDS_PATH), help="Apps Script webhook config path")
    parser.add_argument("--read", action="store_true", help="Read the currently recorded watermark")
    parser.add_argument("--write", metavar="TIMESTAMP", default=None, help="Write/replace the recorded watermark")
    parser.add_argument("--out", default=None, help="With --read, output JSON path (default: print to stdout only)")
    parser.add_argument("--dry-run", action="store_true", help="With --write, print the resulting row instead of writing it")
    args = parser.parse_args()

    if bool(args.read) == bool(args.write):
        sys.exit("Specify exactly one of --read or --write TIMESTAMP")

    backend = get_backend(Path(args.apps_script_credentials), Path(args.credentials))
    sheet_name = args.sheet_name or backend.get_first_sheet_title(args.spreadsheet_id)
    existing = backend.get_values(args.spreadsheet_id, sheet_name)

    if args.read:
        row0 = existing[0] if existing else []
        result = {"latest_ts_last_update": read_watermark(row0)}
        if args.out:
            Path(args.out).write_text(json.dumps(result, indent=2))
            print(f"Wrote {args.out}: {result}")
        else:
            print(json.dumps(result, indent=2))
        return

    new_grid = write_watermark(existing, args.write)
    if args.dry_run:
        print(f"Would set the sheet watermark to {args.write} on sheet '{sheet_name}' (via {backend.mode}).")
        print(new_grid[0])
        return

    backend.write_values(args.spreadsheet_id, sheet_name, new_grid)
    print(f"Published to https://docs.google.com/spreadsheets/d/{args.spreadsheet_id} (sheet '{sheet_name}', via {backend.mode}): watermark set to {args.write}.")


if __name__ == "__main__":
    main()
