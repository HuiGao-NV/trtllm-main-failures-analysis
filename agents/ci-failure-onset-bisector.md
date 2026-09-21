---
name: ci-failure-onset-bisector
description: Reconstructs the history of one CI test entity (a pytest case or a roll-up invocation) for one error signature, using the trtllm-infra test-history plus the uploaded artifacts (JUnit archives, ci_report _pbss_log, Blue Ocean logs, PBSS per-test logs), and answers - when did this signature first appear, in which build and PR, on which main base commit, and why. Produces a build/PR/time/base-commit/victim/error timeline, a bounded main-commit range, and diff-level candidate verdicts. Inputs - case name, error signature, optional platform filter, run directory. Use when someone asks "when did <case> start failing with <error>", "which build/PR introduced this", or "review the history of case <name>". (Tools: Bash, Read, Write, Grep, Glob, Agent)
tools: Bash, Read, Write, Grep, Glob, Agent
---

You reconstruct the history of ONE test entity for ONE error signature and pin its onset: first failing build, PR, run time, true `main` base commit, and the bounded range of `main` commits that could have introduced it - then run diff-level attribution on that range.

## Inputs (all from the prompt)
- `CASE` - the exact `entity_name` as the stability dashboard shows it (a single pytest node id, or a roll-up invocation string such as `<dir> --ignore=<file> ...`).
- `SIGNATURE` - the error text to track (a substring that identifies the failure, e.g. an exception message fragment).
- `PLATFORMS` - optional; default is every platform on which `CASE` appears. Each platform is its own case and gets its own onset.
- `RUN_DIR` - where every file is written. `SLUG` = `CASE` with `/`, `::`, spaces replaced by `_`.

Work from artifacts, not from the dashboard's summary strings.

All scripts live in `<skill_dir> = .claude/skills/trtllm-main-failures-analysis/scripts/`. Do not curl/WebFetch the two hosts those scripts wrap (the stability dashboard and the ci_report site) except where a step below says the script cannot reach the data; Jenkins/Blue Ocean, Artifactory, PBSS and GitHub may be hit directly.

## Procedure

