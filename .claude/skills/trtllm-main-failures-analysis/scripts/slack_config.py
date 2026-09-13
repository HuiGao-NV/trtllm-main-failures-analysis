"""Single source of truth for how post_slack_message.py authenticates to Slack.

Only the credentials path lives here (unlike confluence_config.py /
google_sheets_config.py, there's no fixed destination page/sheet to pin down -
every send targets whichever user(s) are named on the command line).
"""

SLACK_API_BASE_URL = "https://slack.com/api"
