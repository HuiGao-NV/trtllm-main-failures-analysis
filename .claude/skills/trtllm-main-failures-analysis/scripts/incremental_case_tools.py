#!/usr/bin/env python3
"""Ad hoc helpers written during an incremental (same-day repeat) run of the
trtllm-main-failures-analysis skill, promoted into a reusable subcommand
script so later incremental runs don't reinvent them as one-off `python3 -c`
snippets. Each subcommand covers one recurring need from Steps 1-5 when a
window has already been investigated earlier the same day and only the
delta matters:

  diff-groups          Step 2 - which (entity_name, platform) groups are new
                        or dropped between two fetch_failures.py outputs.
  bucket-by-fix         Step 4.1/5 - split an incremental exec-details.json
                        into per-problem buckets (builds + PRs) by matching a
                        fix/waive commit prefix against each entity's
                        recorded Failure Type (fetch_confluence_case_analysis.py
                        / fetch_sheet_case_analysis.py output), so a window
                        mixing e.g. two semantic-conflict problems on shared
                        stages/builds can be told apart.
  filter-base-commits   Step 5 - narrow a get_base_commit.py output down to
                        the PR/build allowlist for one bucket, so
                        rebase_actions.py isn't fed builds resolved against
                        an unrelated fix_candidate (mixing fix commits in one
                        rebase_actions.py call silently mislabels PRs).
  gen-skip-unchanged    Step 4.1 bulk apply - for every entity in an
                        exec-details.json whose recorded Failure Type is
                        still present, write analysis_<slug>.json with
                        "method": "skip-unchanged" plus a consolidated
                        failure_types.json for build_confluence_cases.py,
                        instead of hand-writing one JSON per entity.

All subcommands are read-only / local-file only: no network calls. They
consume the JSON shapes fetch_failures.py, fetch_execution_details.py,
fetch_confluence_case_analysis.py / fetch_sheet_case_analysis.py and
get_base_commit.py already produce, and slug entity names the same way the
skill's run-directory convention does (`/`, `::`, spaces -> `_`).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def load_json(path: str):
    return json.loads(Path(path).read_text())


def write_json(path: str, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")


def slug(entity_name: str, platform: str | None = None) -> str:
    s = re.sub(r"[/\s]|::", "_", entity_name)
    return f"{s}_{platform}" if platform else s


def group_key(g: dict) -> tuple[str, str]:
    return (g.get("entity_name", ""), g.get("platform", ""))


# ---------------------------------------------------------------- diff-groups

def cmd_diff_groups(args: argparse.Namespace) -> int:
    old = load_json(args.old)["main_broken_groups"]
    new = load_json(args.new)["main_broken_groups"]
    old_keys = {group_key(g) for g in old}
    new_by_key = {group_key(g): g for g in new}
    new_keys = set(new_by_key)

    added = sorted(new_keys - old_keys)
    dropped = sorted(old_keys - new_keys)
    kept = sorted(new_keys & old_keys)

    out = {
        "old_count": len(old_keys), "new_count": len(new_keys),
        "added": [{"entity_name": k[0], "platform": k[1]} for k in added],
        "dropped": [{"entity_name": k[0], "platform": k[1]} for k in dropped],
        "kept": [{"entity_name": k[0], "platform": k[1]} for k in kept],
    }
    write_json(args.out, out)
    print(f"old={len(old_keys)} new={len(new_keys)} added={len(added)} "
          f"dropped={len(dropped)} kept={len(kept)}", file=sys.stderr)
    for k in added:
        print(f"  + {k[0]} ({k[1]})", file=sys.stderr)
    for k in dropped:
        print(f"  - {k[0]} ({k[1]})", file=sys.stderr)
    return 0


# --------------------------------------------------------------- bucket-by-fix

def lookup_failure_type(case_analysis: dict, entity_name: str, platform: str) -> str:
    k = f"{entity_name} ({platform})"
    if k in case_analysis:
        return case_analysis[k].get("failure_type", "")
    return case_analysis.get(entity_name, {}).get("failure_type", "")


def cmd_bucket_by_fix(args: argparse.Namespace) -> int:
    exec_d = load_json(args.exec_json)
    case_analysis = load_json(args.case_analysis_json).get("case_analysis", {})

    buckets: dict[str, dict] = {
        label: {"match": match, "groups": [], "prs": set(), "builds": set(), "build_pr": {}}
        for label, match in args.bucket
    }
    unmatched: list[dict] = []

    for r in exec_d["results"]:
        ent, plat = r["entity_name"], r["platform"]
        ft = lookup_failure_type(case_analysis, ent, plat)
        hit_labels = [label for label, b in buckets.items() if b["match"] in ft]
        if not hit_labels:
            unmatched.append({"entity_name": ent, "platform": plat, "failure_type": ft})
            continue
        build_pr = r.get("build_pr_map", {})
        for label in hit_labels:
            b = buckets[label]
            b["groups"].append({"entity_name": ent, "platform": plat})
            for e in r.get("executions", []):
                bid = e["build"]
                b["builds"].add(bid)
                pr = build_pr.get(bid)
                if pr:
                    b["prs"].add(pr)
                    b["build_pr"][bid] = pr

    out = {}
    for label, b in buckets.items():
        out[label] = {
            "match": b["match"],
            "group_count": len(b["groups"]),
            "groups": b["groups"],
            "prs": sorted(b["prs"], key=lambda x: int(x) if x.isdigit() else 0),
            "builds": sorted(b["builds"], key=lambda x: int(x) if x.isdigit() else 0),
            "build_pr": b["build_pr"],
        }
        print(f"  [{label}] match={b['match']!r}: {len(b['groups'])} group(s), "
              f"{len(b['builds'])} build(s), {len(b['prs'])} PR(s)", file=sys.stderr)
    if unmatched:
        out["_unmatched"] = unmatched
        print(f"  {len(unmatched)} entit(y/ies) matched no bucket (see _unmatched)", file=sys.stderr)
    write_json(args.out, out)
    return 0


# ---------------------------------------------------------- filter-base-commits

def cmd_filter_base_commits(args: argparse.Namespace) -> int:
    data = load_json(args.base_commits_json)
    builds_allow = set(args.build) if args.build else None
    prs_allow = {p.lstrip("#") for p in args.pr} if args.pr else None
    if args.bucket_json:
        bucket = load_json(args.bucket_json)
        if args.bucket_label:
            bucket = bucket[args.bucket_label]
        b = set(bucket.get("builds", []))
        builds_allow = (builds_allow | b) if builds_allow else b
        p = set(bucket.get("prs", []))
        prs_allow = (prs_allow | p) if prs_allow else p
    if not builds_allow and not prs_allow:
        print("nothing to filter by: give --build/--pr/--bucket-json", file=sys.stderr)
        return 1

    out: dict[str, list] = {}
    kept = 0
    for pr, entries in data.items():
        if prs_allow is not None and pr and pr not in prs_allow:
            continue
        keep_entries = []
        for entry in entries:
            for bid, rec in entry.items():
                if builds_allow is not None and bid not in builds_allow:
                    continue
                keep_entries.append({bid: rec})
                kept += 1
        if keep_entries:
            out[pr] = keep_entries
    write_json(args.out, out)
    print(f"kept {kept} build record(s) across {len(out)} PR key(s)", file=sys.stderr)
    return 0


# --------------------------------------------------------- gen-skip-unchanged

def cmd_gen_skip_unchanged(args: argparse.Namespace) -> int:
    exec_d = load_json(args.exec_json)
    case_analysis = load_json(args.case_analysis_json).get("case_analysis", {})
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    written, skipped = [], []
    for r in exec_d["results"]:
        ent, plat = r["entity_name"], r["platform"]
        k = f"{ent} ({plat})"
        rec = case_analysis.get(k) or case_analysis.get(ent)
        if rec is None:
            skipped.append({"entity_name": ent, "platform": plat, "reason": "no recorded Failure Type"})
            continue
        ft = rec.get("failure_type", "")
        sig = rec.get("signature", "")
        analysis = {
            "entity_name": ent, "platform": plat, "method": "skip-unchanged",
            "category": ft,
            "attributed_pr": None, "attributed_commit": None, "fix_commit": None,
            "reasoning": (f"Recorded Failure Type unchanged: {ft}. New executions this cycle "
                          f"repeat the identical error signature ({sig[:150]}); no new "
                          f"investigation needed."),
            "confidence": "high",
            "waived": "waiv" in ft.lower(),
            "evidence_files": [str(Path(args.exec_json)), str(Path(args.case_analysis_json))],
        }
        s = slug(ent, plat)
        write_json(str(run_dir / f"analysis_{s}{args.suffix}.json"), analysis)
        written.append(analysis)

    failure_types = {
        "results": [
            {"entity_name": a["entity_name"], "platform": a["platform"],
             "failure_type": a["category"], "analysis": a["reasoning"]}
            for a in written
        ]
    }
    write_json(args.failure_types_out, failure_types)
    print(f"wrote {len(written)} analysis_<slug>{args.suffix}.json file(s) under {run_dir} "
          f"+ {args.failure_types_out}; {len(skipped)} entit(y/ies) had no recorded Failure Type "
          f"(need fresh Methods A-D)", file=sys.stderr)
    for s in skipped:
        print(f"  ! no recorded type: {s['entity_name']} ({s['platform']})", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("diff-groups", help="new/dropped (entity_name, platform) groups between two fetch_failures.py outputs")
    p.add_argument("--old", required=True, help="earlier fetch_failures.py --out JSON")
    p.add_argument("--new", required=True, help="latest fetch_failures.py --out JSON")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_diff_groups)

    p = sub.add_parser("bucket-by-fix", help="split exec-details.json builds/PRs by recorded fix/waive commit per entity")
    p.add_argument("--exec-json", required=True, help="fetch_execution_details.py --out JSON")
    p.add_argument("--case-analysis-json", required=True, help="fetch_confluence_case_analysis.py / fetch_sheet_case_analysis.py --out JSON")
    p.add_argument("--bucket", action="append", nargs=2, metavar=("LABEL", "MATCH_SUBSTRING"), required=True,
                   help="repeatable; e.g. --bucket P20 9f26cc3f --bucket P21 fd39e00a "
                        "(MATCH_SUBSTRING is looked up as a substring of the entity's recorded Failure Type)")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_bucket_by_fix)

    p = sub.add_parser("filter-base-commits", help="narrow a get_base_commit.py output to one PR/build allowlist")
    p.add_argument("--base-commits-json", required=True, help="get_base_commit.py --out JSON")
    p.add_argument("--build", action="append", default=[], help="repeatable build id to keep")
    p.add_argument("--pr", action="append", default=[], help="repeatable PR number to keep")
    p.add_argument("--bucket-json", help="a bucket-by-fix --out JSON to source builds/PRs from")
    p.add_argument("--bucket-label", help="label key inside --bucket-json (omit if the file is a single bucket dict)")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_filter_base_commits)

    p = sub.add_parser("gen-skip-unchanged", help="bulk-apply Step 4.1 skip-unchanged: write analysis_<slug>.json + failure_types.json")
    p.add_argument("--exec-json", required=True, help="fetch_execution_details.py --out JSON")
    p.add_argument("--case-analysis-json", required=True, help="fetch_confluence_case_analysis.py / fetch_sheet_case_analysis.py --out JSON")
    p.add_argument("--run-dir", required=True, help="directory to write analysis_<slug>.json files into")
    p.add_argument("--suffix", default="", help="filename suffix before .json, e.g. _c")
    p.add_argument("--failure-types-out", required=True, help="consolidated failure_types.json for build_confluence_cases.py --failure-types-json")
    p.set_defaults(func=cmd_gen_skip_unchanged)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
