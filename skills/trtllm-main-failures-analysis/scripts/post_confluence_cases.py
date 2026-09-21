#!/usr/bin/env python3
"""Publish analyzed CI-failure cases to the Confluence "CI failure cases records" page.

Reads a JSON list of cases (one per root-cause cluster) and syncs them into a table
on the target Confluence page:
  - a case not already on the page (matched by case name + PR number) becomes a new row
  - a case already on the page gets the new failure_analysis/date appended to its
    existing row (not overwritten, so history accumulates across runs) and its
    Waived cell overwritten with the latest status (a point-in-time flag, not history)

The table schema is driven by the page's own header row, not a hardcoded layout:
any column in DEFAULT_HEADERS missing from the page's existing header (e.g. it
predates this script adding one) is inserted for the header and every existing
data row (with "?" placeholders) before merging in the new cases. This keeps
older rows on the page intact while adding new columns everywhere.

Auth: reads {"email": "...", "api_token": "..."} from a local credentials file
(default ~/.config/confluence/credentials.json) and calls the Confluence Cloud
REST API directly with HTTP Basic auth (email + API token). Generate a token at
https://id.atlassian.com/manage-profile/security/api-tokens if you don't have one.

Input case JSON format (list of objects):
  [
    {"case_name": "Gemma-3 LoRA regression", "pr_number": "18473",
     "failure_analysis": "...", "waived": "Y (nvbugs/6705034)",
     "waived_executions": "3/3 executions waived", "related_bugs": "https://nvbugs/...",
     "stack_trace": "...", "build_pr_mapping": "58051:18571, 57948:18270, ...",
     "date": "2026-09-02"},
    ...
  ]
`waived` should be a short display string, e.g. "Y", "N", "Y (nvbugs/<id>)",
"N (tracked: nvbugs/<id>)", or "?" if unknown. `waived_executions`,
`related_bugs`, `stack_trace`, and `build_pr_mapping` are all optional: a
brand-new row falls back to "?" for a missing one, but on an *update* to an
existing row, a missing field leaves that cell exactly as it already was on
the page (see build_confluence_cases.py, which omits these fields for a
case with no fresh execution data - e.g. every build already known/skipped -
specifically so a re-publish doesn't clobber previously-recorded detail with
placeholders).
"""
import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.request
from html import escape, unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from confluence_config import CONFLUENCE_BASE_URL, CONFLUENCE_PAGE_ID  # noqa: E402

DEFAULT_CREDS_PATH = Path.home() / ".config" / "confluence" / "credentials.json"
DEFAULT_BASE_URL = CONFLUENCE_BASE_URL
DEFAULT_HEADERS = [
    "Case name", "PR number", "Failure analysis", "Waived",
    "Waived (Executions)", "Related Bugs", "Stack Trace", "Failure Type",
    "Build-PR Mapping", "Latest Status", "Analyzed", "Date",
]
CASE_FIELD_BY_HEADER = {
    "case name": "case_name",
    "pr number": "pr_number",
    "failure analysis": "failure_analysis",
    "waived": "waived",
    "waived (executions)": "waived_executions",
    "related bugs": "related_bugs",
    "stack trace": "stack_trace",
    "failure type": "failure_type",
    "build-pr mapping": "build_pr_mapping",
    "latest status": "latest_status",
    "analyzed": "analyzed",
    "date": "date",
}
# Point-in-time status fields: overwritten with the latest value on update.
# Everything else not in APPEND_HEADERS keeps its original value once set
# (case name / PR number, matched on and never rewritten).
APPEND_HEADERS = {"failure analysis", "date"}
OVERWRITE_HEADERS = {"waived", "waived (executions)", "related bugs", "stack trace", "failure type", "build-pr mapping", "latest status", "analyzed"}
# "Failure Type" holds the Step 4 (Analyze failure reasons) conclusion -
# Infra failure / Regression / Flaky test / etc, per build_confluence_cases.py's
# --failure-types-json. Like Waived/Latest Status, it's a point-in-time
# conclusion (re-derived, not a log) - a case build_confluence_cases.py has
# no fresh analysis for leaves this field out of the case dict entirely
# (the normal OVERWRITE_HEADERS "absent = don't touch" convention), not set
# to "?", so a re-publish never clobbers a previously-recorded conclusion
# with a placeholder just because this run didn't re-analyze that case.
# "Analyzed" is different from the other OVERWRITE_HEADERS: it's a sticky,
# often-human-edited flag ("has someone reviewed this case"), not something
# every run should refresh. build_confluence_cases.py only ever includes it
# in a case dict to force a reset to "False" (a fresh pass->fail regression
# needs re-review) - never to set it True, and never on a case with no
# regression signal. A brand-new row (never published before) defaults to
# "False" here; an existing row with the field absent from the incoming
# case data keeps whatever is already on the page (flat mode, via
# append_to_row/value_for_header's normal "absent = don't touch" behavior).
# Nested mode is different again since it always fully replaces the table -
# see the nested publish path in main() for how existing Analyzed values
# are read back and carried forward before the rebuild.
ANALYZED_DEFAULT = "False"