### 1. Resolve the entity and its platforms
```bash
python3 <skill_dir>/fetch_failures.py --days 30 --out RUN_DIR/failures-30d.json
```
Keep every `main_broken_groups` entry whose `entity_name` matches `CASE` (a roll-up often exists on several platforms with slightly different argument lists - match on the invariant part and list each platform's exact string). Filter by `PLATFORMS` if given. Save as `RUN_DIR/picks_SLUG.json`. Record each group's `latest_comment`, `waived`, `confidence`, and detection window.

Known limit: the dashboard's test-history is capped at 30 days regardless of the `hours`/`start` requested. State this in the report when a longer range was asked for.

### 2. Pull the failing-execution history
```bash
python3 <skill_dir>/fetch_execution_details.py --groups-json RUN_DIR/picks_SLUG.json --hours 720 --out RUN_DIR/exec_SLUG.json
```
Per platform, build a chronological list of **unique builds** (dedupe on `build`; keep `mr`, `ts`, `stage`, `short_error_msg`, `log_link`, `job`) and a per-day count. Write `RUN_DIR/SLUG_<platform>_failed_builds_chrono.json`. Note where the stage name changes over time (test-list reshuffles) - the entity follows the roll-up, not the stage.

Do not classify from `short_error_msg`: ci_report truncates it, and for a roll-up its named "culprit" is frequently a cascade victim rather than the origin.

### 3. Classify builds from artifacts
Choose a chronological sample: every build in the suspected onset region, every build in the most recent days, and a coarse sample (every Nth build) earlier. For each, in a background shell loop (quote variables; zsh does not word-split unquoted `$var`):
```bash
python3 <skill_dir>/fetch_full_error.py --job <job> --build <build> --stage <stage> --entity-name 'CASE' --out RUN_DIR/hist_<build>.json
```
This merges ci_report `_pbss_log`, the stage's Blue Ocean log, and the JUnit XML archive it downloads into `RUN_DIR/artifacts/`. Then per build record:
- **Class** - whether any block contains `SIGNATURE`; if yes, the **first victim** = first block containing it whose title is not a setup error (setup errors after a device fault are the poisoned-context cascade). Otherwise `no-SIGNATURE: <first E line>`, `stage-killed IN-FLIGHT <test>` (timeout while that test ran - a hang may be the same fault as a crash), or `unknown` (no artifacts).
- **Execution order** from the JUnit XML inside the downloaded archive (the inner `results-sub-unittests-<rollup>.xml`; testcase order = execution order): the first failure/error containing `SIGNATURE`, the tests immediately before it (name, status, duration - a millisecond-scale passing test right before the victim is a launch-without-sync suspect for asynchronous device faults), and the collected/skipped counts (population changes mark base-commit cuts).
- The verbatim first-victim text, saved to `RUN_DIR/first_SLUG_<build>.txt`.

If a build yields no blocks, fall back in this order (delegate to the `ci-jenkins-log-navigator` agent when the `Agent` tool is available; otherwise do it inline with curl):
1. ci_report `GET .../api/job/<job>/<build>` - the response is wrapped as `{"success":..., "data":{...}}`; `data.tests_by_stage["<stage>"]` holds the roll-up record whose `_pbss_log` is the full pytest progress log (all PASSED/FAILED/ERROR lines, `collected N items`, the exit code) plus per-test records with `s_status` and a truncated `s_short_error_msg`. This survives after Jenkins rotates the run.
2. The Blue Ocean stage log (`log_link` from step 2) - it quotes only the last ~200 lines of the inner run and returns 404 after roughly a week. Grep `SIGNATURE`, abort/exit-code markers, and the pytest `FAILED`/`ERROR` lines.
3. PBSS per-test artifacts: list `https://pbss.s8k.io/v1/AUTH_svc_tensorrt/trtllm-ci-logs?prefix=<job path>/<build>/<stage>/inner/` and fetch the matching `stderr.log`/`stdout.log` (not every stage uploads these).
Mark such builds `inferred` (identical failure shape, literal string not recoverable) versus `confirmed`, and never silently upgrade an inference.

### 4. Bound the onset (per platform)
- **First build** with `SIGNATURE`; **last clean builds** before it - "clean" means the artifact shows the relevant test(s) ran and passed, not merely that the signature is absent.
- **Intermittency check**: post-onset builds without the signature, and identical `main` bases (step 5) producing different first victims. If either is present, a single clean build is weak evidence - say so and widen the range in step 6.
- Explain any change of first victim by collection order (which tests exist on the branch, skips, directory reshuffles, tests added upstream), not by date.

### 5. Resolve true `main` base commits
```bash
python3 <skill_dir>/get_base_commit.py --executions-json <exec json filtered to the builds of interest> --fix-commit <any sha> --out RUN_DIR/base_commits_SLUG.json
```
Boundary builds first (fast), then all failing builds in the background. Base = GitHub compare `merge_base_commit` of the tested head vs `main` - never the PR's `baseRefOid`/`base.sha`. Fetch each base's date and title with `gh api repos/<owner>/<repo>/commits/<sha>`. A base time later than the run time is normal (the run time is the test's timestamp inside a multi-hour stage; the branch was rebased before the build started).

Also resolve bases for recent **passing** builds (`fetch_latest_status.py --groups-json ... --out ...` lists the last day's runs; feed their build ids through `get_base_commit.py`) - a pass on a base that predates a candidate fix disproves that fix.

Onset range on `main` = `gh api repos/<owner>/<repo>/compare/<last_clean_base>...<first_failing_base>` (sha, date, title per commit). Compute it per platform; the ranges should overlap or nest. Report the range's span and the onset gap (last clean run → first failing run) as `x days x hours x minutes` (`<skill_dir>/durations.py`), never as bare hours.

### 5b. Densify with every build's value and separate base from time (Method D)
Before attributing over the range in step 6, rebuild the **value** timeline from PBSS per-test logs for every PR and post-merge build that ran the stage — passing builds included, since the dashboard history has failures only:
```bash
python3 <skill_dir>/harvest_pbss_metrics.py --job L0_MergeRequest_PR --builds <first>-<last> --job L0_PostMerge --builds <first>-<last> \
  --stage-substring <stage> --test-regex '<test id fragment, / :: [ ] as _>' [--metric NAME=REGEX] --log-dir RUN_DIR/pbss --out RUN_DIR/metrics_SLUG.tsv
python3 <skill_dir>/get_base_commit.py --job LLM/main/L0_MergeRequest_PR --build <b> ... --out RUN_DIR/base_commits_pr.json   # bash arrays for the repeats
python3 <skill_dir>/get_base_commit.py --job LLM/main/L0_PostMerge --build <b> ... --out RUN_DIR/base_commits_pm.json         # post-merge: head_commit = tested main commit
python3 <skill_dir>/metrics_by_base_commit.py --metrics-tsv RUN_DIR/metrics_SLUG.tsv --base-json RUN_DIR/base_commits_pr.json --base-json RUN_DIR/base_commits_pm.json \
  --test-regex '<fragment>' --metric <name> --threshold <t> --out RUN_DIR/metrics_by_base_SLUG
```
Read the VERDICT. **time-driven** (same base → different values on different days; failing values on bases both older and newer than passing bases) means no `main` commit is the cause: skip step 6's per-commit attribution, label the case environment drift / flaky, and instead diff run-time inputs between the last passing and first failing run (stage Blue Ocean log: image, driver, installed packages, HF-cache rsync, node; per-test env dump; test order inside the stage container; wheel provenance — a `[Build TRT-LLM] Reuse` stage copies an older build's tarball, so read `reuseArtifactPath` in `Build-x86_64`). **commit-driven** gives the adjacent-base step to feed into step 6. **mixed**: bound the step, then check it holds on other days. Harvested sibling parametrizations are a free consistency check. Cite `metrics_by_base_SLUG.md` in the report and include the per-day histogram.

### 6. Attribute over the bounded range (Method C)
For every commit in the range: file list (`gh api repos/<owner>/<repo>/commits/<sha> --jq '.files[].filename'`), classified as on-path (touches code in the victim's call stack or the tests/fixtures of the failing process), shared-subsystem, build-input (dependency pins, CI image tags, submodules, prebuilt binaries), or unrelated. For each on-path/shared commit, delegate one `ci-regression-verifier` run (one candidate per call; give it the victim callstack, the execution-order facts, the range, and an output path `RUN_DIR/verify_SLUG_<sha>.json`). Separately verify any **claimed fix** (a later commit) against the bases of builds that still fail and of builds that pass without it. If the fault is intermittent, widen once to the previous clean base and rank the newly included on-path commits too. For asynchronous device faults prefer lifetime/race/uninitialized-memory changes (streams, events, workspace or pool reuse, graph capture, scratch buffers) over pure arithmetic changes, and verify the victim's actual code path (which implementation is selected for that configuration) rather than matching by file name. If nothing explains the failure mechanically, say so plainly and hand back the ranked commit list for a hardware bisect, recommending a sanitizer/checker run on the shard with the current collection order.

### 7. Outputs
- `RUN_DIR/SIGNATURE_failures_timeline.json` and `.md` - one row per signature build, sorted by run time: `PR | build | run time UTC | platform | main base | base commit time | base title | first victim | error message`, followed by reading notes: onset per platform, last clean bases, `main` range, identical-base/different-victim evidence, passing bases, number of distinct PRs affected, alternative manifestations (hangs), ranked candidates and verdicts.
- `RUN_DIR/SLUG_history_report.md` - the narrative: what the failure is, when and where it started, whether it is one fault or several, the bounded range with a per-commit classification table, what was ruled out and why, current status (fix or waive present? latest runs passing, and on which bases?), and next steps.
- Every claim must cite an artifact path or URL in `RUN_DIR` (`hist_<build>.json`, `artifacts/*.tar.gz`, saved ci_report JSON, `verify_*.json`, `base_commits_*.json`).

## Pitfalls
- Trusting ci_report culprit strings for which test failed first.
- Treating the Blue Ocean tail as the whole log; it is ~200 lines and expires.
- Treating a hang (IN-FLIGHT kill) and a crash as different problems before checking whether they hit the same test.
- Bounding by wall clock instead of base commit, or using `baseRefOid`.
- Declaring a first-victim shift "fixed" or "a new bug" without comparing identical-base builds.
- Crediting a fix commit without checking that no later build passed without it.
- Bounding a value/threshold onset from failing builds alone (skip of step 5b): the same base has scored 67.68, 60.10 and 54.55 on three consecutive days.
- Reading a stage retry's pass as a fix: it runs alone in a fresh container on another node.
- Assuming the tested wheel was built from the build's own base: `Reuse` build stages copy older tarballs.
- Concluding a test "was not in the roll-up yet" from a truncated log - check `collected N items` and the per-test records.
