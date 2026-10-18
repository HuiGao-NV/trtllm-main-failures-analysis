# CI-analysis Slack bot

A Slack front-end for the Claude Code skills in this repo. Mention the bot
(or DM it), it runs `claude -p` in this repository with the request, and
posts the answer back in the thread. Follow-ups in the same thread resume
the same Claude session.

Ported from `repair-bot/app.py` (`slack_bolt` Socket Mode, in-memory event
dedupe, reply-in-thread, nohup launcher). Differences: `claude -p` subprocess
instead of the Agent SDK, `/skill` routing over `skills/*/SKILL.md`,
per-thread session resume, chunked mrkdwn replies, cancel, SQLite log.

```
slack_bot/
  app.py          Bolt app: handlers, worker threads, progress message, replies
  agent.py        builds and runs the `claude -p` command, cancel/timeout, JSON parse
  router.py       message -> (skill, prompt) | help | stop; skill discovery
  formatting.py   markdown -> mrkdwn, 3,900-char chunking, file-upload fallback
  store.py        SQLite: thread -> session id, request log
  config.py       env / .env / ~/.config/slack/credentials.json
  slack-bot       setup | start | stop | restart | status | logs
  tests/          pytest (no network, no Slack)
```

## Slack app setup (once)

1. https://api.slack.com/apps → create app (from scratch).
2. **Socket Mode** → enable → create an app-level token with `connections:write` (`xapp-…`).
3. **OAuth & Permissions** → Bot Token Scopes: `app_mentions:read`, `chat:write`,
   `im:history`, `im:read`, `im:write`, `users:read.email`, and `files:write`
   (optional, lets very long replies go out as a file). Install to workspace
   and copy the Bot User OAuth Token (`xoxb-…`).

   Put both tokens in the credentials file that `post_slack_message.py`
   already uses (`chmod 600`):
   ```json
   // ~/.config/slack/credentials.json
   {"bot_token": "xoxb-...", "app_token": "xapp-..."}
   ```
   Alternatively set `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` in `slack_bot/.env`
   or the environment; those override the file. `SLACK_CREDENTIALS_FILE`
   points at a different file.
4. **Event Subscriptions** → enable → bot events `app_mention`, `message.im`.
5. **Slash Commands** → create `/analyze` and `/fetch` (any description; with
   Socket Mode no Request URL is needed). Requires the `commands` scope.
6. **App Home** → enable the Messages tab ("Allow users to send Slash commands and messages from the messages tab").
7. Invite the bot to the channels where it should be mentioned or where the
   slash commands will be used (the command's anchor message is posted by the bot).

## Run

```bash
slack_bot/slack-bot setup            # .venv + requirements, copies .env.example -> .env
# tokens: ~/.config/slack/credentials.json (bot_token + app_token) or slack_bot/.env
slack_bot/slack-bot start            # background; `start --foreground` to watch
slack_bot/slack-bot status | logs | stop | restart
```

`start` first mirrors `skills/` → `.claude/skills/` and `agents/` →
`.claude/agents/` so Claude Code discovers them from the repo cwd (the
`.claude/` dir is git-ignored here).

The bot process needs `claude` on PATH and logged in (or `ANTHROPIC_*`
exported). Per-request Claude stderr goes to `slack_bot/out/claude_<thread>.log`;
the bot log is `slack_bot/out/app.log`; state is `slack_bot/.state/slack_bot.db`.

## Using it

| Message | What happens |
|---|---|
| `@bot help` | lists skills and usage |
| `@bot /trtllm-main-failures-analysis review test failures of case unittest/x.py::test_y` | runs that skill with the rest as the request (unique prefix ok: `/trtllm-main …`) |
| `@bot what's breaking on trtllm main in the last day?` | plain question; Claude Code picks a skill itself if one matches |
| reply in the thread | resumes the same Claude session (`--resume`) |
| reply `stop` | cancels the running request in that thread |
| `/fetch [days]` | runs `fetch_failures.py --days N` directly (default 7; windows snap to 1/2/7/30) and posts the triage table; JSON lands in `./<today>/` |
| `/analyze [days] [extra]` | runs the main-failures skill for the last N days (default 1, report-only); the report is posted in the thread **and DM'd to the requester** |

Slash commands post a visible anchor message (`:zap: @you ran /analyze 1`)
and use its thread for progress and the result, so follow-ups and `stop` work
the same way as for mentions. `/fetch help` or `/analyze help` prints usage.

One request per thread at a time; `SLACK_BOT_MAX_CONCURRENT` (default 2)
caps parallel Claude runs, extra requests wait and say so. A progress line is
edited every minute; the final reply is posted as mrkdwn chunks (tables are
fenced), or as a file when it is very long.

Publishing (Confluence / Sheets) and sending Slack DMs from inside a run only
happen when the message explicitly asks for them. The system prompt the bot
appends says so, and the skill's own rules agree.

## Permissions model

Headless runs use `--permission-mode acceptEdits` plus an `--allowedTools`
allow-list (`CLAUDE_ALLOWED_TOOLS`, default: Bash Read Write Edit Glob Grep
Agent Skill ToolSearch WebFetch WebSearch). Anything outside the list is
refused rather than prompted. Set `CLAUDE_PERMISSION_MODE=bypassPermissions`
only if you need that. `SLACK_BOT_ALLOWED_USERS` restricts who may trigger runs.
Slack tokens are blanked in the child environment; `CLAUDECODE` is removed so
the nested CLI starts.

## Tests

```bash
slack_bot/.venv/bin/python -m pytest slack_bot/tests -q
```
