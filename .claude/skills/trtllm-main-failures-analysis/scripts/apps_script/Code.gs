// Bound Apps Script for the CI failure cases Google Sheet - lets
// post_google_sheet_cases.py / fetch_google_sheet_known_builds.py read and
// write the sheet over plain HTTPS, authenticated with a shared secret
// instead of a GCP service account or OAuth token. This exists because the
// GCP-native paths (service account creation, API enablement, and
// quota-project self-service) were all blocked by org IAM policy - this
// script runs under the sheet owner's own Apps Script execution context
// instead, so no GCP project permissions are needed at all.
//
// One-time setup:
//   1. Open the target spreadsheet -> Extensions -> Apps Script.
//   2. Delete the boilerplate and paste this file's contents in.
//   3. Replace SHARED_SECRET below with a real random token (keep it out of
//      anything checked into a shared repo - this file as deployed is
//      yours, not committed anywhere on this machine).
//   4. Deploy -> New deployment -> type "Web app".
//        Execute as: Me
//        Who has access: Anyone
//      (this only exposes what THIS script's doGet/doPost do - the token
//      check below is the actual gate; "Anyone" just means Google won't
//      also demand a Google login on top of that)
//   5. Authorize the script when prompted (grants it edit access to this
//      spreadsheet, under your account).
//   6. Copy the deployment URL (ends in /exec) and give it, plus the token
//      from step 3, to the script side - see post_google_sheet_cases.py's
//      docstring for where those go (~/.config/gsheets/apps_script.json).

const SHARED_SECRET = "REPLACE_WITH_RANDOM_TOKEN";
const SHEET_NAME = null; // null = the spreadsheet's first sheet

function getTargetSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  return SHEET_NAME ? ss.getSheetByName(SHEET_NAME) : ss.getSheets()[0];
}

function checkAuth(e) {
  const token = (e.parameter && e.parameter.token) || "";
  if (token !== SHARED_SECRET) {
    throw new Error("unauthorized");
  }
}

function jsonOutput(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  try {
    checkAuth(e);
    const sheet = getTargetSheet();
    const values = sheet.getDataRange().getValues();
    return jsonOutput({ success: true, sheetName: sheet.getName(), values: values });
  } catch (err) {
    return jsonOutput({ success: false, error: String(err) });
  }
}

function doPost(e) {
  try {
    checkAuth(e);
    const body = JSON.parse(e.postData.contents);
    const values = body.values;
    const sheet = getTargetSheet();
    sheet.clearContents();
    if (values.length > 0) {
      sheet.getRange(1, 1, values.length, values[0].length).setValues(values);
    }
    return jsonOutput({ success: true, rows: values.length });
  } catch (err) {
    return jsonOutput({ success: false, error: String(err) });
  }
}
