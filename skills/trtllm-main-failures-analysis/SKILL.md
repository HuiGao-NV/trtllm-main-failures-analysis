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
- **Time zones:** dashboard per-execution `ts` values **and the detection
  window bounds** (`earliest_window_start`, `latest_observation.window`,
  `latest_failure_time`) are US-Pacific (UTC−7) even when suffixed `Z`;
  ci_report `job_info.ts_created` and GitHub commit dates are UTC. Convert
  before comparing to commits — a "04:00Z" detector bucket was really 11:00
  UTC and briefly looked earlier than the culprit merge. Bound onsets with
  `job_ts_created` from `fetch_execution_details.py`, never with detector
  windows.
- **Dashboard limits:** `/api/incidents` accepts `days` ∈ {1, 2, 7, 30} only
  (`fetch_failures.py` snaps upward); test-history is capped at 30 days.
- Destinations live in `scripts/confluence_config.py`,
  `scripts/google_sheets_config.py`, `scripts/slack_config.py`.
- Subagents (`.claude/agents/`): `ci-jenkins-log-navigator` (recover failing
  case + full error from Jenkins/Blue Ocean/PBSS/JUnit), `ci-regression-verifier`
  (True/False verdict on one candidate PR/commit diff),
  `ci-failure-onset-bisector` (history of one case for one error signature),
  `semantic-conflict-analyzer` (measures a two-PR semantic conflict and records
  it in `semantic_failures_stats.jsonl`).
  Invoke via the `Agent` tool with that `subagent_type`, not a fork.

## Scripts in workflow order

| Step | Script | Purpose |
|---|---|---|
| 1 | `fetch_confluence_known_builds.py`, `fetch_google_sheet_known_builds.py` | builds already recorded on a publish target (skip re-fetch) |
| 2 | `fetch_ci_status_watermark.py`, `sync_confluence_watermark.py`, `sync_sheet_watermark.py` | dashboard "Latest update" timestamp; read/write it on a target for incremental windows |
| 3 | `fetch_failures.py` | Main Break groups + waive status |
| 5 | `fetch_execution_details.py` | per-execution waive/bug/short error from ci_report |
| 6 | `fetch_full_error.py` | full error/callstack (ci_report `_pbss_log` + Blue Ocean log + JUnit archive) |
| 6 | `fetch_confluence_case_analysis.py`, `fetch_sheet_case_analysis.py` | recorded Failure Type + error signature per case (skip unchanged) |
| 6 | `get_base_commit.py` | true `main` base of each build (GitHub merge-base) and whether it predates a fix |
| — | `durations.py` | shared helper: `hours_between()`, `human()` → `x days x hours x minutes` (used by every script that reports a time gap) |
| 7 | `rebase_actions.py` | PRs on pre-fix bases (`base_is_pre_fix` from `get_base_commit.py`) + their GitHub authors → `rebase_actions_<date>.json` and the report/Slack "Rebase Action:" section |
| 7 | `semantic_failures_analysis.py` | used by the `semantic-conflict-analyzer` agent: early/late conflicting commits, late PR's last pre-merge build + base, time/commit gaps → `semantic_failures_stats.jsonl` |
| 6 | `harvest_pbss_metrics.py`, `metrics_by_base_commit.py` | Method D: per-build metric timeline from PBSS per-test logs (passing builds included), joined to base commits, with a commit-driven / time-driven verdict |
| 6 | `fetch_daily_pass_rate.py` | per-UTC-day pass/fail/error/skipped counts + pass rate for one or more cases, up to 30 days |
| 9 | `fetch_latest_status.py` | last-day pass streak per entity |
| 9 | `build_confluence_cases.py` | case JSON for both publish targets (all display formatting lives here) |
| 9 | `post_confluence_cases.py`, `post_google_sheet_cases.py` | publish/sync cases |
| 10 | `post_slack_message.py` | DM named Slack users |

