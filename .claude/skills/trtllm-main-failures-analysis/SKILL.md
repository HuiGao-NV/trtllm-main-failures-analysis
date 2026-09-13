---
name: trtllm-main-failures-analysis
description: Analyzes TensorRT-LLM (trtllm) main-branch CI failures using the internal trtllm-infra stability report dashboard's "Detection Details" / "Main Break" data, and produces a root-cause summary report. Use this whenever the user asks what's breaking or failing on trtllm main, wants a CI stability/health check for TensorRT-LLM, mentions the trtllm-infra stability report, ci-status page, "Main Break" detections, or wants to know why a specific trtllm test keeps failing on main — even if they don't explicitly ask for a "skill" or a "report".
---

# TRT-LLM Main Branch Failures Analysis

Analyzes CI breaks detected on the TensorRT-LLM main branch from the "Detection
Details" (Main Break) data at
`http://trtllm-infra.nvidia.com/trtllm-stability-report/ci-status`.

## Ground rules

- **Two hosts are only ever accessed through `scripts/`** — never curl/WebFetch/
  one-liner them, not even for a quick check: the stability dashboard
  (`trtllm-infra.nvidia.com/trtllm-stability-report`) and the ci_report site
  (`tensorrt-llm.tensorrt-llm-ci-report.sc2-paas.nvidia.com`). Both are SPAs
  whose data lives behind JSON APIs, and ci_report 503s under bursty load; the
  scripts pace and parse correctly. Jenkins/Blue Ocean, Artifactory, PBSS, NVDF
  links and GitHub may be hit directly (by you or a subagent). Confluence,
  Google Sheets and Slack also go through their scripts.
- **Key every per-entity data structure by `(entity_name, platform)`**, never
  by name alone — the same entity appears once per platform (follow
  `exec_key()` in `build_confluence_cases.py`).
- **Everything a run writes goes into `<run_dir> = ./<YYYY-MM-DD>/`**
  (`mkdir -p` it first). `<slug>` = `entity_name` with `/`, `::`, spaces → `_`.
  Write evidence to disk as it is produced (`full_error_*`, `verify_*`,
  `analysis_*`, `actions_*`), not only at report time.
- **Time zones:** dashboard per-execution `ts` values are US-Pacific (UTC−7);
  ci_report `job_info.ts_created` and GitHub commit dates are UTC. Convert before
  comparing to commits.
- **Dashboard limits:** `/api/incidents` accepts `days` ∈ {1, 2, 7, 30} only
  (`fetch_failures.py` snaps upward); test-history is capped at 30 days.
- Destinations live in `scripts/confluence_config.py`,
  `scripts/google_sheets_config.py`, `scripts/slack_config.py`.
- Subagents (`.claude/agents/`): `ci-jenkins-log-navigator` (recover failing
  case + full error from Jenkins/Blue Ocean/PBSS/JUnit), `ci-regression-verifier`
  (True/False verdict on one candidate PR/commit diff),
  `ci-failure-onset-bisector` (history of one case for one error signature).
  Invoke via the `Agent` tool with that `subagent_type`, not a fork.

## Scripts in workflow order

| Step | Script | Purpose |
|---|---|---|
| 0 | `fetch_confluence_known_builds.py`, `fetch_google_sheet_known_builds.py` | builds already recorded on a publish target (skip re-fetch) |
| 0.5 | `fetch_ci_status_watermark.py`, `sync_confluence_watermark.py`, `sync_sheet_watermark.py` | dashboard "Latest update" timestamp; read/write it on a target for incremental windows |
| 1 | `fetch_failures.py` | Main Break groups + waive status |
| 3 | `fetch_execution_details.py` | per-execution waive/bug/short error from ci_report |
| 4 | `fetch_full_error.py` | full error/callstack (ci_report `_pbss_log` + Blue Ocean log + JUnit archive) |
| 4 | `fetch_confluence_case_analysis.py`, `fetch_sheet_case_analysis.py` | recorded Failure Type + error signature per case (skip unchanged) |
| 4 | `get_base_commit.py` | true `main` base of each build (GitHub merge-base) and whether it predates a fix |
| 7 | `fetch_latest_status.py` | last-day pass streak per entity |
| 7 | `build_confluence_cases.py` | case JSON for both publish targets (all display formatting lives here) |
| 7 | `post_confluence_cases.py`, `post_google_sheet_cases.py` | publish/sync cases |
| 8 | `post_slack_message.py` | DM named Slack users |

