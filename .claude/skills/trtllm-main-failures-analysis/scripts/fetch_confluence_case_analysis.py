#!/usr/bin/env python3
"""Read the CI failure cases records Confluence page and extract, per case
name, the currently-recorded `Failure Type` (Step 4's "Analyze failure
reasons" conclusion) plus a signature of the error text that conclusion was
based on - so Step 4 can skip re-running Methods A/B/C for a case whose
current callstack/error message hasn't changed since it was last analyzed,
instead of redoing subagent-driven investigation on an unchanged failure
every run.

Handles both table layouts post_confluence_cases.py can produce:
  - flat mode: one row per case; signature is that row's `Stack Trace` cell
    (the aggregated top error message `build_confluence_cases.py` already
    writes there).
  - nested mode: one row per (case, PR, build); Failure Type is merged
    (rowspan) per case, so it's read once per case name. The signature is
    the `Failure Message` cell of that case's most-recent-`Date` row (the
    build closest to what a fresh Step 3 fetch would surface first).

A case with no `Failure Type` cell yet (never analyzed), or whose cell is
the "?" placeholder, is left OUT of the output entirely - there's nothing to
reuse for it, so Step 4 must analyze it fresh regardless of signature.

Auth: same credentials file as post_confluence_cases.py
(default ~/.config/confluence/credentials.json).

Output: JSON {"case_analysis": {"<case_name>": {"failure_type": "...",
"signature": "..."}, ...}}.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from confluence_config import CONFLUENCE_PAGE_ID  # noqa: E402
from post_confluence_cases import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_CREDS_PATH,
    auth_header,
    get_header_labels,
    get_page,
    load_credentials,
    parse_table_rows,
    reconstruct_grid,
)

PLACEHOLDER_VALUES = {"", "?", "n/a", "none"}


def extract_case_analysis(storage_html: str) -> dict:
    _, header_row, data_rows, _ = parse_table_rows(storage_html)
    if header_row is None:
        return {}

    headers = [h.strip().lower() for h in get_header_labels(header_row)]
    if "case name" not in headers or "failure type" not in headers:
        return {}

    name_idx = headers.index("case name")
    ft_idx = headers.index("failure type")
    grid = reconstruct_grid(header_row, data_rows)

    result: dict = {}

    if "stack trace" in headers:
        # Flat mode: one row per case, Stack Trace already holds the
        # aggregated top error message for that case.
        st_idx = headers.index("stack trace")
        for row in grid:
            name = row[name_idx].strip()
            failure_type = row[ft_idx].strip()
            signature = row[st_idx].strip()
            if not name or failure_type.lower() in PLACEHOLDER_VALUES:
                continue
            result[name] = {"failure_type": failure_type, "signature": signature}
        return result

    if "error / callstack" in headers and "triggered (utc)" in headers:
        # Nested per-build layout (post_confluence_cases.NESTED_HEADERS):
        # Failure Type is per build, so take it from the most recently
        # triggered build that has a real value; the signature is that
        # build's Error / callstack cell.
        err_idx = headers.index("error / callstack")
        trig_idx = headers.index("triggered (utc)")
        best: dict = {}
        for row in grid:
            name = row[name_idx].strip()
            failure_type = row[ft_idx].strip()
            if not name or failure_type.lower() in PLACEHOLDER_VALUES:
                continue
            trig = row[trig_idx].strip()
            if name not in best or trig > best[name]:
                best[name] = trig
                result[name] = {"failure_type": failure_type, "signature": row[err_idx].strip()}
        return result

    if "failure message" in headers and "date" in headers:
        # Nested mode: Failure Type is rowspan-merged per case, so every row
        # for a case repeats the same value once reconstructed - take it
        # from the first row seen. The signature is the Failure Message of
        # whichever row has the latest Date for that case.
        fm_idx = headers.index("failure message")
        date_idx = headers.index("date")
        best_date: dict = {}
        for row in grid:
            name = row[name_idx].strip()
            failure_type = row[ft_idx].strip()
            if not name or failure_type.lower() in PLACEHOLDER_VALUES:
                continue
            entry = result.setdefault(name, {"failure_type": failure_type, "signature": ""})
            row_date = row[date_idx].strip()
            if name not in best_date or row_date > best_date[name]:
                best_date[name] = row_date
                entry["signature"] = row[fm_idx].strip()
        return result

    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--page-id", default=CONFLUENCE_PAGE_ID, help="Confluence page ID (default: the CI failure cases records page, see confluence_config.py)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH))
    parser.add_argument("--out", required=True, help="Output JSON path: {\"case_analysis\": {...}}")
    args = parser.parse_args()

    email, api_token = load_credentials(Path(args.credentials))
    auth = auth_header(email, api_token)

    page = get_page(args.base_url, args.page_id, auth)
    storage_html = page["body"]["storage"]["value"]

    case_analysis = extract_case_analysis(storage_html)
    Path(args.out).write_text(json.dumps({"case_analysis": case_analysis}, indent=2))
    print(f"Found {len(case_analysis)} case(s) with a recorded Failure Type on the Confluence page; wrote {args.out}")


if __name__ == "__main__":
    main()