`<skill_dir>` below = this skill's directory; all commands are
`python3 <skill_dir>/scripts/<name>.py …`.

## Step 1: Known builds (only when publishing)

```bash
fetch_confluence_known_builds.py --out <run_dir>/known_builds_confluence.json     # if Confluence is a target
fetch_google_sheet_known_builds.py --out <run_dir>/known_builds_sheet.json         # if the Sheet is a target
```
Pass each file to every later `fetch_execution_details.py` call as
`--known-builds-json` (repeatable; unions) so already-published builds are
skipped. Don't touch a target's API/credentials unless it is a publish target.
Skip this step entirely for report-only runs.

## Step 2: Fetch window from the watermark (only when publishing without an explicit window)

1. `fetch_ci_status_watermark.py --out <run_dir>/ci_status_watermark_now.json` —
   keep for write-back after publishing.
2. `sync_confluence_watermark.py --read --out …` / `sync_sheet_watermark.py --read --out …`
   for each publish target (ask which targets if not yet decided).
3. `--days` for Step 3: no watermark → 7; else `max(1, ceil(hours_since/24))`,
   using the **older** watermark when targets differ. An explicit user window
   ("last 30 days") always overrides.
4. After a successful publish to a target (Step 9), and only then:
   `sync_confluence_watermark.py --write <ts>` / `sync_sheet_watermark.py --write <ts>`
   (Sheet: write after `post_google_sheet_cases.py`).

Report-only run + explicit window → skip this step.

## Step 3: Fetch detections

```bash
fetch_failures.py --days <N> --out <run_dir>/trtllm-failures-<N>d-<date>.json
fetch_failures.py --days <N> --include-stages --out <run_dir>/trtllm-failures-<N>d-<date>-with-stages.json
```
Stdout is a triage table; the JSON has `waived` / `waive_bugs` per group already.
The default run excludes `entity_kind: "stage"` groups (no test-history); the
second run lists them. **Stage-kind rule:** investigate stage groups (Step 6
"Stage-kind entities") and run them through Step 7 whenever the window has
zero test-kind groups, the user asks about a stage, a stage recurs across
windows, or it has no explaining dashboard comment; otherwise list them with
PRs/window/comment and investigate only those Step 4 flags. "Not
investigated" is never the final state for a stage in an otherwise empty
window.

## Step 4: Triage (no network)

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

## Step 5: Execution details

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

## Step 6: Analyze

Apply the methods in order; the first confident hit ends the analysis for that
group. For threshold/value failures run Method D before crediting any Method C
candidate — a range bounded from failures alone has been wrong before. Write `<run_dir>/analysis_<slug>.json` as soon as a group concludes.

### 6.0 Stage-kind entities
Group stages sharing PR set + window (often one incident), then send each group
to `ci-jenkins-log-navigator` with stage name(s), platform, PRs, window, and the
group's `links.stage_dashboard` / `source_build_url` / `nvdf_document_url`
(note: those Jenkins links point at the CIHealthMonitor *detector*, not the
failing build). It must find the real builds (the orchestrator dispatches
stages as remote jobs on other Jenkins controllers), read the stage log/
artifacts, and return the failing case(s) + full error, and whether all PRs hit
the identical error. Output path `<run_dir>/full_error_<slug>.json`. Then treat
each recovered case as a normal case below.

### 6.1 Skip unchanged cases (only when publishing)
```bash
fetch_confluence_case_analysis.py --out <run_dir>/case_analysis_confluence.json
fetch_sheet_case_analysis.py --out <run_dir>/case_analysis_sheet.json
```
If a case's current error materially matches its recorded `signature` (ignore
build ids/timestamps; a different exception, file/line or symptom counts as a
change), reuse the recorded `failure_type`, write `analysis_<slug>.json` with
`"method": "skip-unchanged"`, and skip Methods A–C.

### 6.2 Always get the full error first
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

### 6.3 Standard opening line for every investigation subagent
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

