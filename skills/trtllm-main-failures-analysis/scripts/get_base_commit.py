#!/usr/bin/env python3
"""Get each CI build's base commit - the commit `main` was actually at when
the tested PR branch diverged - given a job + a set of builds.

Two commits matter per build:
- head_commit: the PR branch commit that was actually tested. Read from
  ci_report's own job JSON (`job_info.s_trigger_mr_commit`, the same
  endpoint/record fetch_execution_details.py already reads `mr` from - see
  fetch_job_data_uncached(), imported from there rather than reimplemented).
- base_commit: the merge-base of head_commit and the target repo's default
  branch, i.e. where the PR branch actually diverged.

IMPORTANT: base_commit is deliberately NOT read from the GitHub PR's own
`base.sha` / `baseRefOid` field. For an OPEN, unmerged PR that field tracks
the CURRENT tip of the base branch and moves forward automatically as the
base branch advances, independent of whether the PR branch itself was ever
rebased - it is not a stable historical reference, and using it produces
false "this PR's base is post-fix-commit X" conclusions for a PR that never
actually rebased past X (confirmed in practice: baseRefOid vs a real
merge-base disagreed for an open PR, wrongly suggesting a supposedly-fixed
bug had recurred). The merge-base (computed via GitHub's own compare API,
so this works even if a local clone is stale or missing the head commit) is
the correct, time-stable answer.

Optionally pass --fix-commit <sha> to also report, per build, whether its
base_commit precedes that commit (`base_is_pre_fix`) - the validation step
of the CI-failure-analysis workflow: a fix commit only actually explains a
build's failure if that build's base predates the fix. Also computed via the
compare API (`compare/<base>...<fix>`, status=="ahead" and behind_by==0 means
base_commit precedes fix_commit), never local git.

Input is one of:
  --executions-json <fetch_execution_details.py output> - every unique
    (job, build) pair referenced anywhere in it is resolved automatically.
  --job <job> --build <build> (each repeatable, paired positionally) - for
    a specific, hand-picked set of builds instead of a whole executions file.

Output: JSON grouped by PR, each PR's value a list of single-key
{build_id: {...}} entries (a list, not a dict, since the same build id can
in principle recur - e.g. a rerun - and a list never silently collapses that):
  {
    "<mr>": [
      {"<build_id>": {
        "head_commit": "...", "base_commit": "...",
        "base_is_pre_fix": true, "fix_candidate": "<the --fix-commit given>"
      }},
      ...
    ],
    ...
  }
A build with no triggering PR (e.g. a PostMerge build) is grouped under the
empty-string key `""`. `base_is_pre_fix` is null when --fix-commit wasn't
given, or when a lookup failed for that build. `fix_candidate` is the
--fix-commit value itself, echoed onto every entry (null if not given) -
makes each entry self-describing about which commit it was validated
against, without needing to cross-reference the invocation that produced it.

Uses `gh api` (the same NVIDIA/TensorRT-LLM-authenticated `gh` CLI already
used ad hoc by Step 4's subagents) for both the head->base and base-vs-fix
resolution - no local git clone dependency, so staleness there can't produce
wrong answers.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_execution_details as fed  # noqa: E402

DEFAULT_REPO = "NVIDIA/TensorRT-LLM"


def gh_json(args: list[str], timeout: int = 30):
    """Run `gh` with the given args and return parsed stdout JSON, or None on failure."""
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"  gh invocation failed: {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"  gh {' '.join(args)} failed: {result.stderr.strip()[:300]}", file=sys.stderr)
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out  # a --jq projection can yield a bare non-JSON scalar string


def resolve_base_commit(repo: str, head_commit: str, default_branch: str = "main") -> str | None:
    data = gh_json([
        "api", f"repos/{repo}/compare/{default_branch}...{head_commit}",
        "--jq", ".merge_base_commit.sha",
    ])
    return data if isinstance(data, str) and data else None


def is_pre_fix(repo: str, base_commit: str, fix_commit: str) -> bool | None:
    data = gh_json([
        "api", f"repos/{repo}/compare/{base_commit}...{fix_commit}",
        "--jq", "{status, ahead_by, behind_by}",
    ])
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return data.get("status") == "ahead" and data.get("behind_by", -1) == 0


def collect_job_builds_from_executions(executions_json: str) -> list[tuple[str, str]]:
    payload = json.loads(Path(executions_json).read_text())
    pairs: set[tuple[str, str]] = set()
    for result in payload.get("results", []):
        for execution in result.get("executions") or []:
            job, build = execution.get("job"), execution.get("build")
            if job and build:
                pairs.add((job, build))
    return sorted(pairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--executions-json", help="fetch_execution_details.py output; every (job, build) pair in it is resolved")
    parser.add_argument("--job", action="append", default=[], help="Job name, paired positionally with --build (repeatable)")
    parser.add_argument("--build", action="append", default=[], help="Build id, paired positionally with --job (repeatable)")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub repo to resolve commits against (default: {DEFAULT_REPO})")
    parser.add_argument("--default-branch", default="main", help="Base branch name (default: main)")
    parser.add_argument("--fix-commit", default=None, help="If given, also report base_is_pre_fix per build against this commit SHA")
    parser.add_argument("--out", default=None, help="Output JSON path (default: stdout)")
    args = parser.parse_args()

    if args.executions_json:
        job_builds = collect_job_builds_from_executions(args.executions_json)
    elif args.job and args.build:
        if len(args.job) != len(args.build):
            sys.exit("--job and --build must be given the same number of times (paired positionally)")
        job_builds = list(zip(args.job, args.build))
    else:
        sys.exit("Provide either --executions-json or paired --job/--build")

    print(f"Resolving {len(job_builds)} (job, build) pair(s)", file=sys.stderr)

    limiter = fed.RateLimiter(min_interval_s=0.5)
    results: dict[str, list[dict]] = {}
    base_cache: dict[str, str | None] = {}
    pre_fix_cache: dict[str, bool | None] = {}
    n_resolved = 0

    for i, (job, build) in enumerate(job_builds, 1):
        print(f"[{i}/{len(job_builds)}] {job} #{build}", file=sys.stderr)
        job_data = fed.fetch_job_data_uncached(job, build, limiter)
        job_info = (job_data or {}).get("job_info", {})
        mr = job_info.get("s_trigger_mr_id") or ""
        head_commit = job_info.get("s_trigger_mr_commit")

        entry: dict = {
            "head_commit": head_commit,
            "base_commit": None,
            "base_is_pre_fix": None,
            "fix_candidate": args.fix_commit,
        }

        if head_commit:
            if head_commit not in base_cache:
                base_cache[head_commit] = resolve_base_commit(args.repo, head_commit, args.default_branch)
            entry["base_commit"] = base_cache[head_commit]
        else:
            entry["note"] = "no head commit in job_info (e.g. a PostMerge build has no triggering PR)"

        if args.fix_commit and entry["base_commit"]:
            base = entry["base_commit"]
            if base not in pre_fix_cache:
                pre_fix_cache[base] = is_pre_fix(args.repo, base, args.fix_commit)
            entry["base_is_pre_fix"] = pre_fix_cache[base]

        results.setdefault(mr, []).append({build: entry})
        n_resolved += 1

    out_json = json.dumps(results, indent=2)
    if args.out:
        Path(args.out).write_text(out_json)
        print(f"Wrote base commit info for {n_resolved} build(s) across {len(results)} PR(s) to {args.out}", file=sys.stderr)
    else:
        print(out_json)


if __name__ == "__main__":
    main()
