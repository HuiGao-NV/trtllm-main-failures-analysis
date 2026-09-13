"""Single source of truth for which Google Sheet this skill publishes the CI
failure case log to (a second, alongside the Confluence page in
confluence_config.py). post_google_sheet_cases.py imports from here instead
of hardcoding the spreadsheet id itself.
"""

SPREADSHEET_ID = "1QhCa9HKmq70ih1YXXkxPmOzw8827Zn0I_iYfXjlfndc"
SPREADSHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