def load_credentials(path: Path) -> tuple[str, str]:
    if not path.exists():
        sys.exit(
            f"Credentials file not found: {path}\n"
            'Create it with: {"email": "you@nvidia.com", "api_token": "<atlassian API token>"}\n'
            "Generate a token at https://id.atlassian.com/manage-profile/security/api-tokens"
        )
    data = json.loads(path.read_text())
    return data["email"], data["api_token"]


def auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def api_request(base_url: str, path: str, auth: str, method: str = "GET", body: dict | None = None) -> dict:
    url = f"{base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": auth,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Confluence API error {e.code} on {method} {path}: {e.read().decode()[:2000]}")


def get_page(base_url: str, page_id: str, auth: str) -> dict:
    return api_request(base_url, f"/api/v2/pages/{page_id}?body-format=storage", auth)


def update_page(base_url: str, page_id: str, auth: str, title: str, new_version: int, storage_html: str) -> dict:
    body = {
        "id": page_id,
        "status": "current",
        "title": title,
        "body": {"representation": "storage", "value": storage_html},
        "version": {"number": new_version, "message": "trtllm-main-failures-analysis: sync CI failure cases"},
    }
    return api_request(base_url, f"/api/v2/pages/{page_id}", auth, method="PUT", body=body)


def parse_table_rows(storage_html: str):
    """Return (before_rows, header_row, data_rows_html_list, after_rows) for the
    first <table> in the storage HTML, preserving any <tbody> wrapper, or
    (storage_html, None, [], "") if no table with rows is found."""
    body_match = re.search(r"<tbody[^>]*>(.*?)</tbody>", storage_html, re.S)
    if body_match:
        rows_container = body_match
    else:
        rows_container = re.search(r"<table[^>]*>(.*?)</table>", storage_html, re.S)
    if not rows_container:
        return storage_html, None, [], ""
    inner = rows_container.group(1)
    rows = re.findall(r"<tr[^>]*>.*?</tr>", inner, re.S)
    if not rows:
        return storage_html, None, [], ""
    header_row, data_rows = rows[0], rows[1:]
    before = storage_html[: rows_container.start(1)]
    after = storage_html[rows_container.end(1):]
    return before, header_row, data_rows, after


def cell_text(row_html: str, index: int) -> str:
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S)
    if index >= len(cells):
        return ""
    return unescape(re.sub(r"<[^>]+>", "", cells[index])).strip()


def get_header_labels(header_row_html: str) -> list[str]:
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", header_row_html, re.S)
    return [unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]


def build_header_row(headers: list[str]) -> str:
    return "<tr>" + "".join(f"<th><p>{escape(h)}</p></th>" for h in headers) + "</tr>"


