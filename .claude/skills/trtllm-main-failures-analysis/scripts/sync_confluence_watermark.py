#!/usr/bin/env python3
"""Read or write the ci-status "Latest update" watermark on the Confluence
"CI failure cases records" page - a standalone field tracking the last time
this skill synced against the dashboard's own `source.latest_ts_last_update`
(see fetch_ci_status_watermark.py), used to compute an incremental fetch
window instead of always re-fetching a fixed lookback.

Stored as a single paragraph OUTSIDE the case table (in the page HTML that
parse_table_rows already treats as everything before the table), so it's
independent of the table schema/migration logic and of any Confluence editing
of the table rows themselves:

  <p><strong>CI status watermark (latest ci-status "Latest update" synced
  through):</strong> 2026-09-01T12:00:00.000000Z</p>

--read prints/writes {"latest_ts_last_update": "..."} or
{"latest_ts_last_update": null} if no such paragraph exists yet (e.g. first
run). --write <timestamp> replaces the existing paragraph if present, else
inserts a new one at the very top of the page body.

Auth: same credentials file as post_confluence_cases.py
(default ~/.config/confluence/credentials.json).
"""
import argparse
import json
import re
import sys
from html import escape, unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from confluence_config import CONFLUENCE_PAGE_ID  # noqa: E402
from post_confluence_cases import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_CREDS_PATH,
    auth_header,
    get_page,
    load_credentials,
    parse_table_rows,
    update_page,
)

LABEL = 'CI status watermark (latest ci-status "Latest update" synced through):'
# The paragraph is written with escape(LABEL) (see write_watermark), so a
# literal `"` in LABEL is stored as `&quot;` in the page HTML - match against
# the same escaped form, not the raw label, or a fresh read never finds what
# a previous write actually wrote.
PARAGRAPH_RE = re.compile(
    r"<p><strong>" + re.escape(escape(LABEL)) + r"</strong>\s*([^<]*?)\s*</p>", re.S
)


def read_watermark(storage_html: str) -> str | None:
    before, _, _, _ = parse_table_rows(storage_html)
    # If there's no table at all, the whole page body is "before".
    haystack = before if before else storage_html
    m = PARAGRAPH_RE.search(haystack)
    if not m:
        return None
    value = unescape(m.group(1)).strip()
    return value or None


def write_watermark(storage_html: str, timestamp: str) -> str:
    paragraph = f"<p><strong>{escape(LABEL)}</strong> {escape(timestamp)}</p>"
    before, header_row, data_rows, after = parse_table_rows(storage_html)
    haystack = before if header_row is not None else storage_html
    if PARAGRAPH_RE.search(haystack):
        new_haystack = PARAGRAPH_RE.sub(paragraph, haystack, count=1)
    else:
        new_haystack = paragraph + haystack

    if header_row is None:
        return new_haystack
    return new_haystack + header_row + "".join(data_rows) + after


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--page-id", default=CONFLUENCE_PAGE_ID, help="Confluence page ID (default: the CI failure cases records page, see confluence_config.py)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH))
    parser.add_argument("--read", action="store_true", help="Read the currently recorded watermark")
    parser.add_argument("--write", metavar="TIMESTAMP", default=None, help="Write/replace the recorded watermark")
    parser.add_argument("--out", default=None, help="With --read, output JSON path (default: print to stdout only)")
    parser.add_argument("--dry-run", action="store_true", help="With --write, print the resulting page body instead of publishing")
    args = parser.parse_args()

    if bool(args.read) == bool(args.write):
        sys.exit("Specify exactly one of --read or --write TIMESTAMP")

    email, api_token = load_credentials(Path(args.credentials))
    auth = auth_header(email, api_token)

    page = get_page(args.base_url, args.page_id, auth)
    storage_html = page["body"]["storage"]["value"]

    if args.read:
        result = {"latest_ts_last_update": read_watermark(storage_html)}
        if args.out:
            Path(args.out).write_text(json.dumps(result, indent=2))
            print(f"Wrote {args.out}: {result}")
        else:
            print(json.dumps(result, indent=2))
        return

    new_html = write_watermark(storage_html, args.write)
    if args.dry_run:
        print(f"Would set the Confluence watermark to {args.write}.")
        print(new_html)
        return

    title = page["title"]
    current_version = page["version"]["number"]
    update_page(args.base_url, args.page_id, auth, title, current_version + 1, new_html)
    print(f"Published to {args.base_url}/pages/{args.page_id}: watermark set to {args.write}.")


if __name__ == "__main__":
    main()
