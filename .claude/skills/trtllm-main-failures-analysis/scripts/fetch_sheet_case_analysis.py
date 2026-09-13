#!/usr/bin/env python3
"""Read the CI failure cases Google Sheet and extract, per case name, the
currently-recorded `Failure Type` (Step 4's "Analyze failure reasons"
conclusion) plus a signature of the error text that conclusion was based
on - the Sheets counterpart of fetch_confluence_case_analysis.py, same
reasoning (skip re-running Methods A/B/C for a case whose current
callstack/error message hasn't changed since it was last analyzed).

Only the flat shape post_google_sheet_cases.py writes exists for sheets (no
nested/rowspan equivalent in a plain grid): one row per case, with the
`Stack Trace` column holding the aggregated top error message
build_confluence_cases.py already writes there.

A case with no `Failure Type` cell yet (never analyzed), or whose cell is
the "?" placeholder, is left OUT of the output entirely.

Auth: same as post_google_sheet_cases.py - tries the Apps Script webhook,
then a service account JSON key, then the user's own gcloud credentials
re-scoped for Sheets, in that order.

Output: JSON {"case_analysis": {"<case_name>": {"failure_type": "...",
"signature": "..."}, ...}}.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from google_sheets_config import SPREADSHEET_ID  # noqa: E402
from post_google_sheet_cases import (  # noqa: E402
    DEFAULT_APPS_SCRIPT_CREDS_PATH,
    DEFAULT_CREDS_PATH,
    get_backend,
)

PLACEHOLDER_VALUES = {"", "?", "n/a", "none"}


def extract_case_analysis(values: list[list[str]]) -> dict:
    if not values:
        return {}

    header = [h.strip().lower() for h in values[0]]
    if "case name" not in header or "failure type" not in header:
        return {}

    name_idx = header.index("case name")
    ft_idx = header.index("failure type")
    st_idx = header.index("stack trace") if "stack trace" in header else None

    result: dict = {}
    for row in values[1:]:
        name = row[name_idx].strip() if name_idx < len(row) else ""
        failure_type = row[ft_idx].strip() if ft_idx < len(row) else ""
        if not name or failure_type.lower() in PLACEHOLDER_VALUES:
            continue
        signature = row[st_idx].strip() if st_idx is not None and st_idx < len(row) else ""
        result[name] = {"failure_type": failure_type, "signature": signature}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spreadsheet-id", default=SPREADSHEET_ID)
    parser.add_argument("--sheet-name", default=None, help="Tab name (default: the spreadsheet's first sheet)")
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH), help="Service account JSON key path")
    parser.add_argument("--apps-script-credentials", default=str(DEFAULT_APPS_SCRIPT_CREDS_PATH), help="Apps Script webhook config path")
    parser.add_argument("--out", required=True, help="Output JSON path: {\"case_analysis\": {...}}")
    args = parser.parse_args()

    backend = get_backend(Path(args.apps_script_credentials), Path(args.credentials))

    sheet_name = args.sheet_name or backend.get_first_sheet_title(args.spreadsheet_id)
    values = backend.get_values(args.spreadsheet_id, sheet_name)

    case_analysis = extract_case_analysis(values)
    Path(args.out).write_text(json.dumps({"case_analysis": case_analysis}, indent=2))
    print(f"Found {len(case_analysis)} case(s) with a recorded Failure Type on the Google Sheet; wrote {args.out}")


if __name__ == "__main__":
    main()
