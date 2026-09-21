#!/usr/bin/env python3
"""Semantic-conflict failure statistics (Method "semantic_failures_analysis").

A *semantic conflict* is a main-branch break caused by two independently green
PRs whose changes are incompatible once both are on `main` (no textual merge
conflict, so Git merges them silently). Typical shape: PR A changes an
interface / call signature; PR B, branched before A merged, adds code or test
fixtures written against the old interface; B merges after A and main breaks.

For each failing test entity this script records, from the two conflicting
commits:
  early_commit  = the conflicting commit that merged first (PR A)
  late_commit   = the conflicting commit that merged second (PR B, the one whose
                  merge actually broke main)
  last_build    = the last L0_MergeRequest_PR build of PR B that started BEFORE
                  late_commit merged (the CI run whose green light let B merge)
  base_commit   = true `main` merge-base of last_build's tested head
and computes
  base_commit -> late_commit  : time gap and commit count (how stale B's CI was)
  early_commit -> late_commit : time gap and commit count (the conflict window)
  base_commit -> early_commit : time gap and commit count; NEGATIVE count means
                                early_commit was already in last_build's base
                                (then B's own CI should have caught it -> the
                                stage was skipped/reused or the test not listed)
plus, per commit: sha, PR, PR title, merge time, PR created time, time-to-merge.

Commit -> PR and PR metadata come from GitHub (`gh api`); builds of PR B come
from ci_report (`job_info.s_trigger_mr_id`, `ts_created`, `s_trigger_mr_commit`)
by scanning a build-id range; base_commit uses the same merge-base rule as
get_base_commit.py (GitHub compare of head vs main, never the PR's baseRefOid).

Usage:
  semantic_failures_analysis.py --entity '<test id>' [--entity ...] \
      --early <sha> --late <sha> (--build-range 60480-60720 | --last-build 60705) \
      [--fix <sha>] [--label P16] --out <run_dir>/semantic_<label>.json \
      --stats-file <repo>/semantic_failures_stats.jsonl

Each entity produces one JSON record; records are appended (deduplicated on
entity+late_commit) to --stats-file so statistics accumulate across days.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from durations import human, parse_iso  # noqa: E402

REPO = "NVIDIA/TensorRT-LLM"
CI_REPORT = "http://tensorrt-llm.tensorrt-llm-ci-report.sc2-paas.nvidia.com/api/job/LLM%2Fmain%2FL0_MergeRequest_PR/"


def gh(args, default=None):
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return default
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout.strip()


def commit_info(sha: str) -> dict:
    c = gh([f"repos/{REPO}/commits/{sha}"], {}) or {}
    prs = gh([f"repos/{REPO}/commits/{sha}/pulls"], []) or []
    pr = prs[0] if prs else {}
    pr_num = pr.get("number")
    pr_full = gh([f"repos/{REPO}/pulls/{pr_num}"], {}) if pr_num else {}
    merged = c.get("commit", {}).get("committer", {}).get("date")
    created = pr_full.get("created_at")
    ttm_h = None
    if merged and created:
        ttm_h = round((iso(merged) - iso(created)).total_seconds() / 3600, 1)
    return dict(sha=c.get("sha", sha), short=c.get("sha", sha)[:10], merged_at=merged,
                title=c.get("commit", {}).get("message", "").split("\n")[0],
                pr=pr_num, pr_title=pr_full.get("title"), pr_created_at=created,
                pr_author=(pr_full.get("user") or {}).get("login"),
                pr_commits=pr_full.get("commits"), pr_changed_files=pr_full.get("changed_files"),
                time_to_merge_hours=ttm_h, time_to_merge=human(ttm_h))


def iso(s: str) -> dt.datetime:
    return parse_iso(s)


def compare(a: str, b: str) -> dict:
    """Commits from a to b along main: positive = b is ahead of a."""
    d = gh([f"repos/{REPO}/compare/{a}...{b}"], {}) or {}
    return dict(status=d.get("status"), ahead_by=d.get("ahead_by"), behind_by=d.get("behind_by"),
                merge_base=(d.get("merge_base_commit") or {}).get("sha", "")[:10])


def gap(a_time: str, b_time: str) -> float | None:
    if not a_time or not b_time:
        return None
    return round((iso(b_time) - iso(a_time)).total_seconds() / 3600, 1)


def ci_build(b: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"{CI_REPORT}{b}", timeout=60) as r:
            d = json.load(r).get("data", {})
    except Exception:  # noqa: BLE001
        return None
    ji = d.get("job_info") or {}
    if not ji:
        return None
    return dict(build=str(b), pr=ji.get("s_trigger_mr_id"), head=ji.get("s_trigger_mr_commit"),
                created=dt.datetime.fromtimestamp(int(ji.get("ts_created", 0)) / 1000, dt.UTC).isoformat()
                if ji.get("ts_created") else None, status=ji.get("s_status"))


def last_build_before(pr: str, before: str, build_range: str) -> dict | None:
    lo, hi = (int(x) for x in build_range.split("-"))
    with cf.ThreadPoolExecutor(8) as ex:
        builds = [x for x in ex.map(ci_build, range(lo, hi + 1)) if x and str(x["pr"]) == str(pr)]
    before_t = iso(before)
    cands = [x for x in builds if x["created"] and iso(x["created"]) <= before_t]
    return max(cands, key=lambda x: x["created"]) if cands else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entity", action="append", required=True, help="failing test entity_name (repeatable)")
    ap.add_argument("--early", required=True, help="sha of the conflicting commit that merged first")
    ap.add_argument("--late", required=True, help="sha of the conflicting commit that merged second (broke main)")
    ap.add_argument("--fix", default=None, help="sha of the fix commit, if any")
    ap.add_argument("--build-range", default=None, help="ci_report build-id range to scan for the late PR's builds, e.g. 60480-60720")
    ap.add_argument("--last-build", default=None, help="known last pre-merge L0_MergeRequest_PR build of the late PR (skips the ci_report scan)")
    ap.add_argument("--label", default=None, help="problem id / label, e.g. P16")
    ap.add_argument("--platform", default=None)
    ap.add_argument("--out", required=True, help="JSON output for this analysis")
    ap.add_argument("--stats-file", default=None, help="JSONL file to append one record per entity (dedup on entity+late)")
    a = ap.parse_args()

    early, late = commit_info(a.early), commit_info(a.late)
    if early["merged_at"] and late["merged_at"] and iso(early["merged_at"]) > iso(late["merged_at"]):
        print("note: --early merged after --late; swapping", file=sys.stderr)
        early, late = late, early
    fix = commit_info(a.fix) if a.fix else None

    if a.last_build:
        lb = ci_build(int(a.last_build))
    elif a.build_range and late["pr"]:
        lb = last_build_before(str(late["pr"]), late["merged_at"], a.build_range)
    else:
        ap.error("give --last-build or --build-range")
    base_sha = None
    if lb and lb.get("head"):
        base_sha = (compare("main", lb["head"]).get("merge_base") or None)
        # compare() truncates; fetch full sha
        full = gh([f"repos/{REPO}/compare/main...{lb['head']}", "--jq", ".merge_base_commit.sha"])
        base_sha = full if isinstance(full, str) else base_sha
    base = commit_info(base_sha) if base_sha else None

    rel = {}
    if base:
        c = compare(base["sha"], late["sha"])
        g = gap(base["merged_at"], late["merged_at"])
        rel["base_to_late"] = dict(hours=g, duration=human(g), commits=c["ahead_by"], compare=c)
        c2 = compare(base["sha"], early["sha"])
        # if early is an ancestor of base, GitHub reports behind_by>0 / status "behind": report negative commits
        n = c2["ahead_by"] if c2.get("status") in ("ahead", "diverged") else (-(c2["behind_by"] or 0) if c2.get("status") == "behind" else 0)
        g2 = gap(base["merged_at"], early["merged_at"])
        rel["base_to_early"] = dict(hours=g2, duration=human(g2), commits=n, compare=c2,
                                   early_already_in_base=(c2.get("status") in ("behind", "identical")))
    c3 = compare(early["sha"], late["sha"])
    g3 = gap(early["merged_at"], late["merged_at"])
    rel["early_to_late"] = dict(hours=g3, duration=human(g3), commits=c3["ahead_by"], compare=c3)
    if fix:
        g4 = gap(late["merged_at"], fix["merged_at"])
        rel["late_to_fix"] = dict(hours=g4, duration=human(g4), commits=compare(late["sha"], fix["sha"])["ahead_by"])

    records = []
    for ent in a.entity:
        records.append(dict(label=a.label, entity_name=ent, platform=a.platform, analyzed_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                            early_commit=early, late_commit=late, fix_commit=fix,
                            last_build_of_late_pr=lb, base_commit=base, relations=rel))
    json.dump(dict(records=records), open(a.out, "w"), indent=1)

    if a.stats_file:
        existing = []
        if os.path.exists(a.stats_file):
            existing = [json.loads(l) for l in open(a.stats_file) if l.strip()]
        keys = {(r["entity_name"], r["late_commit"]["sha"]) for r in existing}
        with open(a.stats_file, "a") as f:
            for r in records:
                if (r["entity_name"], r["late_commit"]["sha"]) not in keys:
                    f.write(json.dumps(r) + "\n")

    # human summary
    def fmt(c):
        return f"{c['short']} (#{c['pr']}, merged {c['merged_at']}, PR opened {c['pr_created_at']}, time-to-merge {c['time_to_merge']}) {c['title'][:70]}"
    print(f"early_commit : {fmt(early)}")
    print(f"late_commit  : {fmt(late)}")
    if fix:
        print(f"fix_commit   : {fmt(fix)}")
    if lb:
        print(f"last_build   : L0_MergeRequest_PR {lb['build']} (PR #{lb['pr']}, head {lb['head'][:10]}, created {lb['created']}, {lb['status']})")
    if base:
        print(f"base_commit  : {fmt(base)}")
    for k, v in rel.items():
        print(f"{k:14}: {v.get('duration')} ({v.get('hours')} h), {v.get('commits')} commits" + ("  (early already in base)" if v.get("early_already_in_base") else ""))
    print(f"entities     : {len(records)} -> {a.out}" + (f" (+ {a.stats_file})" if a.stats_file else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
