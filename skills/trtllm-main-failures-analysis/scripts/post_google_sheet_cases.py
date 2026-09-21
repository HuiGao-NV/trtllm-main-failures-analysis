#!/usr/bin/env python3
"""Publish the same CI-failure case JSON that post_confluence_cases.py writes
to Confluence into a Google Sheet instead (a second publish target, same
data - see build_confluence_cases.py --mode flat for how the case JSON is
built). Only the flat (one row per entity) shape is supported; there's no
Sheets equivalent of the nested/rowspan Confluence table.

Sync semantics mirror post_confluence_cases.py's flat mode as closely as a
plain grid allows:
  - a case not already on the sheet (matched by Case name + PR number)
    becomes a new row
  - a case already there gets Failure analysis/Date appended to their cells
    (preserving history, not overwritten) and its status cells (Waived,
    Waived (Executions), Related Bugs, Stack Trace, Build-PR Mapping)
    overwritten with the latest value - but ONLY for a field the case JSON
    actually provides; an absent field leaves that cell untouched, so
    publishing an incremental case set (e.g. from
    fetch_execution_details.py --known-builds-json) never clobbers
    previously-recorded detail with a placeholder. See post_confluence_cases.py
    for the reasoning; the header/field constants are imported from there so
    the two publish targets can't drift out of sync with each other.
  - the whole sheet range is read, merged with the new cases in memory, and
    written back in one call - simpler and just as correct as an
    incremental per-cell update for a few hundred rows.

Auth: three supported paths, tried in this order.
  1. Apps Script webhook - a JSON credentials file at
     ~/.config/gsheets/apps_script.json: {"url": "https://script.google.com/macros/s/.../exec",
     "token": "..."}. Use this when the GCP-native paths below are blocked
     by org IAM policy (no permission to create service accounts, enable
     APIs, or self-assign a quota project - all real-world corporate
     lockdowns this script has hit). See apps_script/Code.gs in this
     directory for the script to deploy (bound to the target spreadsheet,
     under the sheet owner's own Apps Script execution context - no GCP
     project permissions involved at all) and the deployment steps in its
     header comment. This is the ONLY path of the three that also needs a
     matching entry on the Sheets side (the deployed script); the other two
     just need Google Cloud credentials.
  2. A GCP service account JSON key at ~/.config/gsheets/service_account.json
     (or --credentials). Setup, one time: enable the Sheets API on a GCP
     project, create a service account (no project roles needed - access
     comes from sharing the sheet), download its JSON key to that path, and
     share the target spreadsheet with the key's `client_email` as Editor.
     Needs `pyjwt` and `cryptography` (both already available in this
     environment) to sign the JWT bearer assertion for the token exchange -
     no full google-auth/gspread dependency required.
  3. If neither of the above exists: the user's own `gcloud` OAuth,
     re-scoped for Sheets. One-time setup:
       gcloud auth application-default login --scopes=openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets
     (cloud-platform is required by gcloud's default OAuth client whenever
     any custom scope is requested, not something Sheets-specific), then
     share the spreadsheet with your own Google account if it isn't already
     accessible to you. Note this path additionally needs a *quota project*
     set for Application Default Credentials
     (https://cloud.google.com/docs/authentication/adc-troubleshooting/user-creds) -
     if your org also blocks self-service quota-project assignment, this
     path won't work either and you're back to path 1.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# gcloud's default OAuth client requires cloud-platform whenever any custom
# scope is requested at all (not just Sheets-related ones) - omit it and
# `gcloud auth application-default login --scopes=...` rejects the whole
# request outright ("cloud-platform scope is required but not requested").
GCLOUD_SCOPES = (
    "openid,https://www.googleapis.com/auth/userinfo.email,"
    "https://www.googleapis.com/auth/cloud-platform,"
    "https://www.googleapis.com/auth/spreadsheets"
)
GCLOUD_LOGIN_HINT = f"gcloud auth application-default login --scopes={GCLOUD_SCOPES}"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from google_sheets_config import SPREADSHEET_ID  # noqa: E402
from post_confluence_cases import (  # noqa: E402
    ANALYZED_DEFAULT,
    APPEND_HEADERS,
    CASE_FIELD_BY_HEADER,
    DEFAULT_HEADERS,
    OVERWRITE_HEADERS,
)

DEFAULT_CREDS_PATH = Path.home() / ".config" / "gsheets" / "service_account.json"
DEFAULT_APPS_SCRIPT_CREDS_PATH = Path.home() / ".config" / "gsheets" / "apps_script.json"
TOKEN_URI_DEFAULT = "https://oauth2.googleapis.com/token"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class Backend:
    """Wraps whichever of the two data-access mechanisms is configured:
    the Apps Script webhook (mode="apps_script") or the Sheets REST API
    (mode="sheets_api", using an OAuth access token from either a service
    account or gcloud). get_values/write_values/get_first_sheet_title all
    dispatch on `mode` so callers don't need to care which one is active."""

    def __init__(self, mode: str, *, apps_script_url: str = "", apps_script_token: str = "", access_token: str = ""):
        self.mode = mode
        self.apps_script_url = apps_script_url
        self.apps_script_token = apps_script_token
        self.access_token = access_token
        self._apps_script_cache: dict | None = None  # doGet is read-once-reused within a run

    def _apps_script_call(self, method: str = "GET", body: dict | None = None) -> dict:
        sep = "&" if "?" in self.apps_script_url else "?"
        url = f"{self.apps_script_url}{sep}token={urllib.parse.quote(self.apps_script_token, safe='')}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            sys.exit(f"Apps Script call failed ({e.code}): {e.read().decode()[:2000]}")
        except urllib.error.URLError as e:
            sys.exit(f"Apps Script call failed to reach {self.apps_script_url}: {e}")
        if not result.get("success"):
            sys.exit(f"Apps Script returned an error: {result.get('error')}")
        return result

    def get_first_sheet_title(self, spreadsheet_id: str) -> str:
        if self.mode == "apps_script":
            if self._apps_script_cache is None:
                self._apps_script_cache = self._apps_script_call()
            return self._apps_script_cache["sheetName"]
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?fields=sheets.properties.title"
        data = sheets_api_request(url, self.access_token)
        return data["sheets"][0]["properties"]["title"]

    def get_values(self, spreadsheet_id: str, sheet_name: str) -> list[list[str]]:
        if self.mode == "apps_script":
            if self._apps_script_cache is None:
                self._apps_script_cache = self._apps_script_call()
            return self._apps_script_cache.get("values", [])
        rng = urllib.parse.quote(sheet_name, safe="")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}"
        data = sheets_api_request(url, self.access_token)
        return data.get("values", [])

    def write_values(self, spreadsheet_id: str, sheet_name: str, grid: list[list[str]]) -> None:
        if self.mode == "apps_script":
            self._apps_script_call(method="POST", body={"values": grid})
            return
        rng = urllib.parse.quote(sheet_name, safe="")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}?valueInputOption=RAW"
        sheets_api_request(url, self.access_token, method="PUT", body={"values": grid})


