#!/usr/bin/env python3
"""Read the CI failure cases records Confluence page and extract every ci_report
build id already recorded on it, so a later fetch_execution_details.py run can
skip re-fetching job data for builds we've already processed and published
(a build's test result is immutable once posted - there's nothing to gain by
re-fetching it, and it's extra load on the shared ci_report service for no
benefit).

Handles both table layouts post_confluence_cases.py can produce:
  - nested mode: a "Build ID" column, one build per row
  - flat mode: a "Build-PR Mapping" column, cell values like
    "58051:PR18571, 57948:PR18270, ..." - build ids are parsed out of these

Auth: same credentials file as post_confluence_cases.py
(default ~/.config/confluence/credentials.json).

Output: JSON {"known_builds": ["57785", "57786", ...]} (sorted, deduped).
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Reuse the Confluence read/parse helpers rather than duplicating them -
# both scripts live in the same directory, so this works when run directly.
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

BUILD_PR_PAIR_RE = re.compile(r"(\d+):PR")


def extract_known_builds(storage_html: str) -> set[str]:
    _, header_row, data_rows, _ = parse_table_rows(storage_html)
    if header_row is None:
        return set()

    headers = [h.strip().lower() for h in get_header_labels(header_row)]
    known: set[str] = set()

    if "build id" not in headers and "build" not in headers and "build-pr mapping" not in headers:
        return known

    grid = reconstruct_grid(header_row, data_rows)

    if "build id" in headers or "build" in headers:
        idx = headers.index("build id") if "build id" in headers else headers.index("build")
        for row in grid:
            text = row[idx].strip()
            if text and text.lower() not in ("n/a", "?", "none"):
                known.add(text)

    if "build-pr mapping" in headers:
        idx = headers.index("build-pr mapping")
        for row in grid:
            known.update(BUILD_PR_PAIR_RE.findall(row[idx]))

    return known


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--page-id", default=CONFLUENCE_PAGE_ID, help="Confluence page ID (default: the CI failure cases records page, see confluence_config.py)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH))
    parser.add_argument("--out", required=True, help="Output JSON path: {\"known_builds\": [...]}")
    args = parser.parse_args()

    email, api_token = load_credentials(Path(args.credentials))
    auth = auth_header(email, api_token)

    page = get_page(args.base_url, args.page_id, auth)
    storage_html = page["body"]["storage"]["value"]

    known_builds = sorted(extract_known_builds(storage_html), key=lambda x: int(x) if x.isdigit() else 0)
    Path(args.out).write_text(json.dumps({"known_builds": known_builds}, indent=2))
    print(f"Found {len(known_builds)} known build id(s) already on the Confluence page; wrote {args.out}")


if __name__ == "__main__":
    main()