`<skill_dir>` below = this skill's directory; all commands are
`python3 <skill_dir>/scripts/<name>.py …`.

## Step 0: Known builds (only when publishing)

```bash
fetch_confluence_known_builds.py --out <run_dir>/known_builds_confluence.json     # if Confluence is a target
fetch_google_sheet_known_builds.py --out <run_dir>/known_builds_sheet.json         # if the Sheet is a target
```
Pass each file to every later `fetch_execution_details.py` call as
`--known-builds-json` (repeatable; unions) so already-published builds are
skipped. Don't touch a target's API/credentials unless it is a publish target.
Skip this step entirely for report-only runs.

## Step 0.5: Fetch window from the watermark (only when publishing without an explicit window)

1. `fetch_ci_status_watermark.py --out <run_dir>/ci_status_watermark_now.json` —
   keep for write-back after publishing.
2. `sync_confluence_watermark.py --read --out …` / `sync_sheet_watermark.py --read --out …`
   for each publish target (ask which targets if not yet decided).
3. `--days` for Step 1: no watermark → 7; else `max(1, ceil(hours_since/24))`,
   using the **older** watermark when targets differ. An explicit user window
   ("last 30 days") always overrides.
4. After a successful publish to a target (Step 7), and only then:
   `sync_confluence_watermark.py --write <ts>` / `sync_sheet_watermark.py --write <ts>`
   (Sheet: write after `post_google_sheet_cases.py`).

Report-only run + explicit window → skip this step.

## Step 1: Fetch detections

```bash
fetch_failures.py --days <N> --out <run_dir>/trtllm-failures-<N>d-<date>.json
fetch_failures.py --days <N> --include-stages --out <run_dir>/trtllm-failures-<N>d-<date>-with-stages.json
```
Stdout is a triage table; the JSON has `waived` / `waive_bugs` per group already.
The default run excludes `entity_kind: "stage"` groups (no test-history); the
second run lists them. **Stage-kind rule:** investigate stage groups (Step 4
"Stage-kind entities") and run them through Step 5 whenever the window has
zero test-kind groups, the user asks about a stage, a stage recurs across
windows, or it has no explaining dashboard comment; otherwise list them with
PRs/window/comment and investigate only those Step 2 flags. "Not
investigated" is never the final state for a stage in an otherwise empty
window.

## Step 2: Triage (no network)

- High confidence first (>5 failing PRs, or a PostMerge failure with zero
  passes — see `confidence_reason`); then higher `failure_count` /
  `failure_pr_count` / `postmerge_failure_count`, lower `pass_rate`.
- Cluster by `(platform, test-file prefix)`; identical PR sets across many
  entities usually mean one shared incident. Pick one representative per
  cluster to investigate; roll the rest up.
- `waived: true` → list under "Waived / Already Tracked" (with `waive_bugs`)
  instead of fresh root-causing, unless the pattern looks new (other platform,
  far more frequent than the waive explains).
- Typically 10–20 entities get write-ups; the rest are table rows.

## Step 3: Execution details

```bash
fetch_execution_details.py --groups-json <picks.json> [--hours 720] \
  [--known-builds-json …]... --out <run_dir>/exec-<date>.json
```
Walks every FAILED execution (dashboard `test-detail`) and reads waive/bug/
`short_error_msg`/`log_link` from ci_report per build (deduped, paced — do not
raise `--job-workers` or lower `--job-min-interval`). Run it on the
representatives you will investigate, not all groups, unless publishing.
`--hours` should match the window (default 168). Stage-kind entities are
skipped. Jenkins/NVDF links in the output may be fetched directly.

## Step 4: Analyze

