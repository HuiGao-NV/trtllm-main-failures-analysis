"""Single source of truth for which Confluence page this skill publishes the
CI failure case log to. Both post_confluence_cases.py and
fetch_confluence_known_builds.py import from here instead of hardcoding the
page id/base URL themselves, so there's exactly one place to repoint the
skill at a different page.
"""

CONFLUENCE_BASE_URL = "https://nvidia.atlassian.net/wiki"
CONFLUENCE_PAGE_ID = "3843562196"
CONFLUENCE_PAGE_URL = f"{CONFLUENCE_BASE_URL}/spaces/~huig/pages/{CONFLUENCE_PAGE_ID}/CI+failure+cases+records"