def parse_row_cells(row_html: str) -> list[tuple[int, str]]:
    """[(rowspan, text), ...] for one <tr>'s actual <td>/<th> cells, in
    document order (NOT yet aligned to header columns - rowspan-merged
    columns simply have no cell in this row at all, so a fixed header index
    into this list is wrong; use reconstruct_grid instead)."""
    cells = re.findall(r'<t[dh]( rowspan="(\d+)")?[^>]*>(.*?)</t[dh]>', row_html, re.S)
    return [(int(rs) if rs else 1, unescape(re.sub(r"<[^>]+>", "", text)).strip()) for _, rs, text in cells]


def reconstruct_grid(header_row: str, data_rows: list[str]) -> list[list[str]]:
    """Expand rowspan-merged cells back into a full rectangular grid, one row
    per <tr>, aligned to the header's column count - used wherever a
    nested-mode table's rowspan-merged rows need to be read back (e.g.
    fetch_confluence_known_builds.py, or carrying forward an existing
    per-case value like Analyzed before a full nested-mode rebuild)."""
    ncols = len(get_header_labels(header_row))
    carry: dict[int, tuple[int, str]] = {}  # col -> (remaining_rows, value)
    grid = []
    for row in data_rows:
        raw_cells = iter(parse_row_cells(row))
        full_row = [""] * ncols
        for col in range(ncols):
            if col in carry and carry[col][0] > 0:
                remaining, val = carry[col]
                full_row[col] = val
                carry[col] = (remaining - 1, val)
                if carry[col][0] == 0:
                    del carry[col]
            else:
                rowspan, val = next(raw_cells, (1, ""))
                full_row[col] = val
                if rowspan > 1:
                    carry[col] = (rowspan - 1, val)
        grid.append(full_row)
    return grid


def ensure_columns(header_row_html: str, data_rows: list[str], required: list[str]) -> tuple[str, list[str], list[str], bool]:
    """Return (header_row_html, data_rows, headers, migrated) with every
    header in `required` guaranteed to be present, migrating in place
    (inserting missing columns just before Date, or at the end) if the
    page's existing table predates one or more of them."""
    headers = get_header_labels(header_row_html)
    lower = [h.strip().lower() for h in headers]
    missing = [h for h in required if h.strip().lower() not in lower]
    if not missing:
        return header_row_html, data_rows, headers, False

    insert_at = lower.index("date") if "date" in lower else len(headers)
    new_headers = headers[:insert_at] + missing + headers[insert_at:]
    new_header_row = build_header_row(new_headers)
    placeholder_cells = "".join("<td><p>?</p></td>" for _ in missing)
    migrated_rows = []
    for row in data_rows:
        cells = re.findall(r"<td[^>]*>.*?</td>", row, re.S)
        new_cells = cells[:insert_at] + [placeholder_cells] + cells[insert_at:]
        migrated_rows.append("<tr>" + "".join(new_cells) + "</tr>")
    return new_header_row, migrated_rows, new_headers, True


def value_for_header(case: dict, header: str) -> str:
    lower = header.strip().lower()
    field = CASE_FIELD_BY_HEADER.get(lower)
    if field is None:
        return ""
    if lower == "analyzed":
        default = ANALYZED_DEFAULT
    elif lower in OVERWRITE_HEADERS:
        default = "?"
    else:
        default = ""
    return str(case.get(field, default))


def build_row(case: dict, headers: list[str]) -> str:
    tds = "".join(f"<td><p>{escape(value_for_header(case, h))}</p></td>" for h in headers)
    return f"<tr>{tds}</tr>"