Apply the methods in order; the first confident hit ends the analysis for that
group. Write `<run_dir>/analysis_<slug>.json` as soon as a group concludes.

### 4.0 Stage-kind entities
Group stages sharing PR set + window (often one incident), then send each group
to `ci-jenkins-log-navigator` with stage name(s), platform, PRs, window, and the
group's `links.stage_dashboard` / `source_build_url` / `nvdf_document_url`
(note: those Jenkins links point at the CIHealthMonitor *detector*, not the
failing build). It must find the real builds (the orchestrator dispatches
stages as remote jobs on other Jenkins controllers), read the stage log/
artifacts, and return the failing case(s) + full error, and whether all PRs hit
the identical error. Output path `<run_dir>/full_error_<slug>.json`. Then treat
each recovered case as a normal case below.

### 4.1 Skip unchanged cases (only when publishing)
```bash
fetch_confluence_case_analysis.py --out <run_dir>/case_analysis_confluence.json
fetch_sheet_case_analysis.py --out <run_dir>/case_analysis_sheet.json
```
If a case's current error materially matches its recorded `signature` (ignore
build ids/timestamps; a different exception, file/line or symptom counts as a
change), reuse the recorded `failure_type`, write `analysis_<slug>.json` with
`"method": "skip-unchanged"`, and skip Methods A–C.

### 4.2 Always get the full error first
`short_error_msg` is truncated at the source and has led to a wrong attribution
before. For every analyzed group run, on one representative execution:
```bash
fetch_full_error.py --job <job> --build <build> --stage <stage> \
  --entity-name '<entity_name>' --out <run_dir>/full_error_<slug>.json
```
It merges (1) ci_report `_pbss_log` (falls back to the file-level record for a
`file::Class::test` entity; returns every block for a file-level entity — one
roll-up can hide several root causes), (2) the stage's Blue Ocean log
(`log_link`), and (3) the JUnit XML from the stage's Artifactory test-results
archive (downloaded into `<run_dir>/artifacts/`, reused across sub-tests;
`<failure>` text is appended to what 1–2 found). Use `blocks[].text` as the
evidence for Methods A–C.

If `blocks` is empty, fall back to `ci-jenkins-log-navigator` with the
execution's job/build/stage and the same `--out` path. Useful sources it knows:
ci_report `data.tests_by_stage[<stage>]` (`_pbss_log` = full pytest progress
log; per-test `s_short_error_msg`; survives Jenkins rotation), Blue Ocean node
logs (only the last ~200 lines of the inner run; 404 after ~a week), PBSS
per-test logs
(`https://pbss.s8k.io/v1/AUTH_svc_tensorrt/trtllm-ci-logs?prefix=<job path>/<build>/<stage>/inner/`).
For roll-ups, take the first failure in JUnit **execution order** as the
victim; setup errors after a device fault are cascade, not origin.

### 4.3 Standard opening line for every investigation subagent
```
test case [Case Name] meets error "[Error Message]". Check when this test
case is added. Check the commit history to analyze what does the failure
mean and how it happens? Try to find out if there is commit to fix this
failure.
```
`[Case Name]` = full `entity_name` (or stage name); `[Error Message]` = the
fullest text available (prefer `blocks[].text`). Append the method-specific
instructions after it. The three asks map to: test provenance
(`gh api "repos/NVIDIA/TensorRT-LLM/commits?path=<test file>&sha=main"`),
diff-grounded mechanism, and a fix-commit search after the window.

### Method A — infra signature
Error text matching **device error**, **failed to load weights**, **unable to
connect to node(s)**, **lost connection to node(s)**, SLURM submit/login-node
failures, apt/pip mirror errors (`Mirror sync in progress`, `File has unexpected
size`), stage killed with a test IN-FLIGHT for infra reasons → **Infra
failure**, stop. "Many unrelated tests on one platform in one window" is only a
supporting signal — confirm against the dashboard's outage data before
concluding infra from it. Never state a cause you have no evidence for.

