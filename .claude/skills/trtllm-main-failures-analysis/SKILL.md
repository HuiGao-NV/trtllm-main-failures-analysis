---
name: trtllm-main-failures-analysis
description: Analyzes TensorRT-LLM (trtllm) main-branch CI failures using the internal trtllm-infra stability report dashboard's "Detection Details" / "Main Break" data, and produces a root-cause summary report. Use this whenever the user asks what's breaking or failing on trtllm main, wants a CI stability/health check for TensorRT-LLM, mentions the trtllm-infra stability report, ci-status page, "Main Break" detections, or wants to know why a specific trtllm test keeps failing on main — even if they don't explicitly ask for a "skill" or a "report".
---

# TRT-LLM Main Branch Failures Analysis

Analyzes CI breaks detected on the TensorRT-LLM main branch, using the
"Detection Details" (Main Break) data from the internal stability dashboard
at `http://trtllm-infra.nvidia.com/trtllm-stability-report/ci-status`.

## Division of labor: this file is workflow only

Two hosts are involved and **all access to them, and all parsing of what
they return, lives in `scripts/`** — never fetch or parse them ad hoc (no
inline curl/WebFetch/Python one-liners against these two hosts, even for a
"quick" one-off check):
- `trtllm-infra.nvidia.com/trtllm-stability-report` (the stability dashboard
  and its JSON APIs — including `scripts/fetch_ci_status_watermark.py`, which
  reads the dashboard's own "Latest update" timestamp)
- `tensorrt-llm.tensorrt-llm-ci-report.sc2-paas.nvidia.com` (the ci_report
  site and its per-build JSON API)

This is deliberate, not just tidiness: both are internal SPAs whose real
data lives behind JSON APIs, not the static HTML (see each script's
docstring for the specific endpoints and why); one of them (ci_report) has
been observed to 503 under bursty concurrent load, so the fetch scripts
pace their requests carefully — logic that would be lost and re-broken if
reimplemented ad hoc each run. Your job in this skill is to run the right
script in the right order, feed its output to the next step, and make
judgment calls (triage, root-cause hypotheses, report writing) on data the
scripts already fetched — not to talk to these hosts directly.

Jenkins console URLs, the NVDF/OpenSearch document links, the Confluence
API, and the Google Sheets API are different hosts and not covered by this
constraint — WebFetch/curl the first two directly as needed in Steps 3-4
(including via a subagent, e.g. Step 4's "get the full error
message/callstack" step, which navigates Jenkins directly); the latter two
already have dedicated scripts (Steps 0 and 6) for the same reason the two
main hosts do (consistent parsing/sync logic, not ad hoc each run).

The scripts, in the order the workflow uses them:
0. `scripts/fetch_confluence_known_builds.py` / `scripts/fetch_google_sheet_known_builds.py`
   — build ids already recorded on whichever case-log target(s) are in play,
   so later steps can skip them.
0.5. `scripts/fetch_ci_status_watermark.py` — the dashboard's own current
   "Latest update" timestamp. `scripts/sync_confluence_watermark.py` /
   `scripts/sync_sheet_watermark.py` — read (and, after publishing, write)
   that same timestamp as a standalone field on whichever case-log target(s)
   are in play, so Step 1's fetch window can be incremental instead of always
   a fixed lookback.
1. `scripts/fetch_failures.py` — Main Break detections + waive status.
2. `scripts/fetch_execution_details.py` — per-execution waive/bug/error-message
   detail (only needed for deep investigation or the case log).
3. `scripts/fetch_latest_status.py` — last-day recovery check per entity
   (only needed for the case log; see Step 7).
3.5. `scripts/fetch_confluence_case_analysis.py` / `scripts/fetch_sheet_case_analysis.py`
   — each case's already-recorded Failure Type plus the error-message
   signature it was based on, so Step 4 can skip re-analyzing a case whose
   current error text hasn't changed since it was last analyzed.
3.7. `scripts/get_base_commit.py` — per build, the actual commit `main` was
   at when the tested PR branch diverged (via GitHub's compare API, not the
   PR's own `baseRefOid`/`base.sha` — see the script's docstring for why
   that field is unsafe here), and optionally whether that predates a given
   fix commit. Used by Step 4 Method C to validate an attributed fix commit
   against every build a failure group actually ran on.
4. `scripts/build_confluence_cases.py` — turns the outputs of 2 and 3 into
   the case JSON both publish scripts consume. All the display-formatting
   rules (how a `waived` bool becomes `"Y (nvbugs/...)"`, how executions
   group by PR/build, how a recovery streak becomes `"Passed (3/3 recent
   runs)"`, etc.) live here, not in this file or in ad hoc conversation
   code. (The name predates the Google Sheet target; it builds the case
   data both scripts publish, not just Confluence's.)
5. `scripts/post_confluence_cases.py` / `scripts/post_google_sheet_cases.py`
   — publish/sync the case JSON to Confluence and/or the Google Sheet, per
   whichever target(s) the user asked for (Step 7). The Sheet script reuses
   the Confluence script's header/field constants directly (imported, not
   copied) so the two targets can't drift apart.
6. `scripts/post_slack_message.py` — DM one or more Slack users, only when
   the user of this skill explicitly asks to notify someone (Step 8); no
   stored recipient list, every send is scoped to who was named that run.

**Entities recur across platforms** - the same `entity_name` can appear as
several separate `main_broken_groups` (one per platform, e.g. the
`test_flashinfer_context_fallback_scope` cluster on DGX_B200/DGX_H100/B300).
Every script that keys data by entity must key by `(entity_name, platform)`,
never `entity_name` alone — keying by name alone was a real bug here once
(fetch_execution_details.py and build_confluence_cases.py both silently
collapsed 3 platforms' data into 1, corrupting a live Confluence publish
before it was caught). If you add a new per-entity data source, follow the
`exec_key()` pattern in `build_confluence_cases.py`.

Not part of the ordered workflow, but shared by the scripts above:
`scripts/confluence_config.py` and `scripts/google_sheets_config.py` each
hold their target's id/URL as the single source of truth — change the
destination there, not by editing defaults scattered across scripts.
`scripts/slack_config.py` has no destination to pin down (every
`post_slack_message.py` recipient is per-invocation), just the Slack API
base URL.

## Before Step 0: create the run directory

Every file this skill writes — from Step 0's known-builds lookup through
Step 5's action records, Step 6's report and Step 7's case JSON — goes into one **dated run
directory**, not loose in the working directory: `<run_dir>` =
`./<YYYY-MM-DD>/` for today's date. Create it now, once, before Step 0:
```bash
mkdir -p <run_dir>
```
Every `--out <run_dir>/...` (and bare `<run_dir>/...` path) referenced
anywhere below assumes this directory already exists. This keeps one run's
full output — data, evidence, and report together — self-contained and easy
to find later, and keeps a re-run on a different day from mixing with or
overwriting a prior run's files.

## Step 0: Load already-known builds from Confluence and/or the Google Sheet

Always run this first, before touching `trtllm-infra.nvidia.com` at all:

```bash
python3 <skill_dir>/scripts/fetch_confluence_known_builds.py --out <run_dir>/known_builds_confluence.json
```

This reads the Confluence case log page and pulls out every ci_report build
id already recorded on it (from whichever table layout is currently live —
the nested mode's `Build ID` column or the flat mode's `Build-PR Mapping`
cells). A build's test result is immutable once posted, so anything in this
set has nothing new to learn from a re-fetch — it's pure extra load on the
shared ci_report service for no benefit.

If the Google Sheet is also one of the publish targets this run (see "figure
out the target(s)" at the top of Step 7), run the same thing against it:

```bash
python3 <skill_dir>/scripts/fetch_google_sheet_known_builds.py --out <run_dir>/known_builds_sheet.json
```

Pass every known-builds file you have to each later `fetch_execution_details.py`
call (Steps 3 and 6) via `--known-builds-json` — it's repeatable, one flag
per source, and unions them — so those builds are skipped entirely: not
fetched, not re-processed, not duplicated in the output:

```bash
python3 <skill_dir>/scripts/fetch_execution_details.py --groups-json ... \
  --known-builds-json <run_dir>/known_builds_confluence.json \
  --known-builds-json <run_dir>/known_builds_sheet.json \
  --out ...
```

If the user hasn't asked about the Google Sheet, skip
`fetch_google_sheet_known_builds.py` entirely — don't touch that
credential/API just to check.

## Step 0.5: Determine the fetch window from the ci-status watermark

The dashboard has its own "Latest update" timestamp — the footer text on the
ci-status page ("Source: CIHealthMonitor -> NVDF Incident Documents · Latest
update ..."), sourced from `source.latest_ts_last_update` in the
`/api/incidents` response. This skill tracks that value as a **standalone
field** (independent of the case table/grid) on whichever publish target(s)
are in play, so a re-run only fetches the gap since the last sync instead of
always re-fetching a fixed lookback.

Skip whichever sub-step below isn't needed this run — each exists for one
specific downstream use, not as a mandatory ritual:
- Skip **sub-step 1** (reading the dashboard's current watermark) entirely
  if this run won't publish anywhere (report-only) — its only purpose is
  holding a value for step 4's write-back after a successful publish, and
  there's nothing to write back if nothing gets published.
- Skip **sub-step 2** (reading back the target's recorded watermark)
  entirely if the user already gave an explicit window (e.g. "last 30
  days") — it exists only to compute Step 1's `--days` when no explicit
  window was given; an explicit window overrides it anyway (see Step 1).
- If **both** apply — report-only run *and* an explicit window was given —
  skip this step entirely and go straight to Step 1 with the requested
  `--days`.

1. Read the dashboard's current watermark (cheap — a `days=1` call; this
   field doesn't depend on window size):
   ```bash
   python3 <skill_dir>/scripts/fetch_ci_status_watermark.py --out <run_dir>/ci_status_watermark_now.json
   ```
   Hold onto this value — it's what gets written back in step 4, after a
   successful publish.
2. Read back whatever watermark is already recorded on the publish target(s)
   in play this run (the same target(s) Step 7 publishes to — if that hasn't
   been decided yet, ask now rather than guessing, per Step 7's "figure out
   the target(s)" guidance):
   ```bash
   python3 <skill_dir>/scripts/sync_confluence_watermark.py --read --out <run_dir>/watermark_confluence.json     # if Confluence is a target
   python3 <skill_dir>/scripts/sync_sheet_watermark.py --read --out <run_dir>/watermark_sheet.json                # if the Sheet is a target
   ```
3. Decide Step 1's `--days`:
   - No watermark found on any target checked (first run, or the field is
     genuinely absent) → use `--days 7` (today's default).
   - A watermark found → convert the elapsed time to whole days, rounding
     **up**: `days = max(1, ceil(hours_since_watermark / 24))`. (The
     dashboard's `/api/incidents` endpoint only accepts a whole-number
     `days` param — `hours=`/`end=` and fractional `days` are all silently
     ignored server-side, and the ci-status page's own `?hours=` URL param
     only snaps to {24, 48, 168, 720} before falling back to a 2-day
     default — so there's no true hour-precision window to request;
     rounding up trades a little re-fetched overlap for never missing
     data.) If the targets checked in step 2 have different watermarks, use
     the **older** one, so no target's coverage is short-changed. Don't
     worry about landing on an unsupported day count yourself —
     `fetch_failures.py` snaps whatever you pass up to the nearest value the
     backend actually accepts (confirmed: only 1, 2, 7, 30; anything else
     400s — see its own docstring), printing a note when it does.
4. After Step 7 successfully publishes to a target, write
   `ci_status_watermark_now.json`'s timestamp back to *that* target:
   ```bash
   python3 <skill_dir>/scripts/sync_confluence_watermark.py --write <timestamp>   # after publishing to Confluence
   python3 <skill_dir>/scripts/sync_sheet_watermark.py --write <timestamp>        # after publishing to the Sheet
   ```
   Only write a target's watermark after actually publishing cases to it
   this run — writing it unconditionally would mark a target "caught up"
   even though its case log never changed. (For the Sheet specifically,
   write the watermark *after* `post_google_sheet_cases.py` — see
   `sync_sheet_watermark.py`'s docstring for why the ordering matters when
   the sheet has no header row yet.) If the user isn't publishing this run
   at all (report-only), skip step 4 entirely — nothing to mark as caught up.

## Step 1: Fetch Detection Details for the requested window

Use the `--days` value computed in Step 0.5 (7 by default, on a first run
against a target with no watermark yet, matching the dashboard's own
default). If the user explicitly asks for a specific window (e.g. "last 30
days"), that overrides the computed value.

```bash
python3 <skill_dir>/scripts/fetch_failures.py --days <computed-or-requested-days> --out <run_dir>/trtllm-failures-<days>d-<date>.json
```

This prints a compact stdout table (confidence, waived Y/N/?, platform,
failure/pass counts, PR count, entity name) for every detected Main Break
group, and writes the full condensed data to the `--out` path (default
`<run_dir>/trtllm-failures-<days>d-<date>.json` in the current directory). The
stdout table is usually enough to triage; only read into the JSON file for
details on the specific entities you decide to dig into (use `jq` or `grep`
to pull one entity's record rather than reading the whole file — it can
still be several hundred KB).

Each condensed group already includes `waived` (bool, or `null` if the
waive-info call failed) and `waive_bugs` (list of `{id, url}`), fetched by
this script from the dashboard's `/api/test-waive-info` endpoint — you don't
need a separate step for waive status.

By default this excludes `entity_kind: "stage"` groups (they have no
test-history page, so `fetch_execution_details.py` and
`fetch_latest_status.py` can't investigate them, and they are often
platform-wide infra noise rather than individual code breaks). **Always also
run it once with `--include-stages`** (to a second `--out` file) so the
stage-level detections are known, and then apply this rule:
- If the default run returned **zero** test-kind groups, or the user asks
  about a stage, or a stage-kind group recurs across daily windows / carries
  no dashboard comment explaining it — **investigate the stage-kind groups**
  via Step 4's "Stage-kind entities" subsection (log navigation recovers the
  real failing case and error), run them through Step 5's handlers, and
  report them as first-class entries. "Not investigated" is not an acceptable
  final state for a stage-kind group in a window that has nothing else in
  it.
- Otherwise (a busy window with real test-kind breaks), list the stage-kind
  groups in the report with their PRs/window/comment and investigate only
  those the triage in Step 2 flags (recurring, no comment, or overlapping the
  PR set of a test-kind break).

## Step 2: Triage

This step is pure judgment on the JSON `fetch_failures.py` already wrote —
no network access.

From the table, prioritize:
- **High confidence** groups first — these already required either >5
  failure PRs or a PostMerge failure with zero passes (see
  `confidence_reason` in the JSON for the exact reason).
- Within a confidence tier, higher `failure_count` / `failure_pr_count` /
  `postmerge_failure_count` and lower `pass_rate` indicate a harder, more
  persistent break rather than a one-off flake.
- Group by `platform` and `entity_name` prefix (e.g. shared test file) to
  spot whether one infra/platform or one code area is the common thread
  across multiple entries — that's often the actual root cause of several
  "different" failures.

Check `waived` for each group before investigating it: `waived: true` means
the entity is already tracked against a known bug (see `waive_bugs` for the
nvbugs link(s)) — deprioritize these for fresh root-cause work and roll them
up in a "already tracked" list instead of a full write-up, unless the user
asks otherwise or the current failure pattern looks like a new/different
issue on that same entity (e.g. a different platform, or failures far more
frequent than the waive would explain).

Not every one of the ~100-160 detected groups needs individual write-ups —
use judgment about how many are worth deep investigation vs. a rollup line
in a table (typically the top 10-20 by the criteria above, plus anything the
user specifically asked about).

## Step 3: Gather execution details

For entities worth digging into, pull per-execution detail (real waive
status, bug links, and the actual short error message per failing run, not
just the static waives.txt snapshot) — this is pure data-gathering; Step 4
is where it gets turned into a root-cause conclusion:

```bash
python3 <skill_dir>/scripts/fetch_execution_details.py --groups-json <fetch_failures.py output> --known-builds-json <run_dir>/known_builds_confluence.json --known-builds-json <run_dir>/known_builds_sheet.json --out <run_dir>/trtllm-execution-details-<date>.json
```

(Include whichever `--known-builds-json` file(s) you actually fetched in
Step 0 — drop a flag if you didn't fetch that source.) This walks every FAILED execution for each test-kind entity (via
`/api/test-detail` on the stability dashboard) and looks up its waive
status / bug / error message from the ci_report site's per-build API
(`/api/job/<job>/<build>`), deduping repeated builds across entities so one
build is only fetched once — and, given `--known-builds-json` from Step 0,
skipping any build already recorded there entirely. It defaults to
a gentle request pace (a handful of workers, a minimum gap between request
starts) for the ci_report calls specifically — do not raise `--job-workers`
/ lower `--job-min-interval` without a clear reason, since that host has
been observed to 503 broadly under bursty concurrent load. Stage-kind
entities (no test-history page) are skipped with a note; this can take
several minutes for the full detected set even with known builds skipped,
so only run it against the subset of entities you're actually investigating
(filter the `--groups-json` input) rather than all ~150 unconditionally,
unless you're about to publish the full case log (Step 7).

For non-trtllm-infra / non-ci-report context — the Jenkins console
(`jenkins_urls` / `source_build_url`), and the raw incident doc
(`nvdf_document_url`) — WebFetch or curl those directly, since they're
different hosts.

## Step 4: Analyze failure reasons

Turn what Step 3 gathered into a grounded root-cause conclusion per notable
group. Apply the methods below **in order** — each one is a fast-path check
that, if it hits, ends the analysis for that group; only fall through to the
next method when the current one doesn't produce a confident answer.

This step delegates to two named subagent types (`.claude/agents/`), not ad
hoc generic ones: `ci-jenkins-log-navigator` for recovering a failing case +
its full error text from Jenkins/Blue Ocean logs, and `ci-regression-verifier`
for checking whether a candidate PR/commit's diff explains a given failure.
Each is referenced by name at its call site below.

**Dump evidence to disk as it's generated, in this step, not just at Step 6
report time** — every fetch/subagent call below produces something worth
keeping (a full callstack, a verdict + diff evidence, a final conclusion),
and holding it only in conversation/agent-return-text risks losing it to
context compaction or a later mistake overwriting the reasoning. Write every
file below into `<run_dir>` (created "Before Step 0", above), not the
current directory directly. Use one `<slug>` per entity for all of it —
`entity_name` with `/`, `::`, and spaces replaced by `_` (e.g.
`unittest/_torch/sampler/test_x.py::test_y` →
`unittest__torch_sampler_test_x.py__test_y`) — so every file for one entity
is findable by prefix within `<run_dir>`:
- `<run_dir>/full_error_<slug>.json` — the full failure_message/callstack (from
  `fetch_full_error.py` directly, or written by `ci-jenkins-log-navigator`
  when it had to recover it instead — see below).
- `<run_dir>/verify_<slug>_pr<number>.json` / `<run_dir>/verify_<slug>_commit<short-sha>.json`
  — each `ci-regression-verifier` call's verdict + diff evidence (Methods B
  and C).
- `<run_dir>/analysis_<slug>.json` — the final per-entity conclusion (category,
  method, reasoning, attributed PR/commit, links to the evidence files
  above) — see "Categorizing the conclusion" below.

### Stage-kind entities: recovering failed cases from logs

A `stage`-kind group (only present when `fetch_failures.py` was run with
`--include-stages`) has no test-history page, so it never has `executions`,
`short_error_msg`, or anything else Step 3 normally produces — the
dashboard only knows the whole Jenkins stage failed, not which individual
test case(s) inside it did. Before Methods A-C can apply, that has to be
recovered by hand, per stage-kind group:

1. Group stage-kind entities that share the same `failure_pr_identifiers`
   and detection window first — several can be one shared incident (e.g. a
   packaging/import break that fails every stage that imports the affected
   package) rather than independent breaks, and investigating them together
   in one subagent avoids redundant Jenkins navigation and makes a shared
   root cause obvious instead of looking like several coincidental ones.
2. For each (grouped) stage entity, invoke the `ci-jenkins-log-navigator`
   agent (via the `Agent` tool with `subagent_type: ci-jenkins-log-navigator`,
   not a fork) to find the actual failing test case(s).
   Give it an output path `<run_dir>/full_error_<slug>.json` (same slug convention
   as above, using the stage-group's own name) to write its recovered
   case(s)/error text to — this is what "Get the full error message/callstack
   before analyzing" below reads from when a stage-kind group has no script
   output of its own.
   Give it the stage name(s), platform, the PR(s) behind the failure(s),
   the detection window, and the group's `links.stage_dashboard` /
   `latest_observation.source_build_url` / `latest_observation.jenkins_urls`
   / `latest_observation.nvdf_document_url` (note `source_build_url`/
   `jenkins_urls` point at the CIHealthMonitor *detector* job, not the
   actual failing build — the subagent needs to navigate from there, or
   straight from the stage's Jenkins job, to the real build). Instruct it
   to:
   - Find the actual Jenkins build(s) for the given PR(s) around the
     detection window (the top-level `L0_MergeRequest_PR` orchestrator
     often dispatches the real stage as a *remote* parameterized job on a
     different Jenkins controller host — check for that redirection rather
     than assuming the stage runs on the same host as the orchestrator).
   - Pull that stage's console log (Blue Ocean node log) and find the
     specific failing test case(s)/command and its full error/traceback —
     a pytest failure block if it's pytest-based, or the failing
     command/error if the stage is a shell-based sanity check rather than
     pytest.
   - Report back per case: the full error text, and whether multiple PRs
     hit the identical error (a strong signal the group's real cause is
     shared/upstream, not any of those PRs' own diffs).
3. Once a stage-kind group's actual failing case(s) and error text are
   recovered this way, treat each one exactly like a normal case from here
   on — feed it into Methods A-C below using the standard opening line
   (just below) for any further subagent, with `[Case Name]` being the
   recovered test case id (or the stage name itself, if the failure is a
   pre-test infra/collection error with no single test case to name) and
   `[Error Message]` being the recovered error text, not the stage-level
   summary.

### Skip cases already analyzed with an unchanged signature

Before running any method below, check whether a group has already been
analyzed and nothing has changed since:

```bash
python3 <skill_dir>/scripts/fetch_confluence_case_analysis.py --out <run_dir>/case_analysis_confluence.json     # if Confluence is a publish target
python3 <skill_dir>/scripts/fetch_sheet_case_analysis.py --out <run_dir>/case_analysis_sheet.json                 # if the Sheet is a publish target
```

For a group whose case name appears in either file: compare its current
error message/callstack (Step 3's `short_error_msg`, or the fuller message
from the step below if you've already pulled one) against that file's
recorded `signature`. If they match — same error text, ignoring
incidental noise like build ids/timestamps embedded in it — **skip Methods
A/B/C for this group entirely** and reuse the recorded `failure_type` as its
conclusion; still write `<run_dir>/analysis_<slug>.json` for it (per "Categorizing
the conclusion" below), with `"method": "skip-unchanged"` and `"reasoning"`
noting it was carried forward unchanged from the prior recorded analysis —
this is a judgment comparison (materially the same failure), not
an exact string match: cosmetic differences (a different build number in the
message, a slightly different PR list) don't count as a change; a different
exception type, a different file/line, or a different symptom does.

If the signature differs, or the group isn't in either file yet (never
analyzed, or analyzed under a since-changed Failure Type schema), run
Methods A–C as normal — the world may have moved on since the last
conclusion was recorded.

### Get the full error message/callstack before analyzing

`fetch_execution_details.py`'s `short_error_msg` (`s_short_error_msg` from
ci_report's `/api/job/<job>/<build>`) is frequently **truncated at the
source** — not a display artifact, the field itself is short. Relying on it
alone risks a wrong conclusion: this happened in practice once already — a
truncated `"name ...= 200"` message led to attributing a break to a
plausible-but-wrong PR by circumstantial timing/topic match, until the real
message (`"name 'get_steady_clock_now_in_seconds' is not defined"`) was
retrieved and pointed straight at the actual missing import. **Do this
before Methods A–C, not just inside Method C** — a truncated message can
mislead Method A's keyword match too, and picking the wrong candidate PR to
even check in Method B wastes a subagent call on a guess Method A/B/C should
never have had to make.

For **every** group about to be analyzed (not skipped by the signature
check above), always run `scripts/fetch_full_error.py` for one
representative failing execution (one build/stage is normally enough — the
error is typically identical across a break's executions; pick any one from
`fetch_execution_details.py`'s `executions` list for that entity) — do not
skip this step just because `short_error_msg` already looks like enough to
draw a conclusion (e.g. a clearly repeating pattern, or an existing
dashboard comment suggesting a cause). The whole point of this step is that
`short_error_msg` can look conclusive and still be misleading; judging "this
already looks clear enough" from the short message is exactly the mistake
this step exists to prevent, not a valid reason to skip it:

```bash
python3 <skill_dir>/scripts/fetch_full_error.py \
  --job <execution job> --build <execution build> --stage <execution stage> \
  --entity-name <entity_name> --out <run_dir>/full_error_<entity>.json
```

This does steps 1-3 below itself — no ad hoc curl/WebFetch, no subagent
needed for the common case:

1. `GET {ci_report_base}/api/job/<job>/<build>` (same endpoint
   `fetch_execution_details.py` already calls) and checks `_pbss_log` first
   — it holds the full, untruncated console/pytest log for the build (not
   just `s_short_error_msg`), and usually already contains the complete
   failure output for the relevant test (assertion message *and* traceback,
   plus captured stdout/stderr) without needing Jenkins at all. Note a
   sub-test entity's own record has no `_pbss_log` of its own — only the
   file-level record (whole-file pytest invocation) does, so the script
   automatically falls back to that file-level record's `_pbss_log` for a
   `<file>::<Class>::<test>`-shaped `--entity-name`. It extracts the
   specific failure block(s) by matching pytest's own underscore-delimited
   header line (`___ TestClass.test_name ___`) against a filter derived
   from `--entity-name`'s part after `::`; for a file-level `--entity-name`
   (no `::`), there's no single test to filter by, so it returns **every**
   failure block found — useful since a file-level group can roll up
   several sub-tests' distinct failures (confirmed in practice: a
   `test_trtllm_serve_e2e.py` file-level group rolled up both an unrelated
   `NameError` in its Flux sub-tests and a separate server-startup timeout
   in its Wan sub-tests — two different root causes under one detection).
2. If `_pbss_log` (including the file-level fallback) doesn't yield a
   matching block, falls back to fetching the stage's raw Jenkins log
   directly at its `log_link` (already resolved by
   `fetch_execution_details.py`'s `find_stage_log_link()` — the same "Log"
   link ci_report's own UI shows for that stage, found via
   `categorized_stages.data` in the same job JSON, no node-discovery
   navigation needed) and searches that the same way.
3. **Independent of whether 1-2 found anything, and merged with it rather
   than only a fallback**: the case's own JUnit XML from the stage's
   uploaded test-results artifact. Console-log regex matching can miss a
   sub-test entirely when a whole directory/file ran under one pytest
   invocation (confirmed in practice — this recovered a sub-test's failure
   steps 1-2 found nothing for at all), while the JUnit XML always has it,
   keyed cleanly by test name. Resolved by walking the Blue Ocean pipeline
   graph forward from the stage's own node through its `edges` to whichever
   node is reached first — "Submit Test Result" (plain unittest stages) or
   "Generate Report" (accuracy/perf stages) — then checking every "Upload
   artifacts" step under it (there can be more than one; one is sometimes
   an unrelated rerun-report HTML, not the actual archive) for the
   `.tar.gz`/`.zip` test-results artifact it deployed to Artifactory,
   downloaded into `<run_dir>/artifacts/` (a file already there for the
   given (job, build, url) — the common case, since one stage's artifact is
   shared by every sub-test in it — is reused instead of re-downloaded; the
   script records both the artifact's URL and its local path in
   `junit_xml_artifacts_tried` in its output). The
   target XML inside is found via a hint read straight out of the console
   text already fetched in step 1/2 (pytest's local-variable dump on
   failure includes a literal `output_xml = '<path>'` line), or by scanning
   every XML in the archive for a matching `<testcase>` if that hint isn't
   available. When a match is found, its `<failure>`/`<error>` text is
   **appended** to whatever step 1/2 already found for that block (not a
   replacement) — either source can carry detail the other lacks, so the
   final `blocks[].text` is the union, not a pick-one.

Only if the script's output *still* has an empty `blocks` list after all
three steps (nothing in `_pbss_log`, `log_link`, or the JUnit XML archive —
e.g. a session killed by a stage timeout before this specific test's result
was ever recorded anywhere, confirmed to happen in practice) fall back
further: invoke the
`ci-jenkins-log-navigator` agent (via the `Agent` tool with
`subagent_type: ci-jenkins-log-navigator`, not a fork) with the execution's
`job`/`build` to read `job_info.s_jenkins_link` from
`GET {ci_report_base}/api/job/<job>/<build>` and navigate the Jenkins
pipeline's Blue Ocean REST API (`.../runs/<build>/nodes/`) to find the
failing stage's node manually — this manual navigation is per-CI-quirk
enough (nested pipeline stages, Blue Ocean's node-id indirection) that it's
better delegated to an agent's judgment than hardcoded into a script, which
is why this last resort is a subagent step and not a new script. Give it
the same `<run_dir>/full_error_<slug>.json` path `fetch_full_error.py --out` was
already pointed at, so it overwrites that (empty-`blocks`) file with what it
finds — downstream steps always read from that one path regardless of
whether the script or the subagent ultimately populated it.

Use the script's `blocks[].text` (or the subagent's report, in the
last-resort case) — not the truncated `short_error_msg` — as the evidence
going into Methods A, B, and C below. If nothing beyond `short_error_msg`
can be found anywhere (some errors really are short), that's fine — proceed
with what's available, just don't assume a message ending in `...` or that
looks cut off is the complete picture without checking.

### Standard opening line for any case-failure investigation subagent

Every subagent spun up to analyze a specific case's failure (Method B's
per-PR check, Method C's commit verification, stage-kind entities' log
investigation below, and any other ad hoc "figure out this failure"
delegation) must open its prompt with this exact line, filled in from the
data already gathered above — not a paraphrase:

```
test case [Case Name] meets error "[Error Message]". Check when this test
case is added. Check the commit history to analyze what does the failure
mean and how it happens? Try to find out if there is commit to fix this
failure.
```

- `[Case Name]` — the full `entity_name` (e.g.
  `unittest/_torch/visual_gen/test_trtllm_serve_e2e.py::TestFlux1TextToImage::test_t2i_sync_b64`,
  or a stage name like `GB10-PyTorch-1` for a stage-kind entity).
- `[Error Message]` — the fullest error text available at the time the
  subagent is launched (prefer `fetch_full_error.py`'s `blocks[].text`;
  fall back to `short_error_msg` only if nothing fuller has been pulled
  yet).
- "Check when this test case is added" directs the subagent to look up the
  test file's/test case's own git history (`gh api
  "repos/NVIDIA/TensorRT-LLM/commits?path=<test file>&sha=main"`, going back
  far enough to find its addition, or `git log --follow` if working in a
  local clone) — recently-added tests are more likely a new/still-unstable
  case than an established one suddenly regressing, which is useful context
  for judging Flaky vs. Regression.
- "Check the commit history to analyze what does the failure mean and how
  it happens" is the root-cause ask, anchored explicitly to commit
  history rather than left fully open-ended — ground the mechanism in an
  actual commit/diff (`gh api commits?path=...`, `gh pr diff`, `git log
  -p`, etc.), not just a plausible-sounding narrative. This is where the
  subagent's specific task (PR diff check, commit verification, Jenkins log
  dig, etc.) supplies the method. Append whatever Method B/C/stage-specific
  instructions apply after this opening line; don't replace it with them.
- "Try to find out if there is commit to fix this failure" directs the
  subagent to also search commits/PRs merged *after* the failure's window
  for one that resolves it (same search techniques as Method C step 1-2),
  and report that commit's SHA if found — or say plainly that none exists
  yet. This is the fix-commit half of Method C step 4 and the input to its
  step 5 validation (`get_base_commit.py`); don't skip it just because the
  root-cause half already produced a satisfying story.

### Method A: Infra-error signature match (fast path)

Check the group's error message(s) (`short_error_msg` from
`fetch_execution_details.py`, or the fuller console log if that's truncated
or uninformative — see Method C step 1) for known infra-failure phrasing:
**device error**, **failed to load weights** (or "failed loading weights"),
**unable to connect to node(s)**, **lose/lost connection to node(s)** — and
close variants of these. A match here is a direct, strong signal: classify
the group as an **infra failure** and stop — no need for Methods B or C.

If none of those phrases match, treat "multiple unrelated tests/stages
breaking together on the same platform in the same time window" as a
weaker, supporting infra signal instead — cross-check the Incidents
Timeline / cluster outage data on the dashboard for a matching outage before
concluding infra from this pattern alone. If that doesn't hold up either,
this group is **not an obvious infra failure** — move to Method B.

Only state a hypothesis you can back with something concrete from the
fetched data — don't invent a root cause you haven't actually seen evidence
for.

### Method B: Recent-failure / PR-attribution check via subagents

For a group that Method A didn't resolve as infra, check its recent
failures against the PR(s) behind them. Do this for every PR behind a recent
failure of the group (not only when `failure_pr_count == 1` — that's just
the simplest case of this, where there's exactly one PR to check):

1. For each candidate PR, invoke the `ci-regression-verifier` agent (via the
   `Agent` tool with `subagent_type: ci-regression-verifier`, not a fork —
   it needs its own clean judgment, not this conversation's accumulated
   hypotheses) to check whether that PR's own code change caused the
   failure. Give it an output path `<run_dir>/verify_<slug>_pr<number>.json` to
   write its verdict + diff evidence to. Open its prompt with the
   standard line above, then give it, self-contained: the entity
   name/platform, the PR number, and the error message(s)/evidence already
   gathered for that entity from `fetch_execution_details.py`
   (short_error_msg, confidence_reason, failing stage/file). Instruct it to
   fetch the PR's actual diff with `gh pr diff <number> --repo
   NVIDIA/TensorRT-LLM` (or `gh pr view <number> --repo NVIDIA/TensorRT-LLM`
   for context first), read it, and report a clear verdict — `True` (the
   diff plausibly explains this specific failure) or `False` (it doesn't) —
   with a short justification citing specific files/lines from the diff,
   not speculation.
2. A `True` verdict is the attributed cause — record it (e.g. "PR
   attribution check: caused by this PR's change to `<file>` — see diff
   evidence", citing `<run_dir>/verify_<slug>_pr<number>.json`) and move on to the
   next group.
3. If every checked PR comes back **`False`**, this group's cause isn't a
   single recent PR's own diff — move to Method C for a deeper,
   callstack-driven root cause.

This assumes PR numbers in this data are public `NVIDIA/TensorRT-LLM` GitHub
PR numbers (confirmed) — the subagents need `gh` CLI access to that repo.

### Method C: Callstack vs. commit-history correlation

When Method B doesn't land on one attributable PR, cross-reference the full
callstack/error message (from the step above — not the truncated
`short_error_msg`) against what merged to `main` in the failure's window, to
find the specific commit that introduced it. **Search by the exact
symbol/identifier first, not by topic/timing alone** — picking a candidate
commit just because its title sounds related and its timestamp is close
(e.g. "this PR redesigns the same subsystem, and merged right before the
break") produced a confident-sounding but wrong attribution once already;
matching against the actual name in the error message is what caught the
mistake:

1. If the error message/callstack names a specific symbol (a function,
   variable, class, module path — e.g. a `NameError`'s exact `name '...' is
   not defined`, an `ImportError`'s module path, an `AttributeError`'s
   attribute), search for it directly: `gh api
   "search/code?q=<symbol>+repo:NVIDIA/TensorRT-LLM"` to find where it's
   defined/used, then `gh api "repos/NVIDIA/TensorRT-LLM/commits?path=<file>&sha=main&since=<window-start>&until=<window-end>"`
   on the specific file(s) that reference it to find what changed there
   recently. This directly locates the bug (e.g. an import added in one
   file but not another) rather than inferring it from a diff read alone.
2. Only if there's no specific symbol to search (a generic assertion
   failure, a crash with no clear name/attribute), fall back to listing
   everything that merged to `main` in the window between the last
   known-good run and the first failing one (`gh api
   "repos/NVIDIA/TensorRT-LLM/commits?sha=main&since=...&until=..."`, or
   `gh pr list --state merged --base main --search "merged:<window>"`), and
   narrow by which of those touch files plausibly related to the failing
   test/entity.
3. Invoke the `ci-regression-verifier` agent (via the `Agent` tool with
   `subagent_type: ci-regression-verifier`, not a fork — same diff-reading
   judgment call as Method B, so treat it the same way rather than doing
   this matching inline) to verify the candidate. Give it an output path
   `<run_dir>/verify_<slug>_commit<short-sha>.json` (short-sha = first 7-12 hex
   chars) to write its verdict + diff evidence to. Open its
   prompt with the standard line above, then have it read the actual diff
   of the specific file/commit found in step 1 (not just
   the PR's overall diff — a large PR's summary can look topically relevant
   while the actual bug is a one-line omission the summary doesn't mention)
   and confirm the exact mechanism — e.g. "line N calls `foo()` but the
   import block only brings in `bar`" — not just "this PR touches related
   code."
4. Report the specific commit (hash, and PR number if it landed via one) as
   the attributed root cause, with the exact line/mechanism as
   justification (citing `<run_dir>/verify_<slug>_commit<short-sha>.json`) — don't
   settle for "probably introduced around this time" if the commit and the
   specific broken line can both be pinned down from what's available.
5. **Validate the fix commit against every build behind this group's
   failures**, using `scripts/get_base_commit.py` — do not skip this even
   when step 4's verdict felt confident:
   ```bash
   python3 <skill_dir>/scripts/get_base_commit.py \
     --executions-json <fetch_execution_details.py output, filtered to this group's builds> \
     --fix-commit <the commit from step 4> \
     --out <run_dir>/base_commits_<slug>.json
   ```
   This resolves each build's actual base commit via GitHub's compare API
   (`merge_base_commit` of the tested head commit vs. `main`) — **not** the
   PR's `baseRefOid`/`base.sha`, which for an open, unmerged PR tracks the
   *current* tip of `main` and moves forward automatically regardless of
   whether the PR branch itself ever rebased, so it silently produces false
   "this build's base is post-fix" conclusions (confirmed in practice: it
   did, for a PR that had never actually rebased past the fix — do not
   reach for that field for this check, even ad hoc).
   - If **every** build's `base_is_pre_fix` is `true` — the attributed fix
     commit is validated. Proceed to record it (below).
   - If **any** build shows `base_is_pre_fix: false` — the fix commit does
     not actually explain that build's failure (it was already present when
     that build ran, yet the build still failed). Don't record the step 4
     conclusion as-is: go back to step 1 of this method with that
     contradiction in hand (a later, different commit may be the real fix,
     or the mechanism may need revisiting) and re-verify. If re-investigation
     converges on the **same** fix commit again, treat it as the best
     available answer but flag it explicitly as unresolved/contradicted in
     the conclusion (below) rather than presenting it as settled.

### Categorizing the conclusion

Whichever method above resolves a group, label it with one of these terms:
- **Infra failure** — Method A's signature match, or its supporting
  same-platform/same-window pattern backed by a matching outage.
- **Regression** — Method B/C attributed it to a specific PR or commit
  (include the identifier in the label, e.g. "Regression (PR #18684)" or
  "Regression (commit abc1234)"), or (absent that) the group shows
  consistent failures across many PRs/commits with no passes, especially
  with a PostMerge failure.
- **Flaky test** — mixed pass/fail, low `failure_pr_count`, medium
  confidence, sporadic timing, and no infra/PR/commit attribution found —
  worth flagging but lower urgency.

Use this label both in the report (Step 6's "Likely cause" per entity) and,
if the case log is being published this run (Step 7), as that entity's
`failure_type` in a `--failure-types-json` file fed to
`build_confluence_cases.py` — it publishes as a "Failure Type" column,
alongside Waived/Latest Status. Only entities you actually ran Methods A–C
on this run belong in that file — **not** entities the signature check
above skipped: `build_confluence_cases.py`'s "absent means don't touch this
cell" convention already leaves an untouched entity's existing Failure Type
exactly as it is (flat mode) or carries it forward (nested mode), so a
skipped case needs no entry here at all; adding one would just be
redundant, not wrong.

As soon as a group reaches a conclusion this way — right here, not deferred
to Step 6 — write `<run_dir>/analysis_<slug>.json`:

```json
{
  "entity_name": "<full entity_name>",
  "platform": "<platform>",
  "method": "A | B | C | skip-unchanged",
  "category": "Infra failure | Regression (PR #123) | Regression (commit abc1234) | Flaky test",
  "attributed_pr": "<number, or null>",
  "attributed_commit": "<full sha, or null>",
  "reasoning": "<the mechanism/justification, in prose>",
  "confidence": "low | medium | high",
  "evidence_files": ["<run_dir>/full_error_<slug>.json", "<run_dir>/verify_<slug>_pr<number>.json", "..."]
}
```

This is the single file Step 5 (dispatch), Step 6 (report) and Step 7 (publish) read the
conclusion from — write it once per entity, here, so the reasoning and its
evidence trail survive independent of this conversation.

## Step 5: Dispatch each failure to its handler and record the actions

Step 4 ends with a label per entity; this step turns each label into a
concrete, recorded set of actions. It is pure judgment on the
`analysis_<slug>.json` files Step 4 wrote — no new network access beyond
what a handler explicitly lists — and it runs for **every** entity that has
an `analysis_<slug>.json` this run (including stage-kind entities whose
cases were recovered from logs, and `skip-unchanged` ones, which simply
inherit the handler of their recorded failure type).

### The handlers

Dispatch on `category` (the Step 4 label), in this order — the first
handler whose condition matches owns the entity:

1. **Infra failure** →
   - Identify the infra component from the error text (SLURM login node /
     allocation, node connectivity, storage/model download, container/image,
     GPU health, stage timeout with a test IN-FLIGHT) and record it.
   - Check recovery: run `fetch_latest_status.py` for the entity (or, for a
     stage-kind entity, look at whether later builds of the same stage ran
     to completion) and record `recovered: true/false` with the last
     observed build/time.
   - If the failure text was **not** recognised by CI's INFRA-RETRY
     classifier (the log says "no infra pattern matched (classified as user
     failure)"), record the exact signature strings as a proposed addition
     to the infra-retry pattern list — that misclassification is what
     promotes an outage to a "Main Break".
   - Actions to record: `notify-infra` (who/what channel is named in the
     dashboard comment, if any), `add-infra-retry-pattern <strings>`,
     `no-code-action`. Never propose a waive for an infra failure.

2. **Regression (PR #N / commit X) with a fix already on `main`** →
   - Record the fix commit/PR, its merge time, and — from Step 4 Method C
     step 5's `base_commits_<slug>.json` — how many of this entity's failing
     builds have a base that predates the fix (expected: all of them).
   - Actions: `rebase-affected-prs` with the list of PRs whose builds
     failed on pre-fix bases (from `build_pr_map`), `no-further-code-action`.
     If any failing build has a **post-fix** base, do **not** close it out
     here — re-dispatch that build's failure as its own entity through
     Step 4 (it is a different problem) and record `split-off: <new slug>`.

3. **Regression (PR #N / commit X) with no fix on `main`** →
   - Record the culprit (PR/commit, merge time, mechanism from
     `verify_*.json`) and the blast radius (number of entities/platforms
     sharing the attribution, number of distinct PRs hit).
   - Actions: `notify-author <PR author from gh pr view>` (Step 8 does the
     actual Slack send, only if the user asks), `propose-revert-or-fix`
     (state which is cheaper given the diff), `file-or-link-nvbug` (link an
     existing `waive_bugs` id if the dashboard already carries one), and,
     for a break that blocks many PRs, `propose-temporary-waive` with the
     exact `waives.txt` line — a waive is a stop-gap, record it as such.

4. **Regression attributed to the PR's own diff** (the "culprit" is the very
   PR whose build failed — e.g. an `AttributeError` only reachable from the
   PR's new code) →
   - This is not a main break. Actions: `comment-on-pr <number>` with the
     mechanism, `exclude-from-case-log` (do not publish it as a main-branch
     case in Step 7; note it in the report's "PR-own defects" list).

5. **Flaky test** →
   - Record pass/fail counts and the pattern (mixed pass/fail, low
     `failure_pr_count`, sporadic timing) and whether it is already waived.
   - Actions: `link-nvbug` (existing `waive_bugs` id) or `file-nvbug` if
     none, `propose-waive <waives.txt line>` when the flake blocks unrelated
     PRs, `request-owner-triage` naming the owner from the dashboard
     comment or `CODEOWNERS` for the test file.

6. **Unattributed / unresolved** (Methods A–C exhausted, or Method C step 5
   contradicted the candidate) →
   - Record the bounded commit range and the ranked candidates from
     `verify_*.json`, and whether the fault is intermittent (same base →
     different victims; post-onset clean builds).
   - Actions: `hardware-bisect <range, first commits to try>`,
     `sanitizer-run <shard / test files>` for device faults (CUDA illegal
     memory access, hangs), `request-owner-triage`, and `track-daily`
     (re-check on the next run; if the dashboard already shows an owner
     comment, record it instead of proposing new ownership).

If an entity matches two handlers (e.g. a roll-up whose pre-fix failures are
a fixed regression and whose post-fix failures are unattributed), record
**both** action sets under the one entity, each scoped to its build list —
do not pick one.

### Record the actions

Write `<run_dir>/actions_<slug>.json` per entity:

```json
{
  "entity_name": "<full entity_name>",
  "platform": "<platform>",
  "category": "<Step 4 label>",
  "handler": "infra | regression-fixed | regression-open | pr-own-defect | flaky | unresolved",
  "actions": [
    {"action": "rebase-affected-prs", "prs": ["18614", "18898"], "note": "bases predate 7db313ce"},
    {"action": "add-infra-retry-pattern", "patterns": ["fork: Resource temporarily unavailable", "No job ID found"]}
  ],
  "owner": "<name/team from dashboard comment or CODEOWNERS, or null>",
  "status": "open | recovered | fixed-awaiting-rebase | pr-own",
  "evidence_files": ["<run_dir>/analysis_<slug>.json", "..."]
}
```

and one consolidated `<run_dir>/actions_<date>.json` (`{"results": [...]}`,
all entities) that Step 6 reads to build its **Actions** section and that
Step 7 can attach to each published case's analysis text. Actions are
proposals until a human executes them — never file bugs, edit `waives.txt`,
comment on PRs, or message people from this step; Steps 7/8 are the only
outward-facing steps and both stay gated on an explicit request.

## Step 6: Write the report

Save a markdown report (default path `<run_dir>/trtllm-main-failures-report-<date>.md`,
or wherever the user specifies) with:

```markdown
# TRT-LLM Main Branch Failures Report — <window, e.g. last 7 days>
Generated: <date>. Source: trtllm-infra stability report, Detection Details (Main Break).

## Summary
- Total Main Break groups detected: N (H high / M medium / L low confidence)
- Breakdown by platform / by likely cause

## Top Offenders
| Entity | Platform | Confidence | Waived | Failures/Passes | PRs | Likely cause |
|---|---|---|---|---|---|---|
| ... | ... | ... | Y ([nvbug](...)) / N | ... |

## Investigation Details
### <entity_name> (<platform>)
- Confidence: ... — <confidence_reason>
- Window: <first seen> – <last seen>, <window_count> detection window(s)
- Likely cause: ... (evidence: ...)
- Actions: <from actions_<slug>.json — handler + action list>
- Links: [Jenkins](...) · [Test history](...) · [NVDF doc](...)

## Actions (from Step 5)
| Entity | Platform | Failure type | Handler | Actions | Owner | Status |
|---|---|---|---|---|---|---|
| ... | ... | Regression (PR #…) | regression-fixed | rebase-affected-prs (#…, #…) | … | fixed-awaiting-rebase |

## PR-own defects (not main breaks, excluded from the case log)
| Entity | Platform | PR | Mechanism |
|---|---|---|---|

## Waived / Already Tracked
Entities with `waived: true` — already tracked against a known bug, not
investigated fresh above unless their current pattern looked like a new issue.

| Entity | Platform | Bug |
|---|---|---|
| ... | ... | [nvbugs/...](https://nvbugs/...) |
```

Then give the user a short chat summary: the headline (how many breaks,
worst offenders, any cross-cutting infra issue found), and point them at the
saved report file.

## Step 7: Publish cases (only if the user asks)

If — and only if — the user asks to record/post/log the cases somewhere,
publish **one entry per individual test/stage entity** (the raw
`entity_name` from `main_broken_groups`, not a rolled-up cluster name). Do
not merge related entities into one row/case, even when Step 4's
analysis identified them as sharing one root cause — the case log
tracks failures 1:1 with what the dashboard detected; cross-entity
groupings belong in the report's narrative (Steps 4-5), not here.

There are two possible targets, and they're independent, equally-supported
publish destinations — this isn't "Confluence, plus a Sheet as an
afterthought":
- **Confluence** — the "CI failure cases records" page at
  `scripts/confluence_config.py`'s `CONFLUENCE_PAGE_URL`. Supports both flat
  (one row per entity) and nested (one row per PR-per-build) layouts.
- **Google Sheet** — at `scripts/google_sheets_config.py`'s
  `SPREADSHEET_URL`. Flat layout only (no rowspan/nested equivalent in a
  plain grid).

**Figure out the target(s) before doing anything else.** If the user named
one explicitly ("post to the sheet", "log these to Confluence"), that's it.
If they just said something like "publish/record/log the cases" with no
target named, ask which (Confluence, the Sheet, or both) rather than
defaulting silently — don't assume Confluence just because it was built
first. If they've already told you a preference earlier in this
conversation, don't ask again.

1. Get known builds for every target you're about to publish to (reuse
   Step 0's output if you already fetched it this session; otherwise fetch
   now, one script per target — never fetch a source you're not
   publishing to):
   ```bash
   python3 <skill_dir>/scripts/fetch_confluence_known_builds.py --out <run_dir>/known_builds_confluence.json     # if publishing to Confluence
   python3 <skill_dir>/scripts/fetch_google_sheet_known_builds.py --out <run_dir>/known_builds_sheet.json         # if publishing to the Sheet
   ```
2. If you haven't already run it for the full detected set (Step 3 says to
   scope `fetch_execution_details.py` to just the entities under
   investigation, but the case log wants everyone), run it against the full
   `fetch_failures.py` output now, with a `--known-builds-json` for every
   file from step 1:
   ```bash
   python3 <skill_dir>/scripts/fetch_execution_details.py --groups-json <fetch_failures.py output> \
     --known-builds-json <run_dir>/known_builds_confluence.json \
     --known-builds-json <run_dir>/known_builds_sheet.json \
     --out <run_dir>/trtllm-execution-details-<date>.json
   ```
   (Drop whichever `--known-builds-json` flag doesn't apply if you're only
   publishing to one target.) Because already-known builds are skipped,
   this output's `executions` lists are *incremental* (only builds not yet
   published anywhere in the sources you passed). That's safe to feed
   straight into flat mode: `build_confluence_cases.py` omits the four
   execution-derived fields entirely for a case with nothing fresh to
   report (rather than writing "?"), and both `post_confluence_cases.py`
   and `post_google_sheet_cases.py` leave an existing row's cell alone when
   a field is absent from the case JSON — so a re-publish never clobbers
   previously-recorded detail with placeholders on either target.
   **Nested mode is different**: it always fully replaces the whole table,
   so publishing an incremental (skip-filtered) set that way would
   silently drop every already-published row not present in this run's
   data. `build_confluence_cases.py --mode nested` refuses to run against a
   `--known-builds-json`-filtered executions file for exactly this reason —
   if you want the nested breakdown, re-run `fetch_execution_details.py`
   for the target entities *without* `--known-builds-json` first, to get
   the complete picture. (This also means: if the user wants nested
   Confluence *and* the Sheet in the same run, run
   `fetch_execution_details.py` twice — once with no known-builds filter
   feeding `--mode nested` for Confluence, once with the Sheet's
   known-builds filter feeding flat mode for the Sheet.)
3. Get each entity's recovery status — whether it's stopped failing since
   detection (see `fetch_latest_status.py`'s docstring for the exact
   "passed continuously" definition: the most recent 3 executions in the
   last day, or all of them if fewer than 3 happened, all PASSED). This is
   a separate, much cheaper pass than step 2 — it only hits the stability
   dashboard (never ci_report), only needs the last day, and works for the
   full detected set every time without the pacing/known-builds concerns
   step 2 has:
   ```bash
   python3 <skill_dir>/scripts/fetch_latest_status.py --groups-json <fetch_failures.py output> --out <run_dir>/trtllm-latest-status-<date>.json
   ```
4. Build the case JSON. Nested mode only makes sense if Confluence is one
   of the targets (the Sheet can't use it); ask the user which layout they
   want for Confluence if not already clear — the nested mode multiplies
   row count by each entity's execution count (easily 3000-5000+ rows
   across the full detected set), so don't default to it without them
   asking for per-execution granularity. One case JSON build covers both
   targets when publishing flat to both:
   ```bash
   python3 <skill_dir>/scripts/build_confluence_cases.py \
     --groups-json <fetch_failures.py output> \
     --executions-json <fetch_execution_details.py output> \
     --status-json <fetch_latest_status.py output> \
     --failure-types-json <hand-written Step 4 conclusions, see below> \
     --mode flat --out <run_dir>/confluence_cases_<date>.json
   # or --mode nested for Confluence's per-PR-per-build breakdown (Confluence only)
   ```
   `--status-json` is optional but always worth including if step 3 was
   run — it adds a `Latest Status` column (a per-entity property: merged
   across a case's rows in nested mode, one cell per case in flat mode),
   omitted (not defaulted) for any entity step 3 has no data for, same
   "absent means don't touch this cell" convention as the execution-derived
   fields. It also drives the **Analyzed** column: a sticky, manually-edited
   flag ("has someone reviewed this case") that defaults to `"False"` for a
   case that's never been published before, and is otherwise left exactly
   as it already is on the page — EXCEPT when `fetch_latest_status.py`
   reports `regressed_since_pass` for that entity (it was passing and has
   failed again), in which case it's forced back to `"False"` regardless of
   what was there, since a fresh failure after a pass needs re-review even
   if someone already marked it analyzed. Never write anything but
   `"False"` to this column yourself — `"True"` is set by whoever reviews
   the case, not by this pipeline.

   `--failure-types-json` is likewise optional, and there's no script that
   generates it — write it yourself as `{"results": [{"entity_name": ...,
   "platform": ..., "failure_type": "<Step 4 label>"}, ...]}` for whichever
   entities you actually ran through Step 4 this run (skip the rest; same
   "absent means don't touch" convention). It adds a **Failure Type**
   column, merged per-case in nested mode like Latest Status/Analyzed.
5. Publish to each target the user asked for, then write back that target's
   ci-status watermark (see Step 0.5 step 4 — use the timestamp already
   fetched into `ci_status_watermark_now.json`, don't re-fetch it):
   ```bash
   python3 <skill_dir>/scripts/post_confluence_cases.py --cases-json <run_dir>/confluence_cases_<date>.json      # flat
   python3 <skill_dir>/scripts/post_confluence_cases.py --nested-json <run_dir>/confluence_cases_<date>.json      # nested
   python3 <skill_dir>/scripts/sync_confluence_watermark.py --write <timestamp-from-ci_status_watermark_now.json>
   python3 <skill_dir>/scripts/post_google_sheet_cases.py --cases-json <run_dir>/confluence_cases_<date>.json     # Sheet (flat only)
   python3 <skill_dir>/scripts/sync_sheet_watermark.py --write <timestamp-from-ci_status_watermark_now.json>
   ```
   **Confluence** (`post_confluence_cases.py`): flat mode reads the current
   page via the Confluence Cloud REST API, matches each case against
   existing rows by (case name, PR number): a match gets the new date +
   analysis **appended** to its existing row (preserving prior history, not
   overwritten) and its status cells (Waived, Waived (Executions), Related
   Bugs, Stack Trace, Build-PR Mapping, Latest Status, Analyzed)
   **overwritten** with the latest values *when the case JSON provides
   them* (a cell whose field is absent from the case dict — the normal
   state for Analyzed outside a regression — is left exactly as it already
   is, never reset to a placeholder); a non-match becomes a new row. Nested
   mode is different for Analyzed specifically: since it always fully
   replaces the table (see below), before rebuilding it reads the current
   page's existing Analyzed values (rowspan-reconstructed per case) and
   carries them forward for any case that isn't forcing a reset — the one
   piece of "existing page state" nested mode's otherwise-stateless rebuild
   depends on.

   In nested mode, **Date is per-build-row, not case-level** — unlike Case
   name/Latest Status/Analyzed (which are rowspan-merged once per case),
   every build row gets its own Date from that specific execution's own
   run timestamp (`build_confluence_cases.py`'s `execution_date()`), not a
   single "when this report was built" stamp shared across the whole case.
   Flat mode's `Date` field keeps its original meaning (the run/publish
   date, used for the Failure analysis history log) — this only applies to
   nested mode's table.

   The table's column layout is read from the page's own header row — if it's
   missing any of the columns this script knows about, the header and every
   existing row are migrated in place (`"?"` placeholders on old rows)
   before merging in the new cases, so nothing on the page is lost. Nested
   mode always fully replaces the table (rowspan-merged cells can't be
   sensibly "appended to"). Needs credentials at
   `~/.config/confluence/credentials.json` (JSON: `{"email": "...",
   "api_token": "..."}` — an Atlassian API token from
   https://id.atlassian.com/manage-profile/security/api-tokens). If that
   file is missing, tell the user how to create it rather than guessing at
   credentials.

   **Google Sheet** (`post_google_sheet_cases.py`): same match/append/
   overwrite semantics as Confluence's flat mode, applied to a plain grid
   (whole range read, merged in memory, written back in one call — there's
   no per-row API call to make on Sheets the way there is with Confluence's
   HTML table). Three supported auth paths, tried in this order:
   1. An **Apps Script webhook** — a config file at
      `~/.config/gsheets/apps_script.json` (`{"url": "...", "token": "..."}`)
      pointing at a small script (`scripts/apps_script/Code.gs`) deployed
      bound to the target spreadsheet. This runs under the sheet owner's
      own Apps Script execution context, so it needs **no GCP project
      permissions at all** — reach for this when the two paths below are
      blocked by org IAM policy (no permission to create service accounts,
      enable APIs, or self-assign an ADC quota project — all real corporate
      lockdowns this has hit in practice). Deployment is a one-time manual
      step by whoever can edit the sheet (paste the script, deploy as a web
      app, share the URL + a random token) — walk the user through
      `Code.gs`'s header comment rather than improvising different Apps
      Script code.
   2. A GCP service account JSON key at
      `~/.config/gsheets/service_account.json`, with that service account's
      `client_email` given Editor access to the sheet.
   3. The user's own `gcloud` credentials, re-scoped for Sheets (one-time:
      `gcloud auth application-default login
      --scopes=openid,...,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets`
      — `cloud-platform` is mandatory alongside any custom scope for
      gcloud's default OAuth client, not optional) — this additionally
      needs a *quota project* set for Application Default Credentials,
      which is itself something orgs can lock down; if it is, that's a sign
      to fall back to path 1.

   The plain `gcloud auth` token already active on a machine for unrelated
   `gcloud` use does **not** carry Sheets scope by default — don't assume
   it'll work without the explicit re-scoped login. If none of the three
   paths are set up, the script prints exact setup steps for all of them —
   tell the user rather than guessing or improvising a workaround, and
   don't burn time retrying GCP IAM commands once one has come back
   `PERMISSION_DENIED` — that's a signal to move to path 1, not to try
   variations of the same blocked command.
6. Confirm with the user before publishing to a target for the first time
   in the conversation, or if a Confluence run would migrate the table
   schema or replace it (nested mode) — posting to a shared page/sheet is a
   visible, hard-to-undo action. Use `--dry-run` first (on either publish
   script) to show them exactly what rows will be
   added/updated/migrated/replaced if there's any doubt. If publishing to
   both targets, this applies to each independently — confirming for
   Confluence doesn't cover the Sheet or vice versa.

## Step 8: Notify people on Slack (only if the user asks)

Same gating as Step 7: never send a Slack message as a default part of a
fetch/triage/report/publish run — only when the user explicitly asks to
notify/ping/message someone. This is independent of Step 7; the user can ask
to be notified on Slack whether or not this run also published to
Confluence/the Sheet.

There is no stored recipient list or channel for this — every send is
scoped to whoever the user names in that request (e.g. "ping Jerry about
this on Slack", "DM the DGX_B200 owners"). If the user says "notify the
owners" without naming anyone and no owner is identifiable from the
dashboard's `latest_comment` field either, ask who to message rather than
guessing an identifier.

```bash
python3 <skill_dir>/scripts/post_slack_message.py \
  --user <email-or-slack-member-id> [--user <email-or-slack-member-id> ...] \
  --message-file <run_dir>/slack_message_<date>.md \
  --dry-run   # preview recipient resolution + rendered message before actually sending
```

- `--user` accepts either a Slack member id (`U...`) or an email address
  (resolved via `users.lookupByEmail`, so it must be the person's
  nvidia.com address as registered in the workspace); pass it once per
  recipient — each gets an individual DM, not a group message.
- **Write the message to a file first** (`<run_dir>/slack_message_<date>.md`)
  and pass it with `--message-file` — multi-line quoted text does not survive
  shell interpolation reliably, and the file is the record of what was sent.
  `--message "<text>"` remains for one-liners.
- **Format: Slack mrkdwn, one block per case, one labelled field per line.**
  Slack renders its own markdown dialect, not GitHub's: `*bold*`, `_italic_`,
  `` `code` ``, `> quote`, `•` bullets, `<url|label>` links. It has **no
  headings and no tables** — do not emit `#`, `|---|` or `**double
  asterisks**` (they render literally). Escape `&`, `<`, `>` inside error
  text as `&amp;`, `&lt;`, `&gt;`, and put error text in a fenced
  ```` ``` ```` block so it is not reflowed. Template:

  ```
  *TRT-LLM main CI — <window label>* (<start UTC> → <end UTC>)
  <N> case-level Main Break groups (<H> high / <M> medium) · <S> stage-level · <W> waived

  *1. `<entity_name>`* (<platform>)
  • *Waived:* <Y (nvbugs/…) | N>
  • *Type:* <Step 4 label: Regression (PR #…) | Infra failure | Flaky test | …>
  • *Reason:* <one-sentence justification from analysis_<slug>.json>
  • *PRs / builds:* <#PR(build, …) …, or a count when long>
  • *Action:* <primary action from actions_<slug>.json>
  • *Status:* <open | recovered | fixed-awaiting-rebase | waived>
  • *Error:*
  ```
  <representative error text, ≤ 3 lines, &lt; &gt; &amp; escaped>
  ```

  *2. `<next entity_name>`* …

  _Cross-cutting:_ <shared infra or shared root cause, if any>
  _Report:_ `<run_dir>/trtllm-main-failures-report-<date>.md`
  ```

  - `<entity_name>` — the full dashboard `entity_name`; add the platform in
    parentheses when the same name exists on several platforms.
  - *Waived* — from the group's `waived` / `waive_bugs`.
  - *Type* — the Step 4 label exactly as written in `analysis_<slug>.json`.
  - *Reason* — the short justification behind that label (e.g. "31 failed
    executions across 19 unrelated PRs, no single-PR attribution" or "PR
    #18684's change to `<file>`").
  - *Action* / *Status* — from Step 5's `actions_<slug>.json` (primary
    action and `status`).
  - *Error* — the representative error from `fetch_full_error.py`'s block
    (fall back to `short_error_msg`), trimmed to the line(s) that identify
    the failure; never paste a whole traceback.
  - Order the blocks as the report orders them (top offenders first). When
    a run has no case-level entities, use the same block layout for the
    investigated stage-level entities and say so in the header line.
  - Slack truncates long messages: keep each DM under ~3,500 characters. If
    more cases exist than fit, send the header plus the top cases and end
    with `_… <k> more cases in the report_` rather than a second message,
    unless the user asked for everything.
- Needs credentials at `~/.config/slack/credentials.json` (JSON:
  `{"bot_token": "xoxb-..."}` — a Slack bot token with the `chat:write` and
  `users:read.email` scopes, from a Slack app installed to the workspace).
  If that file is missing, tell the user how to create it rather than
  guessing at a token.
- Run with `--dry-run` first and show the user the resolved recipient(s)
  and exact message text before sending for real — this is a message to a
  real person, not a preview-only artifact, so treat it with the same
  "confirm before the first send in this conversation" care as Step 7's
  publish confirmation.