def append_to_row(row_html: str, case: dict, headers: list[str]) -> str:
    """Append failure_analysis/date to their cells (preserving history), and
    overwrite point-in-time status cells (Waived, Waived (Executions),
    Related Bugs, Stack Trace) with the latest values (current flags, not logs) -
    but only for a header whose field the case JSON actually provided a key
    for. A field the caller deliberately left out (e.g. build_confluence_cases.py
    skipping an entity whose executions were all already-known, so it has
    nothing fresh to say) leaves the existing cell untouched rather than
    getting clobbered with a "?" default."""
    cells = re.findall(r"<td[^>]*>.*?</td>", row_html, re.S)
    lower = [h.strip().lower() for h in headers]
    if len(cells) != len(headers):
        return row_html  # unexpected shape (e.g. a manually edited row); leave untouched

    new_cells = list(cells)
    if "failure analysis" in lower:
        i = lower.index("failure analysis")
        new_cells[i] = re.sub(
            r"</td>$", f"<p>[{escape(case['date'])}] {escape(case['failure_analysis'])}</p></td>", cells[i], count=1
        )
    if "date" in lower:
        i = lower.index("date")
        new_cells[i] = re.sub(r"</td>$", f"<p>{escape(case['date'])}</p></td>", cells[i], count=1)
    for header in OVERWRITE_HEADERS:
        field = CASE_FIELD_BY_HEADER.get(header)
        if header in lower and field is not None and field in case:
            i = lower.index(header)
            new_cells[i] = f"<td><p>{escape(str(case[field]))}</p></td>"
    return "<tr>" + "".join(new_cells) + "</tr>"


def build_empty_table(cases: list[dict], headers: list[str] = DEFAULT_HEADERS) -> str:
    header = build_header_row(headers)
    rows = [build_row(c, headers) for c in cases]
    return "<table><tbody>" + header + "".join(rows) + "</tbody></table>"


def sync_cases(storage_html: str, cases: list[dict]) -> tuple[str, int, int, bool]:
    before, header_row, data_rows, after = parse_table_rows(storage_html)
    if header_row is None:
        new_table = build_empty_table(cases)
        new_html = storage_html + new_table if storage_html.strip() else new_table
        return new_html, len(cases), 0, False

    header_row, data_rows, headers, migrated = ensure_columns(header_row, data_rows, DEFAULT_HEADERS)
    lower = [h.strip().lower() for h in headers]
    name_idx = lower.index("case name") if "case name" in lower else 0
    pr_idx = lower.index("pr number") if "pr number" in lower else 1

    added, updated = 0, 0
    for case in cases:
        matched_idx = None
        for i, row in enumerate(data_rows):
            if (
                cell_text(row, name_idx).strip().lower() == case["case_name"].strip().lower()
                and cell_text(row, pr_idx).strip() == str(case["pr_number"]).strip()
            ):
                matched_idx = i
                break
        if matched_idx is None:
            data_rows.append(build_row(case, headers))
            added += 1
        else:
            data_rows[matched_idx] = append_to_row(data_rows[matched_idx], case, headers)
            updated += 1

    new_table_inner = header_row + "".join(data_rows)
    return before + new_table_inner + after, added, updated, migrated


# Nested layout: one case -> its PRs -> each PR's builds. Case-level cells
# (name, waive state in the latest main, latest status) are rowspan-merged
# over all of the case's rows; the PR cell over that PR's build rows; every
# other column is per build.
NESTED_CASE_HEADERS = ["Case name", "Waived (latest main)", "Latest Status"]
NESTED_PR_HEADERS = ["PR number"]
NESTED_BUILD_HEADERS = ["Build", "Triggered (UTC)", "Base commit", "Error / callstack", "Waived at run", "Bug",
                        "Analyzed", "Failure analysis", "Failure type"]
NESTED_HEADERS = NESTED_CASE_HEADERS + NESTED_PR_HEADERS + NESTED_BUILD_HEADERS
# build-row field -> header
NESTED_BUILD_FIELDS = {"build": "Build", "triggered": "Triggered (UTC)", "base_commit": "Base commit",
                       "error": "Error / callstack", "waived": "Waived at run", "bug": "Bug",
                       "analyzed": "Analyzed", "failure_analysis": "Failure analysis", "failure_type": "Failure type"}
