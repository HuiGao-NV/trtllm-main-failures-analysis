#!/usr/bin/env python3
"""Read the CI failure cases Google Sheet and extract every ci_report build
id already recorded on it, so a later fetch_execution_details.py run can
skip re-fetching job data for builds already processed and published there -
the Sheets counterpart of fetch_confluence_known_builds.py, same reasoning
(a build's test result is immutable once posted).

Only the flat shape post_google_sheet_cases.py writes exists for sheets (no
nested/rowspan equivalent in a plain grid): a "Build-PR Mapping" column with
cell values like "58051:PR18571, 57948:PR18270, ...".

Auth: same as post_google_sheet_cases.py - tries the Apps Script webhook,
then a service account JSON key, then the user's own gcloud credentials
re-scoped for Sheets, in that order. See that script's docstring for
one-time setup of any of the three paths.

Output: JSON {"known_builds": ["57785", "57786", ...]} (sorted, deduped).
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Reuse the Sheets auth/read helpers rather than duplicating them - both
# scripts live in the same directory, so this works when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from google_sheets_config import SPREADSHEET_ID  # noqa: E402
from post_google_sheet_cases import (  # noqa: E402
    DEFAULT_APPS_SCRIPT_CREDS_PATH,
    DEFAULT_CREDS_PATH,
    get_backend,
)

BUILD_PR_PAIR_RE = re.compile(r"(\d+):PR")


def extract_known_builds(values: list[list[str]]) -> set[str]:
    if not values:
        return set()

    header = [h.strip().lower() for h in values[0]]
    if "build-pr mapping" not in header:
        return set()
    idx = header.index("build-pr mapping")

    known: set[str] = set()
    for row in values[1:]:
        if idx < len(row):
            known.update(BUILD_PR_PAIR_RE.findall(row[idx]))
    return known


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spreadsheet-id", default=SPREADSHEET_ID)
    parser.add_argument("--sheet-name", default=None, help="Tab name (default: the spreadsheet's first sheet)")
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH), help="Service account JSON key path")
    parser.add_argument("--apps-script-credentials", default=str(DEFAULT_APPS_SCRIPT_CREDS_PATH), help="Apps Script webhook config path")
    parser.add_argument("--out", required=True, help="Output JSON path: {\"known_builds\": [...]}")
    args = parser.parse_args()

    backend = get_backend(Path(args.apps_script_credentials), Path(args.credentials))

    sheet_name = args.sheet_name or backend.get_first_sheet_title(args.spreadsheet_id)
    values = backend.get_values(args.spreadsheet_id, sheet_name)

    known_builds = sorted(extract_known_builds(values), key=lambda x: int(x) if x.isdigit() else 0)
    Path(args.out).write_text(json.dumps({"known_builds": known_builds}, indent=2))
    print(f"Found {len(known_builds)} known build id(s) already on the Google Sheet; wrote {args.out}")


if __name__ == "__main__":
    main()
