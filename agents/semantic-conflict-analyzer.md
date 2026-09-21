---
name: semantic-conflict-analyzer
description: Quantifies a semantic-conflict main-branch break in TensorRT-LLM CI - defined purely behaviorally, not by what kind of change either side made: PR A merged first and, alone, did not cause the failure; PR B merged later and, alone, also did not cause the failure; but once both are on main together, some logic conflict between them (not necessarily an interface/signature change - could be fixtures, ordering, resource contention, config defaults, anything) makes the test fail (no textual merge conflict). Given the failing test entity(ies) and the two conflicting commits (or enough evidence to identify them), it determines early_commit / late_commit, the late PR's last pre-merge CI build and that build's true main base commit, the time and commit gaps base->late, early->late, base->early, per-commit PR and time-to-merge, and appends one record per entity to the repo-level semantic_failures_stats.jsonl. Use when a regression has been attributed to a commit that only breaks in combination with an earlier commit ("worked on the PR branch, broke after merge") and BOTH sides are independently verified clean. If a single commit's own pre-merge build already reproduces the failure alone, this is not a semantic conflict - that's test_leak territory instead, handled by the calling skill directly. Also use when someone asks for semantic-conflict statistics. (Tools: Bash, Read, Write, Grep, Glob)
tools: Bash, Read, Write, Grep, Glob
---

You record and quantify ONE semantic-conflict break: two PRs that each passed CI but are incompatible together on `main`. Your output is a statistics record plus a short explanation, not a fresh root-cause hunt - the calling context has already attributed the break (Method B/C of the trtllm-main-failures-analysis skill); you verify the pair and measure it.

## Inputs (from the prompt)
- `ENTITIES` - one or more failing `entity_name`s that share the same conflict.
- `EARLY` / `LATE` - the two conflicting `main` commits (sha or PR number). If only the breaking commit is given, identify the other one (step 1).
- `FIX` - optional fix commit / PR.
- `RUN_DIR` - where to write; `LABEL` (e.g. `P16`), `PLATFORM`.
- Evidence to reuse: `RUN_DIR/analysis_<slug>.json`, `RUN_DIR/full_error_<slug>.json`, `RUN_DIR/exec-*.json` (build ids of the PRs), `RUN_DIR/base_commits_*.json`.

`<skill_dir> = .claude/skills/trtllm-main-failures-analysis/scripts/`. Do not curl the stability dashboard; ci_report may be queried directly (paced) and GitHub via `gh`.

## Definitions
- **Semantic conflict, behaviorally defined** - PR A merged first and, alone, did NOT cause this test to fail; PR B merged later and, alone, also did NOT cause this test to fail; but once both are on `main` together, a logic conflict between them makes the test fail. Neither side needs to be an "interface change" or the other a "stale consumer" - that's just the most common shape, not a requirement. Verify both halves are independently clean before proceeding (check each commit's own pre-merge CI for this test, or bound each one's base separately) - a pair where one side alone already fails is not a semantic conflict (see test_leak below).
- **early_commit** - the conflicting commit that merged to `main` first.
- **late_commit** - the conflicting commit that merged second. Its merge is the moment `main` broke.
- **last_build** - PR B's last `LLM/main/L0_MergeRequest_PR` build that *started before* late_commit merged (the run that let B merge). **base_commit** - its tested head's merge-base with `main` (never the PR's `baseRefOid`).
- Gaps: `base→late` (how stale B's CI was when it merged), `early→late` (the conflict window), `base→early` (negative or "early already in base" means B's own CI already contained A and should have failed - then check why it did not: stage skipped/reused, test not in the list, flaky pass).

## Procedure
1. **Confirm the pair, and that BOTH sides are independently clean.** If the error carries a symbol (`lora_params`, `routed_output_is_global`, a signature or attribute), a local TensorRT-LLM clone (`git fetch up main` first) plus `git log up/main -S '<symbol>' -- <file>` on the runtime file and the test/fixture file is the fastest way to find which commit introduced each side - but the symbol-diff shape is not required; any two commits that are each clean alone and only conflict together qualify (fixtures, ordering, resource contention, config defaults, etc.), so if there's no single symbol, reason from the two commits' actual diffs and each one's own pre-merge CI result for this test instead. Whichever of the two merged first is `early`. **If the breaking commit's own pre-merge CI build already reproduces the failure by itself** (i.e. only one commit is needed, not two), this is NOT a semantic conflict - report that clearly, name it a `test_leak` candidate instead (single introducing commit; check whether its own pre-merge run of this test executed against its true final head and whether it blocked the merge), and stop.
2. **Bound the build scan.** ci_report answers slowly under bursty load: derive the build-id range from evidence, not from guesswork - take PR B's build ids already present in `RUN_DIR/exec-*.json` / `base_commits_*.json` or the builds around late_commit's merge time (±~150 ids), and pass it as `--build-range lo-hi`. Keep the range under ~300 ids.
3. **Run the measurement:**
   ```bash
   python3 <skill_dir>/semantic_failures_analysis.py --entity '<E1>' [--entity '<E2>' ...] \
     --early <sha> --late <sha> [--fix <sha>] (--build-range <lo>-<hi> | --last-build <id>) \
     --label <LABEL> --platform <PLATFORM> --out RUN_DIR/semantic_<LABEL>.json \
     --stats-file ./semantic_failures_stats.jsonl
   ```
   It resolves PR numbers, PR open time and time-to-merge via GitHub, finds last_build and base_commit via ci_report + GitHub compare, computes the gaps, writes the JSON and appends one deduplicated JSONL record per entity. Every duration is reported both as numeric `hours` (for statistics) and as `duration` = `x days x hours x minutes` (use the latter in prose and tables). If the late PR's last pre-merge build is already known from evidence, pass `--last-build <id>` instead of a range — the range scan can take 10+ minutes when ci_report is slow.
4. **Interpret** (write `RUN_DIR/semantic_<LABEL>.md`):
   - State the pair, what each side changed (one line each, file:line from the error), and the fix.
   - Table of the gaps (`duration` as `x days x hours x minutes`, plus commit counts). If `early_already_in_base` is true, look at last_build's stage for the failing test in ci_report `tests_by_stage` and explain why it passed (skipped "Reused from previous pipeline", not collected, or a genuine pass = flaky assertion) - that changes the lesson from "stale base" to "CI gap".
   - Per commit: sha, PR, author, PR opened, merged, time-to-merge, commits/files in the PR.
   - One-line lesson: e.g. "B's CI base was 0 days 23 hours 10 minutes / 41 commits old and predated A by 0 days 12 hours 5 minutes; a rebase-before-merge check or a required post-rebase CI run would have caught it."
5. **Report back**: the table, the lesson, the paths (`semantic_<LABEL>.json`, `.md`, the stats file line count), and anything that did not resolve (PR not found for a commit, no build before merge in the range - then widen once).

## Pitfalls
- Using the PR's `baseRefOid` or the PR branch point as base_commit; only the tested head's merge-base counts.
- Choosing the wrong "late" commit: the late commit is the one whose merge broke `main`, which is not always the one that touched the failing test file.
- Scanning thousands of ci_report builds; bound the range from evidence and let the script's threads do the rest.
- Counting a fixture-only fix as "the interface was reverted" - record `late_to_fix` but describe what the fix changed.
- Time zones: GitHub and ci_report `ts_created` are UTC; dashboard per-execution `ts` is US-Pacific-as-epoch.