def load_apps_script_config(path: Path) -> dict | None:
    if not path.exists():
        return None
    config = json.loads(path.read_text())
    for field in ("url", "token"):
        if field not in config:
            sys.exit(f"{path} is missing required field '{field}'")
    return config


def load_service_account(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def get_access_token_from_service_account(service_account: dict) -> str:
    try:
        import jwt  # PyJWT
    except ImportError:
        sys.exit("PyJWT is required (pip install pyjwt cryptography) to sign the service-account JWT.")

    now = int(time.time())
    token_uri = service_account.get("token_uri", TOKEN_URI_DEFAULT)
    claims = {
        "iss": service_account["client_email"],
        "scope": SHEETS_SCOPE,
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    }
    assertion = jwt.encode(claims, service_account["private_key"], algorithm="RS256")

    body = (
        "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
        f"&assertion={assertion}"
    ).encode()
    req = urllib.request.Request(
        token_uri, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Failed to get an access token ({e.code}): {e.read().decode()[:1000]}")


def get_access_token_from_gcloud() -> str | None:
    """Fall back to the user's own gcloud application-default credentials,
    re-scoped for Sheets (see GCLOUD_LOGIN_HINT). Returns None if gcloud
    isn't installed or application-default credentials aren't configured at
    all (as opposed to configured-but-wrong-scope/no-quota-project, which
    are Sheets API errors we let happen and explain in sheets_api_request)."""
    try:
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def get_backend(apps_script_path: Path, service_account_path: Path) -> Backend:
    apps_script_config = load_apps_script_config(apps_script_path)
    if apps_script_config is not None:
        return Backend(mode="apps_script", apps_script_url=apps_script_config["url"], apps_script_token=apps_script_config["token"])

    service_account = load_service_account(service_account_path)
    if service_account is not None:
        return Backend(mode="sheets_api", access_token=get_access_token_from_service_account(service_account))

    token = get_access_token_from_gcloud()
    if token is not None:
        return Backend(mode="sheets_api", access_token=token)

    sys.exit(
        f"No usable Sheets credential found. Any of:\n"
        f"  1. An Apps Script webhook config at {apps_script_path} - use this if your org blocks the\n"
        f"     GCP-native paths below (service account creation, API enablement, quota-project\n"
        f"     self-service). See apps_script/Code.gs for the script to deploy.\n"
        f"  2. A service account JSON key at {service_account_path} (or --credentials), with the\n"
        f"     target spreadsheet shared to its client_email as Editor.\n"
        f"  3. Run: {GCLOUD_LOGIN_HINT}\n"
        f"     then share the spreadsheet with your own Google account if it isn't already accessible."
    )


def sheets_api_request(url: str, access_token: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        if e.code == 403 and "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in body_text:
            sys.exit(
                "Sheets API error 403: the active credential doesn't carry Sheets scope.\n"
                f"If you're using gcloud auth, run:\n  {GCLOUD_LOGIN_HINT}\n"
                "then try again."
            )
        if e.code == 403 and "quota project" in body_text:
            sys.exit(
                "Sheets API error 403: Application Default Credentials need a quota project, and\n"
                "either none is set or your org blocks self-service quota-project assignment.\n"
                "See this script's docstring for the Apps Script webhook fallback (path 1),\n"
                "which doesn't need a quota project at all."
            )
        sys.exit(f"Sheets API error {e.code} on {method} {url}: {body_text[:2000]}")


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


def sync_grid(existing: list[list[str]], cases: list[dict]) -> tuple[list[list[str]], int, int]:
    """Merge `cases` into `existing` (a raw values grid, header row first),
    matching by (Case name, PR number). Returns (new_grid, added, updated)."""
    if not existing:
        header = DEFAULT_HEADERS
        rows = []
    else:
        header = existing[0]
        rows = [list(r) for r in existing[1:]]

    lower_header = [h.strip().lower() for h in header]
    ncols = len(header)

    def pad(row: list[str]) -> list[str]:
        return row + [""] * (ncols - len(row))

    name_idx = lower_header.index("case name") if "case name" in lower_header else 0
    pr_idx = lower_header.index("pr number") if "pr number" in lower_header else 1

    added, updated = 0, 0
    for case in cases:
        match_idx = None
        for i, row in enumerate(rows):
            row = pad(row)
            if (
                (row[name_idx] if name_idx < len(row) else "").strip().lower() == case["case_name"].strip().lower()
                and (row[pr_idx] if pr_idx < len(row) else "").strip() == str(case["pr_number"]).strip()
            ):
                match_idx = i
                break

        if match_idx is None:
            new_row = [value_for_header(case, h) for h in header]
            rows.append(new_row)
            added += 1
            continue

        row = pad(rows[match_idx])
        for i, h in enumerate(header):
            hl = h.strip().lower()
            if hl in APPEND_HEADERS:
                field = CASE_FIELD_BY_HEADER.get(hl)
                if field and field in case:
                    prior = row[i]
                    entry = f"[{case['date']}] {case[field]}" if hl == "failure analysis" else str(case[field])
                    row[i] = f"{prior}\n{entry}" if prior else entry
            elif hl in OVERWRITE_HEADERS:
                field = CASE_FIELD_BY_HEADER.get(hl)
                if field and field in case:
                    row[i] = str(case[field])
        rows[match_idx] = row
        updated += 1

    return [header] + rows, added, updated


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases-json", required=True, help="Flat case JSON from build_confluence_cases.py --mode flat")
    parser.add_argument("--spreadsheet-id", default=SPREADSHEET_ID, help="Ignored in Apps Script mode - the deployed script is already bound to one spreadsheet")
    parser.add_argument("--sheet-name", default=None, help="Tab name (default: the spreadsheet's first sheet; ignored in Apps Script mode, which uses whatever SHEET_NAME the deployed script is configured with)")
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH), help="Service account JSON key path (path 2)")
    parser.add_argument("--apps-script-credentials", default=str(DEFAULT_APPS_SCRIPT_CREDS_PATH), help="Apps Script webhook config path (path 1)")
    parser.add_argument("--dry-run", action="store_true", help="Print the resulting grid instead of writing it")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases_json).read_text())
    for c in cases:
        for field in ("case_name", "pr_number", "failure_analysis", "date"):
            if field not in c:
                sys.exit(f"Case missing required field '{field}': {c}")

    backend = get_backend(Path(args.apps_script_credentials), Path(args.credentials))

    sheet_name = args.sheet_name or backend.get_first_sheet_title(args.spreadsheet_id)
    existing = backend.get_values(args.spreadsheet_id, sheet_name)
    new_grid, added, updated = sync_grid(existing, cases)

    if args.dry_run:
        print(f"Would add {added} new row(s), update {updated} existing row(s) on sheet '{sheet_name}' (via {backend.mode}).")
        for row in new_grid[:5]:
            print(row)
        if len(new_grid) > 5:
            print(f"... ({len(new_grid) - 5} more rows)")
        return

    backend.write_values(args.spreadsheet_id, sheet_name, new_grid)
    print(
        f"Published to https://docs.google.com/spreadsheets/d/{args.spreadsheet_id} "
        f"(sheet '{sheet_name}', via {backend.mode}): added {added} new row(s), updated {updated} existing row(s)."
    )


if __name__ == "__main__":
    main()
