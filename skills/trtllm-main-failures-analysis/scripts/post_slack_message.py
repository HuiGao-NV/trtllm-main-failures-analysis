#!/usr/bin/env python3
"""Send a Slack DM to one or more users. Only ever run when the user of this
skill explicitly asks to notify someone - never as a default part of a
fetch/triage/report run, and never to a channel/broadcast by default.

Recipients are resolved per invocation, not stored anywhere: pass one
--user per recipient, each either a Slack member id (starts with "U") or an
email address (resolved to a member id via users.lookupByEmail - this must
be an nvidia.com address registered in the workspace).

Auth: reads {"bot_token": "xoxb-..."} from a local credentials file (default
~/.config/slack/credentials.json). The token needs the chat:write and
users:read.email OAuth scopes. If the file is missing, this prints setup
instructions rather than guessing at a token.

Usage:
  python3 post_slack_message.py --user jerryg@nvidia.com --user U01ABCDEF \\
      --message "Heads up: unittest/_torch/attention is still flaky on DGX_B200, not yet waived. See <confluence-link>." \\
      [--dry-run]
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from slack_config import SLACK_API_BASE_URL  # noqa: E402

DEFAULT_CREDS_PATH = Path.home() / ".config" / "slack" / "credentials.json"


def load_bot_token(path: Path) -> str:
    if not path.exists():
        sys.exit(
            f"Credentials file not found: {path}\n"
            'Create it with: {"bot_token": "xoxb-..."}\n'
            "The token needs the chat:write and users:read.email scopes.\n"
            "Create/install a Slack app at https://api.slack.com/apps, "
            "add those Bot Token Scopes under OAuth & Permissions, install "
            "it to the workspace, and copy the Bot User OAuth Token."
        )
    data = json.loads(path.read_text())
    return data["bot_token"]


def api_call(base_url: str, method: str, token: str, params: dict) -> dict:
    # form-encoded, not JSON: some Slack Web API methods (users.lookupByEmail
    # in particular) reject an application/json body with invalid_arguments.
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"{base_url}/{method}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Slack API HTTP error on {method}: {e.code} {e.read().decode(errors='replace')}")
    if not result.get("ok"):
        sys.exit(f"Slack API error on {method}: {result.get('error')} (params: {params})")
    return result


def resolve_user_id(base_url: str, token: str, identifier: str) -> str:
    if identifier.startswith("U") and " " not in identifier and "@" not in identifier:
        return identifier
    result = api_call(base_url, "users.lookupByEmail", token, {"email": identifier})
    return result["user"]["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", action="append", required=True, dest="users",
                         help="Recipient: Slack member id or email address. Repeatable.")
    msg = parser.add_mutually_exclusive_group(required=True)
    msg.add_argument("--message", help="Message text to send (Slack mrkdwn supported).")
    msg.add_argument("--message-file", type=Path,
                     help="Read the message text from this file (Slack mrkdwn; preferred for multi-line messages).")
    parser.add_argument("--creds", type=Path, default=DEFAULT_CREDS_PATH, help=f"Path to credentials JSON (default {DEFAULT_CREDS_PATH})")
    parser.add_argument("--base-url", default=SLACK_API_BASE_URL, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Resolve recipients and print the message instead of sending")
    args = parser.parse_args()

    token = load_bot_token(args.creds)
    message = args.message if args.message is not None else args.message_file.read_text(encoding="utf-8")
    if not message.strip():
        raise SystemExit("message is empty")
    if len(message) > 3500:
        print(f"warning: message is {len(message)} characters; Slack may truncate or reject messages over ~4000", file=sys.stderr)

    resolved = []
    for identifier in args.users:
        user_id = resolve_user_id(args.base_url, token, identifier)
        resolved.append((identifier, user_id))

    if args.dry_run:
        print("Would send to:")
        for identifier, user_id in resolved:
            print(f"  {identifier} -> {user_id}")
        print("Message:")
        print(message)
        return

    for identifier, user_id in resolved:
        im = api_call(args.base_url, "conversations.open", token, {"users": user_id})
        channel_id = im["channel"]["id"]
        api_call(args.base_url, "chat.postMessage", token, {"channel": channel_id, "text": message, "mrkdwn": True})
        print(f"Sent to {identifier} ({user_id})")


if __name__ == "__main__":
    main()
