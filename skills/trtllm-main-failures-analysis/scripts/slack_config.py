"""Single source of truth for how post_slack_message.py authenticates to Slack.

Only the credentials path lives here (unlike confluence_config.py /
google_sheets_config.py, there's no fixed destination page/sheet to pin down -
every send targets whichever user(s) are named on the command line).
"""

SLACK_API_BASE_URL = "https://slack.com/api"

# Recipient placeholder for `notify-infra` actions (Step 5 `infra` handler).
# Infra failures are never attributed to a PR, so no PR-author lookup happens
# for them: the notification goes to the infra team via this value. Replace
# the placeholder with the team's real Slack user id / email / channel once
# known; until then post_slack_message.py --dry-run shows it unresolved.
INFRA_TEAM_RECIPIENT = "<INFRA_TEAM_SLACK_PLACEHOLDER>"