### 6.4 Daily pass-rate history for a case
Use whenever someone asks to "review the history of case `<X>`", wants its
day-by-day pass rate, or Method C/D's bounded range needs an independent
day-level view of pass/fail (not just PR groupings) to sanity-check an onset:
```bash
fetch_daily_pass_rate.py --entity '<entity_name>' [--entity '<entity_name2>' ...] \
  --hours 720 --out <run_dir>/daily_pass_rate_<slug>.json \
  --md-out <run_dir>/daily_pass_rate_<slug>.md
```
`--entity` reports every platform seen for that name plus an `ALL` combined
row; use `--entity-platform NAME PLATFORM` (repeatable) to restrict to one.
`NAME` must be the dashboard's own `entity_name` form (file path, `file::Class::test`,
or bare `file.py::test`) — **not** a shorthand like `test_x[a|b-...]` for two
separate parametrizations; verify the real id(s) first (`gh api
"search/code?q=<test function name>+repo:NVIDIA/TensorRT-LLM"`, then read the
`@pytest.mark.parametrize` ids, or grep `tests/integration/test_lists/waives.txt`
/ the QA lists for the exact bracketed id) — a wrong/combined name silently
returns 0 hits rather than erroring. `--hours` caps at 720 (30 days, the
dashboard's own cap). Zero-executed days (all `SKIPPED`, e.g. from a waive)
report `pass_rate: null`, not `0.0` — read that as "no signal", not "100% failing".

A day-by-day table that goes from 100% straight to `null`/all-skipped is the
signature of a waive landing, not a recovery: cross-check by finding when the
SKIP line was added to `waives.txt` (`gh api
"repos/NVIDIA/TensorRT-LLM/commits?path=tests/integration/test_lists/waives.txt&sha=main&since=<onset>&until=<onset+2d>"`,
then grep each candidate commit's patch for the entity's bracketed id) — the
PR/commit that added it usually names the nvbug and the triggering build.

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

### Method D — base-vs-time separation (dense value timeline)
Use when the failure is a **value crossing a threshold** (accuracy, perf, timeout
margin) or whenever Method C does not close cleanly: the bounded range contains
no mechanically plausible commit, identical bases give different outcomes, a
retry of the same wheel passes, or the "onset" was inferred from failures only.
The dashboard's test-history lists failures only; the passing builds carry the
signal that separates a commit from an environment change, and PBSS keeps every
per-test `stdout.log` for PR and post-merge builds
(`main/L0_MergeRequest_PR/<build>/<stage>/…/stdout.log`, `main/L0_PostMerge/<build>/…`).
1. Harvest the metric for **every** build that ran the stage in the window (both
   jobs; a wide build range is cheap — builds without the stage are listed as
   `<no-stage>`):
   ```bash
   harvest_pbss_metrics.py --job L0_MergeRequest_PR --builds <first>-<last> \
     --job L0_PostMerge --builds <first>-<last> --stage-substring <stage> \
     --test-regex '<test id fragment with / :: [ ] as _>' \
     [--metric NAME=REGEX]... --log-dir <run_dir>/pbss --out <run_dir>/metrics_<slug>.tsv
   ```
   Defaults extract lm-eval accuracies (`gpqa`, `gsm8k`, `mmlu`, `Evaluated accuracy`,
   `but got N`). One row per attempt: a stage retry yields two rows for one build.
2. Resolve bases for every harvested build with the **full** job names
   (`--job LLM/main/L0_MergeRequest_PR --build N` … and `--job LLM/main/L0_PostMerge
   --build N`; post-merge builds report the tested `main` commit as `head_commit`).
   Bash arrays, not an unquoted string, for the repeated flags (zsh does not
   word-split).
3. Join and judge:
   ```bash
   metrics_by_base_commit.py --metrics-tsv <run_dir>/metrics_<slug>.tsv \
     --base-json <run_dir>/base_commits_pr.json --base-json <run_dir>/base_commits_pm.json \
     --test-regex '<same fragment>' --metric gpqa --threshold <threshold> \
     --out <run_dir>/metrics_by_base_<slug>
   ```
   It prints the same-base/different-day groups, the per-day histogram, the
   threshold interleave test and a **VERDICT**:
   - **commit-driven** — every base has one value and the failing value starts
     at one base and persists → take the adjacent-base step it prints into
     Method C step 3 (verify the commits in that step's compare range).
   - **time-driven** — the same base scores differently on different days and
     failing values sit on bases both older and newer than passing bases → **do
     not attribute to a `main` commit**. Label **Flaky test** (environment
     drift) and, for the report, compare run-time inputs between the last
     passing and first failing run: stage Blue Ocean log (image tag, driver,
     `Successfully installed` lines, HF-cache rsync, node), per-test env dump,
     test order inside the stage container (`TLLM_AUTOTUNER_CACHE_PATH` is
     container-local per attempt), and the **wheel provenance** — a
     `[Build TRT-LLM] Reuse` stage copies an older build's tarball, so the wheel
     can predate the build by days (check `Build-x86_64` for `reuseArtifactPath`).
   - **mixed** — bound the base-ordered step with Method C, then confirm the
     step survives on other days before crediting a commit.
   Sibling params of the same test (other parametrizations in the stage) are
   harvested for free and often move in the same windows; use them as a check.
4. Record `"method": "D"` in `analysis_<slug>.json` with the verdict, the
   `metrics_by_base_<slug>.md` path, and the per-day histogram in `reasoning`.

### Labels and `analysis_<slug>.json`
- **Infra failure** · **Regression (PR #N | commit sha)** · **Semantic conflict
  (early sha × late sha)** (see Step 7) · **Test leak (commit sha)** (see
  Step 7) · **PR-own defect** · **Flaky test** (mixed pass/fail, few PRs, no
  attribution) · **Unresolved** (methods exhausted).
```json
{"entity_name": "...", "platform": "...", "method": "A | B | C | D | skip-unchanged",
 "category": "Infra failure | Regression (PR #123) | Regression (commit abc1234) | Semantic conflict (early abc1234 x late def5678) | Test leak (commit abc1234) | PR-own defect | Flaky test | Unresolved",
 "attributed_pr": null, "attributed_commit": null, "fix_commit": null,
 "reasoning": "<mechanism + evidence>", "confidence": "low | medium | high",
 "waived": false, "evidence_files": ["<run_dir>/full_error_<slug>.json", "..."]}
```
Only entities analyzed this run go into Step 9's `--failure-types-json`;
skipped ones keep their recorded type.

## Step 7: Dispatch to handlers and record actions

For every `analysis_<slug>.json`, pick the first matching handler and write
`<run_dir>/actions_<slug>.json` plus a consolidated
`<run_dir>/actions_<date>.json` (`{"results": [...]}`). Actions are proposals;
nothing outward-facing happens here.

| Handler | When | Record | Actions |
|---|---|---|---|
| `infra` | Infra failure | component, recovery (`fetch_latest_status.py` / later builds), INFRA-RETRY "no infra pattern matched" strings | `notify-infra` (recipient = `INFRA_TEAM_RECIPIENT` placeholder from `slack_config.py`; **no PR/author lookup** — infra failures have no culprit PR), `add-infra-retry-pattern <strings>`, `no-code-action` (never a waive) |
| `regression-fixed` | Regression with fix on main | fix commit/time, pre-fix base count, `rebase_actions_<date>.json` | `rebase-affected-prs <PRs>` (PR list + authors via `rebase_actions.py`, below); post-fix-base failures → `split-off` back to Step 6 |
| `regression-open` | Regression, no fix | culprit, mechanism, `blast_radius_groups` | per the blast-radius rule below: > 3 groups → `find-culprit-and-revert <commit>`; 1–2 groups → `propose-waive <waives.txt line>` + `file-or-link-nvbug`; plus `notify-author` (sent only via Step 10) |
| `pr-own-defect` | culprit is the failing PR itself | mechanism | `comment-on-pr`, `exclude-from-case-log` |
| `test_leak` | single introducing commit, its own pre-merge CI didn't run/block on this test (see Step 7 "Test leak") | introducing commit, whether the PR's own build ran this test and what happened, fix commit if any | same as `regression-fixed`/`regression-open` (rebase or revert per blast radius) **plus** `flag-ci-gating-gap <test>` noting the coverage hole so it doesn't recur |
| `flaky` | Flaky test | counts, pattern, waive state | `link-nvbug`/`file-nvbug`, `propose-waive`, `request-owner-triage` |
| `unresolved` | Unattributed | bounded range, ranked candidates, intermittency | `hardware-bisect <range>`, `sanitizer-run <shard>`, `request-owner-triage`, `track-daily` |

An entity matching two handlers (e.g. fixed pre-fix failures + unattributed
post-fix ones) gets both action sets, each scoped to its build list.

**Blast-radius rule (every non-`infra` handler).** Count the test groups
(`(entity_name, platform)` pairs) that share the same failure — same
signature/mechanism, same culprit or same bounded range — across the whole
window, and record it as `blast_radius_groups`:
- **> 3 groups** → the failure is wide; the primary action is
  `find-culprit-and-revert <commit>`: pin the introducing commit (Method
  B/C, or `ci-failure-onset-bisector` if the onset isn't known yet) and
  propose reverting it. A waive is only a stop-gap here (`propose-temporary-waive`
  stays secondary) — with more than 3 groups broken, waiving hides the break
  instead of fixing it. If the culprit can't be pinned, the action is
  `bisect-then-revert <range>` — still not a waive.
- **1–2 groups** → the primary action is `propose-waive <waives.txt line>` +
  `file-nvbug` (or `link-nvbug` if one already exists) so the case is tracked
  while the owner fixes it; `notify-author` if the culprit is known.
- **Exactly 3 groups** → judgment call; default to the waive+nvbug path
  unless the groups are on different platforms (a cross-platform break is
  treated as wide).
Groups already fixed on `main` (`regression-fixed`) still get counted so the
report states the true blast radius, but their action stays
`rebase-affected-prs`.

```json
{"entity_name": "...", "platform": "...", "category": "...", "handler": "...",
 "blast_radius_groups": 5,
 "actions": [{"action": "find-culprit-and-revert", "commit": "8fff903d8a58", "pr": "18990", "note": "..."},
             {"action": "rebase-affected-prs", "prs": ["18614"], "note": "..."}],
 "owner": null, "status": "open | recovered | fixed-awaiting-rebase | waived | pr-own",
 "evidence_files": ["<run_dir>/analysis_<slug>.json"]}
```


**Rebase list (every `rebase-affected-prs` action).** Do not type the PR
list by hand: a PR needs a rebase exactly when one of its failing builds ran
on a `main` base that predates the fix (`base_is_pre_fix: true` in the
`get_base_commit.py --fix-commit <fix>` output). Run
```bash
rebase_actions.py --base-commits-json <run_dir>/base_commits_<date>.json [--base-commits-json …] \
  [--actions-json <run_dir>/actions_<date>.json] [--pr <n> …] --fix-commit <fix sha> \
  --entity "<Pn short problem label>" --out <run_dir>/rebase_actions_<date>.json \
  --md-out <run_dir>/rebase_actions_<date>.md --slack-out <run_dir>/rebase_actions_slack_<date>.md
```
once per fix commit (a window with two fixed problems → two files, suffix the
label). It looks up each PR's author (`gh api repos/<repo>/pulls/<n>` login +
display name), drops PRs already merged/closed into `skipped`, and writes
`{"fix_commit", "prs": [{"pr", "author", "author_name", "title", "state",
"builds", "base_commits", "url"}], "skipped", "authors": {login: [prs]}}`.
Put the resulting `prs` numbers into the action's `"prs"` list and the file
into `evidence_files`; the `.md` / Slack snippets feed Step 8 and Step 10
verbatim. Only `regression-fixed` groups get a rebase list — while the fix is
still open (`regression-open`), record `rebase-affected-prs` with the note
"once fixed: N PRs" and no author lookup. `infra` groups never get one:
their `notify-infra` action carries `"recipient": "<INFRA_TEAM_RECIPIENT>"`
(the placeholder in `slack_config.py`), not a PR author — do not run
`rebase_actions.py` or `gh api …/pulls/<n>` for an infra failure.

**Semantic conflicts.** A *semantic conflict* is defined purely behaviorally, not
by what kind of change either side made: **PR A merged first and, alone, did not
cause this test to fail; PR B merged later and, alone, also did not cause this
test to fail; but once both are on `main` together, some logic conflict between
them makes the test fail.** Neither PR individually reproduces the break —
verify this before calling it a semantic conflict (check each PR's own
pre-merge CI for this test, or its base commit against just one side). It does
NOT require one side to be an "interface change" and the other a "stale
consumer" — any two-sided logic incompatibility qualifies (fixtures, ordering,
resource contention, config defaults, anything), as long as each side is
independently clean. If a *single* commit's own pre-merge build already
reproduces the failure (that PR alone is red), this is not a semantic conflict
— see **test_leak** below instead.

When a `regression-*` group is a genuine two-PR semantic conflict, delegate the
measurement to the `semantic-conflict-analyzer` agent (entities, the two
commits, fix, `RUN_DIR`, label). It writes `RUN_DIR/semantic_<label>.{json,md}`
and appends the record to `./semantic_failures_stats.jsonl`; quote its gap table
in the investigation section.

**Test leak.** Use category `test_leak` instead of a plain `Regression (commit
…)` when a *single* introducing commit is responsible (no second PR needed to
reproduce it), but that commit's own pre-merge CI did not run this test case
against its true final head before merging — the stage wasn't scheduled on the
final head, the test was treated as non-blocking/out of gate scope, or a
failure was observed but merged anyway. The defect existed at merge time and
was verifiable, but the gate let it leak onto `main` regardless — that gap in
pre-merge verification is the reportable finding, distinct from an ordinary
regression where the introducing PR's own CI was clean. Confirm with
`get_base_commit.py`/`ci-jenkins-log-navigator` that the introducing PR's own
build for this test either didn't run on the final head or ran and failed
without blocking the merge, and say which in `reasoning`.

## Step 8: Report

**Durations.** Every time difference that reaches a report or a record (onset
gap, bounded range span, base staleness, time-to-merge, time-to-fix) is written
as `x days x hours x minutes` (`scripts/durations.py: human()` /
`duration_between()`); keep numeric hours only as an extra field for statistics.

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

## Actions (from Step 7)
| Entity | Platform | Failure type | Handler | Blast radius (groups) | Actions | Owner | Status |

## Rebase Action:
<contents of rebase_actions_<date>.md, one block per fix commit: intro line
naming the fix and problem, then>
| PR | Author | Title | Failing builds |
By author:
- @<login>: #PR #PR …
(omit the section only when no group is `regression-fixed`; write `_None._`
if the handler fired but every affected PR is already merged/closed)

## PR-own defects (excluded from the case log)
## Waived / Already Tracked
| Entity | Platform | Bug |
## Open items carried forward
## Artifacts
```
Then give a short chat summary (headline, worst offenders, cross-cutting
issues) and the report path.

## Step 9: Publish cases (only if asked)

Targets: **Confluence** (`CONFLUENCE_PAGE_URL`) and/or **Google Sheet**
(`SPREADSHEET_URL`; flat only). If no target is named, ask (don't default to
Confluence); reuse a preference already given in the conversation. Publish one
entry per raw `entity_name` — never merge related entities.

**Confluence layout = nested (default).** One case → its PR list → each PR's
build list. Case-level cells (rowspan): `Case name`, `Waived (latest main)`
(waive state in the current `waives.txt`, from `fetch_failures.py`),
`Latest Status`. PR-level: `PR number`. Per build: `Build`, `Triggered (UTC)`
(ci_report `ts_created`; dashboard `ts`+7 h as fallback), `Base commit`
(merge-base of the tested head, from `get_base_commit.py`), `Error / callstack`
(first `fetch_full_error.py` block when one exists, else the ci_report short
message), `Waived at run`, `Bug`, `Analyzed`, `Failure analysis`, `Failure type`.
Publishing **merges build by build**: new case/PR/build rows are added, known
builds get their refreshable cells updated, rows only on the page are kept,
`Analyzed` is sticky (reset only on a detected pass→fail regression or an
explicit per-build value). The legacy flat layout (`--cases-json`) remains for
the Sheet and for a page that still carries the flat table.

1. Known builds per target (Step 1 output, or fetch now) — the nested reader
   takes them from the `Build` column.
2. `fetch_execution_details.py` on the **full** `fetch_failures.py` output with
   every `--known-builds-json` → incremental executions (already-published
   builds are skipped; the nested merge keeps their rows). Each execution now
   carries `job_ts_created` and `head_commit` from ci_report.
3. `fetch_latest_status.py --groups-json <failures.json> --out <run_dir>/latest-status-<date>.json`
   (last 3 runs in 24 h all PASSED = recovered; drives Latest Status and marks
   `reset_analyzed` only on `regressed_since_pass`; never write `"True"` yourself).
4. `get_base_commit.py --executions-json <exec.json> --fix-commit <any sha> --out <run_dir>/base_commits_<date>.json`
   for every build in the exec JSON (skip only if the run has no new builds).
5. Build the nested cases. If `fetch_execution_details.py` skipped every
   build of a case (nothing new), the builder still emits one placeholder row
   per PR (`build: "n/a"`, error `N/A (no execution data)`); strip those rows
   before publishing (keep the case entry so `Latest Status` / `Waived` still
   refresh — `merge_nested` accepts a case with an empty `prs` list):
   ```bash
   build_confluence_cases.py --groups-json <failures.json> --executions-json <exec.json> \
     --status-json <status.json> --failure-types-json <run_dir>/failure_types.json \
     --base-commits-json <run_dir>/base_commits_<date>.json --full-errors-dir <run_dir> \
     --mode nested --date <date> --out <run_dir>/cases_nested_<date>.json
   ```
   `failure_types.json` = `{"results": [{"entity_name", "platform", "failure_type",
   "analysis", "build" (optional), "analyzed" (optional)}]}` for entities analyzed
   this run: an entry without `build` applies to every build of the entity; an
   entry with `build` overrides that build only (use it when builds of one case
   have different causes, e.g. stale-base vs. new failure). `analysis` = the
   Step 6 conclusion from error message/callstack + commit history, ending
   with Step 7's primary action. Flat mode is unchanged
   (`--mode flat --out cases_<date>.json`).
6. Publish, then write the watermark (Step 2 step 4):
   ```bash
   post_confluence_cases.py --nested-json <cases_nested.json>   # merge; --replace only to convert a flat page
   sync_confluence_watermark.py --write <ts>
   post_google_sheet_cases.py --cases-json <cases_flat.json>    # flat only
   sync_sheet_watermark.py --write <ts>
   ```
   Confluence nested refuses to merge into a page whose table is not in the
   nested layout; converting the page (`--replace`) discards the old table and
   needs explicit confirmation. Flat: matches rows by (case, PR), **appends**
   date + analysis, **overwrites** status cells only when the field is present;
   migrates missing columns in place. Sheet: same merge semantics on a grid,
   one write. Credentials: Confluence `~/.config/confluence/credentials.json`
   (`{"email", "api_token"}`); Sheets — Apps Script webhook
   `~/.config/gsheets/apps_script.json` (`scripts/apps_script/Code.gs`; no GCP
   permissions needed — use when IAM blocks the others), or
   `~/.config/gsheets/service_account.json` (Editor on the sheet), or gcloud ADC
   re-scoped with `…/auth/spreadsheets` + `cloud-platform` and a quota project.
   A plain `gcloud auth` token lacks Sheets scope. If credentials are missing
   or `PERMISSION_DENIED` appears, tell the user the setup steps — don't retry
   variations.
7. **Confirm before the first publish to a target in the conversation, before
   any schema migration, and before any `--replace` (flat→nested conversion)**; use `--dry-run` to
   show the exact row changes. Each target is confirmed independently.

## Step 10: Slack notification (only if asked)

No stored recipients for case DMs: send only to people the user names
(email or `U…` id, `--user` repeatable, individual DMs). If no one is named
and no owner is identifiable from the dashboard comment, ask. The one
exception is `notify-infra`: its recipient is the infra-team placeholder
`INFRA_TEAM_RECIPIENT` in `scripts/slack_config.py`, never a PR author —
while it is still the literal placeholder, write the infra notification
to `<run_dir>/slack_infra_<date>.md`, show it, and tell the user the
placeholder must be filled in before it can be sent. Credentials:
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
• *Type:* <Step 6 label>
• *Reason:* <one sentence from analysis_<slug>.json>
• *PRs / builds:* #PR(build, …) …
• *Action:* <action 1>; <action 2>; …
• *Status:* open | recovered | fixed-awaiting-rebase | waived
• *Error:*
```<representative error, ≤ 3 lines>```

*Rebase Action:* rebase past `<fix sha>` — <problem label>
• <#PR link> — @<author login>
• …

_Cross-cutting:_ <shared infra or root cause>
_Report:_ `<run_dir>/trtllm-main-failures-report-<date>.md`
```
Order blocks as the report does; in a window with no case-level entities use
the same layout for the investigated stage-level entities.

Field rules:
- **PRs / builds** — which CI runs the failure was seen in: each failing PR
  number (`#19198`) followed, in parentheses, by the `L0_MergeRequest_PR`
  orchestrator build numbers of that PR that hit it (`#19198(60720, 60797)`),
  then post-merge builds as `post-merge 2965`. Entries are separated by
  spaces. It is the evidence set for the block, not the PRs' fault — PRs are
  listed because they were broken *by* main. Long lists (> ~8 PRs) collapse
  to a count plus `(list in report)`; builds added since the previous run
  may be prefixed `new`.
- **Rebase Action** — paste `rebase_actions_slack_<date>.md` from Step 7 (one
  block per fix commit) after the case blocks; every open PR with its author's
  GitHub login, one per `•` line, so the recipient can ping people directly.
  Omit the block when no group is `regression-fixed`; it counts toward the
  ~3,500-character budget, so collapse to `N PRs / M authors (list in report)`
  plus the by-author lines if the DM would overflow.
- **Action** — every action from `actions_<slug>.json` in priority order,
  joined with `; ` (`file-nvbug; propose-waive full:…; notify-author #19108`).
  Never join actions with `+`, `&`, `and` or commas; one action per `;`
  segment so the line can be split mechanically.

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
- Bounding an accuracy/threshold onset from failing builds only: the passing
  builds' values (PBSS per-test logs, Method D) showed the same `main` base
  scoring 67.68, 60.10 and 54.55 on three consecutive days — no commit was
  responsible, and two earlier attributions had to be withdrawn.
- Assuming a build's wheel was compiled from its own base at build time: the
  `[Build TRT-LLM] Reuse` stage copies an earlier build's tarball
  (`reuseArtifactPath`), so build-time inputs must be read from the producing
  `Build-x86_64` run.
- Treating a stage retry that passes as proof of a fix: the retry runs alone in
  a fresh container (different autotuner-cache/test-order state) and on another
  node; compare it with same-day, same-base first attempts.