# per-build fields that are refreshed from the new data whenever present;
# "analyzed" is sticky (human-edited) and only changes on an explicit reset.
NESTED_BUILD_REFRESH = {"triggered", "base_commit", "error", "waived", "bug", "failure_analysis", "failure_type"}


def _nested_headers_match(header_row_html: str | None) -> bool:
    if header_row_html is None:
        return False
    labels = [h.strip().lower() for h in get_header_labels(header_row_html)]
    return labels == [h.lower() for h in NESTED_HEADERS]


def parse_nested_table(header_row_html: str, data_rows: list[str]) -> dict:
    """Read an existing nested table back into
    {case_name: {"waived_latest", "latest_status", "prs": {pr: {build: {field: value}}}}}
    using the rowspan-expanded grid."""
    labels = [h.strip().lower() for h in get_header_labels(header_row_html)]
    idx = {h.lower(): i for i, h in enumerate(labels)}
    grid = reconstruct_grid(header_row_html, data_rows)
    existing: dict = {}
    for row in grid:
        name = row[idx["case name"]].strip()
        if not name:
            continue
        case = existing.setdefault(name, {"waived_latest": row[idx["waived (latest main)"]].strip(),
                                          "latest_status": row[idx["latest status"]].strip(), "prs": {}})
        pr = row[idx["pr number"]].strip() or "n/a"
        build = row[idx["build"]].strip() or "n/a"
        rec = {f: row[idx[h.lower()]] for f, h in NESTED_BUILD_FIELDS.items() if h.lower() in idx}
        rec["build"] = build
        case["prs"].setdefault(pr, {})[build] = rec
    return existing


def merge_nested(existing: dict, cases: list[dict]) -> tuple[list[dict], int, int]:
    """Merge new nested cases into the parsed existing table. Returns
    (merged cases in publish shape, builds added, builds updated).
    - unknown case / PR / build -> added
    - known build -> refresh NESTED_BUILD_REFRESH fields that the new data
      provides (non-empty); keep Analyzed unless the case carries
      reset_analyzed or the build provides an explicit `analyzed`
    - cases / builds only on the page are kept untouched."""
    added = updated = 0
    for case in cases:
        name = case["case_name"]
        cur = existing.setdefault(name, {"waived_latest": "?", "latest_status": "?", "prs": {}})
        if case.get("waived_latest"):
            cur["waived_latest"] = case["waived_latest"]
        if case.get("latest_status"):
            cur["latest_status"] = case["latest_status"]
        reset = bool(case.get("reset_analyzed"))
        if reset:
            for pr in cur["prs"].values():
                for rec in pr.values():
                    rec["analyzed"] = ANALYZED_DEFAULT
        for pr in case.get("prs") or []:
            pr_id = str(pr.get("pr", "n/a"))
            cur_pr = cur["prs"].setdefault(pr_id, {})
            for b in pr.get("builds") or [{}]:
                build = str(b.get("build", "n/a"))
                if build in cur_pr:
                    rec = cur_pr[build]
                    changed = False
                    for f in NESTED_BUILD_REFRESH:
                        v = b.get(f)
                        if v not in (None, "") and str(v) != rec.get(f, ""):
                            rec[f] = str(v)
                            changed = True
                    if b.get("analyzed") not in (None, "") and str(b["analyzed"]) != rec.get("analyzed", ""):
                        rec["analyzed"] = str(b["analyzed"])
                        changed = True
                    updated += 1 if changed else 0
                else:
                    rec = {f: str(b.get(f, "n/a" if f != "analyzed" else ANALYZED_DEFAULT)) for f in NESTED_BUILD_FIELDS}
                    if b.get("analyzed") in (None, ""):
                        rec["analyzed"] = ANALYZED_DEFAULT
                    for f in ("failure_analysis", "failure_type"):
                        if b.get(f) in (None, ""):
                            rec[f] = "?"
                    cur_pr[build] = rec
                    added += 1
    merged = []
    for name in sorted(existing):
        c = existing[name]
        prs = []
        for pr_id in sorted(c["prs"], key=lambda p: (0, int(p)) if p.isdigit() else (1, p)):
            builds = [c["prs"][pr_id][b] for b in sorted(c["prs"][pr_id], key=lambda x: int(x) if x.isdigit() else 0)]
            prs.append({"pr": pr_id, "builds": builds})
        merged.append({"case_name": name, "waived_latest": c["waived_latest"], "latest_status": c["latest_status"], "prs": prs})
    return merged, added, updated


