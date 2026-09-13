# trtllm_pre_merge_analysis

Claude Code skill + agents for analyzing TensorRT-LLM main-branch CI breaks
("Main Break" detections on the internal trtllm-infra stability report), with
a per-run report, failure-type dispatch to handlers, optional publishing to a
Confluence case log / Google Sheet, and optional Slack notifications.

## Layout

```
.claude/
  skills/trtllm-main-failures-analysis/
    SKILL.md            # the workflow: Steps 0–8
    scripts/            # every access to the dashboard / ci_report / Confluence / Sheets / Slack
    evals/
  agents/
    ci-jenkins-log-navigator.md   # recovers failing case + full error from Jenkins/Blue Ocean/PBSS/JUnit artifacts
    ci-regression-verifier.md     # diff-level True/False verdict for one candidate PR/commit
    ci-failure-onset-bisector.md  # history of one case for one error signature: first build/PR/base commit, bounded range
```

Clone into (or copy `.claude/` into) the project directory where Claude Code
runs; the skill is then invoked automatically for questions like "what's
breaking on trtllm main", or explicitly with `/trtllm-main-failures-analysis`.

## Workflow (SKILL.md)

| Step | What |
|---|---|
| 0 / 0.5 | Known builds + ci-status watermark from the publish target(s) (incremental fetch window) |
| 1 | `fetch_failures.py --days N` (and `--include-stages`) — Main Break groups |
| 2 | Triage (confidence, waived, platform/file clustering) |
| 3 | `fetch_execution_details.py` — per-execution waive/bug/error from ci_report |
| 4 | Analyze: `fetch_full_error.py` (JUnit + `_pbss_log` + Blue Ocean), Method A infra signatures, Method B per-PR verification, Method C callstack ↔ commit history with `get_base_commit.py` validation; `analysis_<slug>.json` per entity |
| 5 | Dispatch each failure to a handler by failure type (infra / regression-fixed / regression-open / PR-own / flaky / unresolved) and record actions in `actions_<slug>.json` |
| 6 | Write the markdown report |
| 7 | Publish cases to Confluence and/or Google Sheet (only on request) |
| 8 | Slack DM (only on request; mrkdwn, one block per case, `post_slack_message.py --message-file`) |

Every run writes into a dated run directory (`./YYYY-MM-DD/`), which is
git-ignored here.

## Credentials (never committed)

- Confluence: `~/.config/confluence/credentials.json` — `{"email": ..., "api_token": ...}`
- Google Sheets: `~/.config/gsheets/apps_script.json` (preferred) or `service_account.json`, or re-scoped gcloud ADC
- Slack: `~/.config/slack/credentials.json` — `{"bot_token": "xoxb-..."}` (`chat:write`, `users:read.email`)

## Notes learned the hard way

- The dashboard's per-execution timestamps are US-Pacific (UTC−7); ci_report `job_info.ts_created` is UTC.
- Test-history is capped at 30 days regardless of the requested range.
- Bound regressions by each build's true `main` base (GitHub merge-base of the tested head), never by the PR's `baseRefOid` or by wall clock.
- Blue Ocean stage logs keep only the last ~200 lines and expire after ~a week; ci_report `_pbss_log` and the Artifactory JUnit archive survive longer.
- Stage-kind detections are investigated (log navigation) whenever a window has no test-kind breaks, a stage recurs, or it has no explaining comment.