### Method B — per-PR attribution
For every PR behind the group's recent failures, run `ci-regression-verifier`
(standard opening line + entity/platform, PR number, error evidence; it reads
`gh pr diff <n> --repo NVIDIA/TensorRT-LLM`) with output
`<run_dir>/verify_<slug>_pr<number>.json`. `True` → attributed to that PR
(a **PR-own defect**, not a main break, when only that PR fails). All `False`
→ Method C.

### Method C — callstack vs. main history
1. Search the exact symbol from the error first (`gh api
   "search/code?q=<symbol>+repo:NVIDIA/TensorRT-LLM"`, then
   `commits?path=<file>&sha=main&since=…&until=…`). Only without a symbol list
   all main commits between last-good and first-bad and narrow by touched files.
   Never attribute by title/timing alone.
2. **Bound the window by base commit, not wall clock:**
   ```bash
   get_base_commit.py --executions-json <exec json filtered to the group> \
     --fix-commit <candidate or any sha> --out <run_dir>/base_commits_<slug>.json
   ```
   Base = GitHub compare `merge_base_commit` of the tested head vs `main`
   (never the PR's `baseRefOid`). Last clean base → first failing base gives the
   commit range (`gh api repos/NVIDIA/TensorRT-LLM/compare/<a>...<b>`).
   PostMerge builds test `main` directly and pin the range best.
3. Verify the candidate with `ci-regression-verifier` (read the specific hunk,
   confirm the exact mechanism), output `<run_dir>/verify_<slug>_commit<sha>.json`.
4. **Validate any attributed fix**: every failing build must have
   `base_is_pre_fix: true`; any `false` means that build's failure has another
   cause — split it off and re-investigate rather than closing it out. Also
   check that no later build passed *without* the fix before crediting it.
5. Intermittent faults (same base → different victims; post-onset clean builds)
   weaken single-build boundaries: widen once, prefer lifetime/race changes for
   async device faults, and if nothing fits mechanically say so and hand back a
   ranked list for a hardware bisect / sanitizer run.

### Labels and `analysis_<slug>.json`
- **Infra failure** · **Regression (PR #N | commit sha)** · **PR-own defect**
  · **Flaky test** (mixed pass/fail, few PRs, no attribution) ·
  **Unresolved** (methods exhausted).
```json
{"entity_name": "...", "platform": "...", "method": "A | B | C | skip-unchanged",
 "category": "Infra failure | Regression (PR #123) | Regression (commit abc1234) | PR-own defect | Flaky test | Unresolved",
 "attributed_pr": null, "attributed_commit": null, "fix_commit": null,
 "reasoning": "<mechanism + evidence>", "confidence": "low | medium | high",
 "waived": false, "evidence_files": ["<run_dir>/full_error_<slug>.json", "..."]}
```
Only entities analyzed this run go into Step 7's `--failure-types-json`;
skipped ones keep their recorded type.

## Step 5: Dispatch to handlers and record actions

For every `analysis_<slug>.json`, pick the first matching handler and write
`<run_dir>/actions_<slug>.json` plus a consolidated
`<run_dir>/actions_<date>.json` (`{"results": [...]}`). Actions are proposals;
nothing outward-facing happens here.

| Handler | When | Record | Actions |
|---|---|---|---|
| `infra` | Infra failure | component, recovery (`fetch_latest_status.py` / later builds), INFRA-RETRY "no infra pattern matched" strings | `notify-infra`, `add-infra-retry-pattern <strings>`, `no-code-action` (never a waive) |
| `regression-fixed` | Regression with fix on main | fix commit/time, pre-fix base count | `rebase-affected-prs <PRs>`; post-fix-base failures → `split-off` back to Step 4 |
| `regression-open` | Regression, no fix | culprit, mechanism, blast radius | `notify-author` (sent only via Step 8), `propose-revert-or-fix`, `file-or-link-nvbug`, `propose-temporary-waive <waives.txt line>` for wide breaks |
| `pr-own-defect` | culprit is the failing PR itself | mechanism | `comment-on-pr`, `exclude-from-case-log` |
| `flaky` | Flaky test | counts, pattern, waive state | `link-nvbug`/`file-nvbug`, `propose-waive`, `request-owner-triage` |
| `unresolved` | Unattributed | bounded range, ranked candidates, intermittency | `hardware-bisect <range>`, `sanitizer-run <shard>`, `request-owner-triage`, `track-daily` |

An entity matching two handlers (e.g. fixed pre-fix failures + unattributed
post-fix ones) gets both action sets, each scoped to its build list.

```json
{"entity_name": "...", "platform": "...", "category": "...", "handler": "...",
 "actions": [{"action": "rebase-affected-prs", "prs": ["18614"], "note": "..."}],
 "owner": null, "status": "open | recovered | fixed-awaiting-rebase | waived | pr-own",
 "evidence_files": ["<run_dir>/analysis_<slug>.json"]}
```

## Step 6: Report

`<run_dir>/trtllm-main-failures-report-<date>.md`:
```markdown
# TRT-LLM Main Branch Failures Report — <window> (<start UTC> → <end UTC>)
Generated: <date>. Source: trtllm-infra stability report, Detection Details (Main Break). <run mode: published / report-only / Slack>

## Summary
- Total groups: N (H high / M medium) · stage-level S · waived W; breakdown by platform / cause
### Problem → case → PR → build map
**P1 — <problem>** · <cause / fix> — `<entity>` (platform) — #PR(build,…) … (carve out builds that belong to another problem)

## Top Offenders
| Entity | Platform | Confidence | Waived | Failures/Passes | PRs | Likely cause |

## Investigation Details
### <entity_name> (<platform>)
- Confidence / window / failure mode / mechanism (evidence files) / verify step / links

## Actions (from Step 5)
| Entity | Platform | Failure type | Handler | Actions | Owner | Status |

## PR-own defects (excluded from the case log)
## Waived / Already Tracked
| Entity | Platform | Bug |
## Open items carried forward
## Artifacts
```
Then give a short chat summary (headline, worst offenders, cross-cutting
issues) and the report path.

## Step 7: Publish cases (only if asked)

Targets: **Confluence** (`CONFLUENCE_PAGE_URL`; flat = one row per entity,
nested = one row per PR-per-build) and/or **Google Sheet** (`SPREADSHEET_URL`;
flat only). If no target is named, ask (don't default to Confluence); reuse a
preference already given in the conversation. Publish one entry per raw
`entity_name` — never merge related entities.

1. Known builds per target (Step 0 output, or fetch now).
2. `fetch_execution_details.py` on the **full** `fetch_failures.py` output with
   every `--known-builds-json` → incremental executions. Safe for flat mode
   (absent fields leave existing cells untouched). **Nested mode replaces the
   whole table** and refuses a known-builds-filtered input — re-run without the
   filter for nested. Nested + Sheet in one run → two `fetch_execution_details.py`
   runs.
3. `fetch_latest_status.py --groups-json <failures.json> --out <run_dir>/latest-status-<date>.json`
   (last 3 runs in 24 h all PASSED = recovered; drives Latest Status and resets
   the manual **Analyzed** flag to `"False"` only on `regressed_since_pass`;
   never write `"True"` yourself).
4. Build cases (`--mode nested` only for Confluence, only if the user wants
   per-execution rows — it is thousands of rows):
   ```bash
   build_confluence_cases.py --groups-json <failures.json> --executions-json <exec.json> \
     --status-json <status.json> --failure-types-json <run_dir>/failure_types.json \
     --mode flat --out <run_dir>/cases_<date>.json
   ```
   `failure_types.json` = `{"results": [{"entity_name", "platform", "failure_type"}]}`
   for entities analyzed this run only. Attach Step 5's primary action to each
   case's analysis text.
5. Publish, then write the watermark (Step 0.5 step 4):
   ```bash
   post_confluence_cases.py --cases-json <cases.json>      # flat   | --nested-json for nested
   sync_confluence_watermark.py --write <ts>
   post_google_sheet_cases.py --cases-json <cases.json>    # flat only
   sync_sheet_watermark.py --write <ts>
   ```
   Confluence flat: matches rows by (case, PR), **appends** date + analysis,
   **overwrites** status cells only when the field is present; migrates missing
   columns in place; nested rebuilds the table but carries Analyzed forward and
   dates each build row from its execution. Sheet: same merge semantics on a
   grid, one write. Credentials: Confluence `~/.config/confluence/credentials.json`
   (`{"email", "api_token"}`); Sheets — Apps Script webhook
   `~/.config/gsheets/apps_script.json` (`scripts/apps_script/Code.gs`; no GCP
   permissions needed — use when IAM blocks the others), or
   `~/.config/gsheets/service_account.json` (Editor on the sheet), or gcloud ADC
   re-scoped with `…/auth/spreadsheets` + `cloud-platform` and a quota project.
   A plain `gcloud auth` token lacks Sheets scope. If credentials are missing
   or `PERMISSION_DENIED` appears, tell the user the setup steps — don't retry
   variations.
6. **Confirm before the first publish to a target in the conversation, before
   any schema migration, and before any nested replace**; use `--dry-run` to
   show the exact row changes. Each target is confirmed independently.

## Step 8: Slack notification (only if asked)

No stored recipients: send only to people the user names (email or `U…` id,
`--user` repeatable, individual DMs). If no one is named and no owner is
identifiable from the dashboard comment, ask. Credentials:
`~/.config/slack/credentials.json` (`{"bot_token": "xoxb-…"}`, scopes
`chat:write`, `users:read.email`).

```bash
post_slack_message.py --user <email|Uxxx> [--user …] --message-file <run_dir>/slack_message_<date>.md --dry-run
post_slack_message.py --user <email|Uxxx> [--user …] --message-file <run_dir>/slack_message_<date>.md
```
Always write the message to a file and dry-run first (show recipient resolution
and text before the first real send in the conversation).

**Format — Slack mrkdwn, one block per case, one labelled field per line**
(`*bold*`, `_italic_`, `` `code` ``, `•` bullets, fenced block for error text;
**no headings, tables or `**`** — they render literally; escape `& < >` in error
text as `&amp; &lt; &gt;`; keep a DM under ~3,500 characters, ending with
`_… k more cases in the report_` if needed):
```
*TRT-LLM main CI — <window>* (<start UTC> → <end UTC>)
<N> case-level Main Break groups (<H> high / <M> medium) · <S> stage-level · <W> waived

*1. `<entity_name>`* (<platform>)
• *Waived:* Y (nvbugs/…) | N
• *Type:* <Step 4 label>
• *Reason:* <one sentence from analysis_<slug>.json>
• *PRs / builds:* #PR(build, …) …
• *Action:* <primary action from actions_<slug>.json>
• *Status:* open | recovered | fixed-awaiting-rebase | waived
• *Error:*
```<representative error, ≤ 3 lines>```

_Cross-cutting:_ <shared infra or root cause>
_Report:_ `<run_dir>/trtllm-main-failures-report-<date>.md`
```
Order blocks as the report does; in a window with no case-level entities use
the same layout for the investigated stage-level entities.

## Pitfalls (each cost a wrong conclusion before)

- Trusting `short_error_msg` or a roll-up's "culprit" string (often a cascade
  victim) instead of the full error and execution order.
- Attributing by PR title/timing instead of the symbol in the error and the
  actual diff hunk.
- Bounding a regression by wall clock or the PR's `baseRefOid` instead of each
  build's merge-base.
- Crediting a fix without checking that no later build passed without it, or
  ignoring builds whose base already contains it.
- Declaring "fixed" or "a new bug" when the first victim shifted, without
  comparing identical-base builds (intermittent faults rotate victims).
- Treating a hang (IN-FLIGHT kill) and a crash as different problems before
  checking they hit the same test.
- Reading the Blue Ocean tail as the whole log (200 lines, expires) or
  concluding a test "wasn't in the roll-up" from it.
- Leaving stage-kind detections "not investigated" in a window with nothing
  else.
- Mixing dashboard (UTC−7) and commit (UTC) times.