def build_nested_table(cases: list[dict]) -> str:
    """Render the nested layout (see NESTED_HEADERS). Input case shape:
      {"case_name": str, "waived_latest": str, "latest_status": str,
       "prs": [{"pr": str, "builds": [{"build", "triggered", "base_commit", "error",
                "waived", "bug", "analyzed", "failure_analysis", "failure_type"}, ...]}, ...]}
    Missing build fields render as "n/a" (Analyzed -> ANALYZED_DEFAULT,
    Failure analysis / type -> "?")."""
    header = build_header_row(NESTED_HEADERS)
    row_htmls = []
    for case in cases:
        prs = case.get("prs") or [{"pr": "n/a", "builds": []}]
        total_rows = sum(max(1, len(pr.get("builds") or [])) for pr in prs) or 1
        first_case_row = True
        for pr in prs:
            builds = pr.get("builds") or [{}]
            pr_rows = len(builds)
            first_pr_row = True
            for b in builds:
                cells = []
                if first_case_row:
                    rs = f' rowspan="{total_rows}"' if total_rows > 1 else ""
                    cells.append(f'<td{rs}><p>{escape(str(case["case_name"]))}</p></td>')
                    cells.append(f'<td{rs}><p>{escape(str(case.get("waived_latest", "?")))}</p></td>')
                    cells.append(f'<td{rs}><p>{escape(str(case.get("latest_status", "?")))}</p></td>')
                    first_case_row = False
                if first_pr_row:
                    rs = f' rowspan="{pr_rows}"' if pr_rows > 1 else ""
                    cells.append(f'<td{rs}><p>{escape(str(pr.get("pr", "n/a")))}</p></td>')
                    first_pr_row = False
                for f in NESTED_BUILD_FIELDS:
                    default = ANALYZED_DEFAULT if f == "analyzed" else ("?" if f in ("failure_analysis", "failure_type") else "n/a")
                    val = b.get(f)
                    val = default if val in (None, "") else val
                    cells.append(f'<td><p>{escape(str(val))}</p></td>')
                row_htmls.append("<tr>" + "".join(cells) + "</tr>")
    return "<table><tbody>" + header + "".join(row_htmls) + "</tbody></table>"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases-json", help="Path to a JSON file: list of {case_name, pr_number, failure_analysis, waived, date}")
    parser.add_argument("--nested-json", help="Path to a JSON file in the nested {case_name, prs: [{pr, builds: [{build, date, ...}]}]} shape (see build_nested_table docstring). Always fully replaces the table - mutually exclusive with --cases-json.")
    parser.add_argument("--page-id", default=CONFLUENCE_PAGE_ID, help="Confluence page ID (default: the CI failure cases records page, see confluence_config.py)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH))
    parser.add_argument("--dry-run", action="store_true", help="Print the resulting page body instead of publishing")
    parser.add_argument("--replace", action="store_true", help="Discard the existing table entirely and rebuild it from just --cases-json, instead of merging with what's already on the page")
    args = parser.parse_args()

    if bool(args.cases_json) == bool(args.nested_json):
        sys.exit("Specify exactly one of --cases-json or --nested-json")

    email, api_token = load_credentials(Path(args.credentials))
    auth = auth_header(email, api_token)

    page = get_page(args.base_url, args.page_id, auth)
    storage_html = page["body"]["storage"]["value"]
    title = page["title"]
    current_version = page["version"]["number"]

    if args.nested_json:
        nested_cases = json.loads(Path(args.nested_json).read_text())
        before, header_row, data_rows, after = parse_table_rows(storage_html)

        # Nested mode MERGES build by build into the page's existing nested
        # table: unknown case/PR/build rows are added, known builds get their
        # refreshable cells updated, and everything only on the page is
        # kept. "Analyzed" is sticky (human-edited) - it is carried forward
        # unless the case carries reset_analyzed (pass->fail regression
        # detected by build_confluence_cases.py) or a build provides an
        # explicit value. A page whose table is not in the nested layout
        # (e.g. the legacy flat table) cannot be merged into; that needs
        # --replace, which discards the old table.
        if header_row is not None and _nested_headers_match(header_row) and not args.replace:
            existing = parse_nested_table(header_row, data_rows)
            merged, added, updated = merge_nested(existing, nested_cases)
            verb = f"merge into the nested table: +{added} build row(s) added, {updated} updated; {len(merged)} case(s) total"
        elif header_row is not None and not _nested_headers_match(header_row) and not args.replace:
            sys.exit(
                "The page's table is not in the nested layout (headers differ from NESTED_HEADERS); "
                "merging is not possible. Re-run with --replace to rebuild the table in the nested layout "
                "(the existing table is discarded - confirm with the user first), or use --cases-json for the flat layout."
            )
        else:
            merged, added, updated = merge_nested({}, nested_cases)
            verb = f"replace the table with a nested breakdown: {len(merged)} case(s), {added} build row(s)"

        new_table = build_nested_table(merged)
        if header_row is None:
            new_html = storage_html + new_table if storage_html.strip() else new_table
        else:
            new_html = before + new_table.replace("<table><tbody>", "").replace("</tbody></table>", "") + after
        if args.dry_run:
            print(f"Would {verb} ({len(new_html):,} bytes of HTML).")
            print(new_html)
            return
        update_page(args.base_url, args.page_id, auth, title, current_version + 1, new_html)
        print(f"Published to {args.base_url}/pages/{args.page_id}: {verb}.")
        return

    cases = json.loads(Path(args.cases_json).read_text())
    for c in cases:
        for field in ("case_name", "pr_number", "failure_analysis", "date"):
            if field not in c:
                sys.exit(f"Case missing required field '{field}': {c}")
        # Note: waived/waived_executions/related_bugs/stack_trace/build_pr_mapping
        # are intentionally NOT defaulted here. A field's absence is a
        # meaningful signal on update (leave the existing cell untouched -
        # see append_to_row) - only a brand-new row falls back to "?" for a
        # missing field, via value_for_header's own default.

    migrated = False
    if args.replace:
        before, header_row, _, after = parse_table_rows(storage_html)
        if header_row is None:
            new_html = storage_html + build_empty_table(cases) if storage_html.strip() else build_empty_table(cases)
        else:
            new_html = before + build_empty_table(cases).replace("<table><tbody>", "").replace("</tbody></table>", "") + after
        added, updated = len(cases), 0
    else:
        new_html, added, updated, migrated = sync_cases(storage_html, cases)

    if args.dry_run:
        verb = "replace the table with" if args.replace else f"add {added} new case(s), update {updated} existing case(s)"
        migrate_note = " (existing rows would be migrated to add missing columns)" if migrated else ""
        print(f"Would {verb}{migrate_note}.")
        print(new_html)
        return

    update_page(args.base_url, args.page_id, auth, title, current_version + 1, new_html)
    migrate_note = " (migrated existing rows to add missing columns)" if migrated else ""
    if args.replace:
        print(f"Published to {args.base_url}/pages/{args.page_id}: replaced table with {added} case(s).")
    else:
        print(f"Published to {args.base_url}/pages/{args.page_id}: added {added} new case(s), updated {updated} existing case(s){migrate_note}.")


if __name__ == "__main__":
    main()
