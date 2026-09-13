#!/usr/bin/env python3
"""Turn fetch_failures.py / fetch_execution_details.py output into the case
JSON that post_confluence_cases.py publishes. This is the one place that
knows the display/aggregation rules (how a `waived` bool+bug list becomes a
"Y (nvbugs/...)" string, how per-execution records get grouped by PR/build,
etc.) - it does no network access itself, only reads the JSON those two
fetch scripts already wrote.

Two output modes:
  --mode flat (default): one row per entity, matching
    post_confluence_cases.py --cases-json. If --executions-json is given,
    each case is additionally enriched with waived_executions/related_bugs/
    stack_trace/build_pr_mapping aggregated across that entity's executions.
    If an entity has no fresh executions to report (no --executions-json,
    or every one of its builds was already known and skipped - see
    fetch_execution_details.py --known-builds-json), those four fields are
    left OUT of the case entirely rather than set to "?": post_confluence_cases.py
    treats an absent field as "nothing new to say" and leaves that cell as
    it already is on an existing row, instead of clobbering it.
  --mode nested: one row per (entity, PR, build_id), matching
    post_confluence_cases.py --nested-json. Requires --executions-json,
    since the whole point is per-execution detail. Stage-kind entities (no
    test-history / execution data) get one row per known PR with an N/A note
    instead of build-level detail. Refuses to run against an
    --executions-json that used --known-builds-json (skipped some builds),
    since nested mode always fully replaces the table - publishing an
    incremental set that way would silently drop already-published rows.

If --status-json is given (from fetch_latest_status.py), every case also
gets a `latest_status` field - a per-entity property (not per-execution), so
it's set once per case in both modes regardless of PR/build breakdown.
Omitted (not defaulted) when no status data exists for that entity, same
"absent means don't touch this cell" convention as the other optional fields.

When --status-json also reports `regressed_since_pass` (the entity was
passing and has failed again), the case additionally gets `analyzed: "False"`
- forcing the case log's Analyzed column back to False even if a human had
previously marked it reviewed. This is the ONLY way `analyzed` ever gets
set here; a case with no regression signal never has this field, which
post_confluence_cases.py treats as "leave whatever Analyzed value is
already there alone" (flat mode: untouched cell on update; nested mode:
carried forward from the existing page before the full rebuild). A
genuinely new case (never published before) defaults to "False" in either
mode via post_confluence_cases.py's own fallback, not something set here.

If --failure-types-json is given, every case whose entity appears in it gets
a `failure_type` field - the SKILL.md Step 4 ("Analyze failure reasons")
conclusion for that entity (e.g. "Infra failure", "Regression (PR #18684)",
"Regression (commit abc1234)", "Flaky test"), written by hand after doing
that analysis (there's no automated way to derive it here - Step 4 is
subagent/judgment work, this script only carries the result through). Same
"absent means don't touch this cell" convention as the other optional
fields: an entity this run didn't analyze simply has no `failure_type` key,
so post_confluence_cases.py leaves that cell alone on update (flat mode) or
carries forward the existing page's value (nested mode).
Format: {"results": [{"entity_name": ..., "platform": ..., "failure_type": ...}, ...]}
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path


def waived_str(group: dict) -> str:
    bug_ids = ",".join(b["id"] for b in group.get("waive_bugs", []) if b.get("id"))
    if group.get("waived") is True:
        return f"Y (nvbugs/{bug_ids})" if bug_ids else "Y"
    if group.get("waived") is False:
        return f"N (tracked: nvbugs/{bug_ids})" if bug_ids else "N"
    return "?"


def waived_cell(is_waived) -> str:
    if is_waived is True:
        return "Y"
    if is_waived is False:
        return "N"
    return "?"


def display_case_name(group: dict, name_counts: Counter) -> str:
    name = group["entity_name"]
    if name_counts[name] > 1:
        return f"{name} ({group.get('platform') or 'n/a'})"
    return name


def aggregate_executions(execs: list) -> tuple[str, str, str, str]:
    """-> (waived_executions, related_bugs, stack_trace, build_pr_mapping) display strings."""
    total = len(execs)
    waived_n = sum(1 for e in execs if e.get("is_waived") is True)
    waived_executions = f"{waived_n}/{total} executions waived"

    bugs = sorted({e["waive_bug_url"] for e in execs if e.get("waive_bug_url")})
    related_bugs = ", ".join(bugs) if bugs else "none"

    err_counts = Counter(e["short_error_msg"] for e in execs if e.get("short_error_msg"))
    if err_counts:
        top_err, top_count = err_counts.most_common(1)[0]
        distinct = len(err_counts)
        snippet = top_err[:500]
        extra = f" (+{distinct - 1} other distinct error(s))" if distinct > 1 else ""
        stack_trace = f"[{top_count}/{total} executions] {snippet}{extra}"
    else:
        stack_trace = "N/A (no error message recorded)"

    build_pr_map = {}
    for e in execs:
        if e.get("build") and e.get("mr") and e["build"] not in build_pr_map:
            build_pr_map[e["build"]] = e["mr"]
    if build_pr_map:
        pairs = sorted(build_pr_map.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
        build_pr_mapping = ", ".join(f"{b}:PR{p}" for b, p in pairs)
    else:
        build_pr_mapping = "none"

    return waived_executions, related_bugs, stack_trace, build_pr_mapping


def exec_key(entity_name: str, platform) -> tuple:
    """Key for looking up a group's execution-detail result. Must include
    platform: many entity_names recur across multiple platforms (e.g. the
    test_flashinfer_context_fallback_scope cluster, on DGX_B200/DGX_H100/B300
    as three separate main_broken_groups with the same entity_name) as
    distinct fetch_execution_details.py result entries - keying by
    entity_name alone collides and silently drops/misattributes 2 of every
    3 such platforms' data to whichever one a dict comprehension happens to
    keep last."""
    return (entity_name, platform)


def build_flat_cases(groups: list, exec_by_key: dict, status_by_key: dict, failure_type_by_key: dict, date: str) -> list:
    name_counts = Counter(g["entity_name"] for g in groups)
    cases = []
    for g in groups:
        obs = g.get("latest_observation") or {}
        prs = obs.get("failure_pr_identifiers") or []
        pr_number = ",".join(prs) if prs else "n/a"
        analysis = (
            f"confidence={g.get('confidence')}; platform={g.get('platform') or 'n/a'}; "
            f"failures={obs.get('failure_count')}; passes={obs.get('pass_count')}"
        )

        case = {
            "case_name": display_case_name(g, name_counts),
            "pr_number": pr_number,
            "failure_analysis": analysis,
            "waived": waived_str(g),
            "date": date,
        }

        # The four execution-derived fields are deliberately left OUT of the
        # case dict (not set to "?") unless there's something meaningful to
        # report - see post_confluence_cases.py: an absent field leaves an
        # existing row's cell untouched on update, rather than clobbering
        # previously-recorded detail with a placeholder. This matters
        # specifically when --known-builds-json caused every one of this
        # entity's executions to be skipped (already published, nothing
        # fresh) - `r` exists but `executions` is empty for a reason other
        # than "we never looked."
        r = exec_by_key.get(exec_key(g["entity_name"], g.get("platform")))
        if r is not None and r.get("skipped"):
            na = "N/A (stage entity, no test-history)"
            case["waived_executions"], case["related_bugs"], case["stack_trace"], case["build_pr_mapping"] = na, "N/A", "N/A", "N/A"
        elif r is not None and r.get("executions"):
            we, rb, st, bpm = aggregate_executions(r["executions"])
            case["waived_executions"], case["related_bugs"], case["stack_trace"], case["build_pr_mapping"] = we, rb, st, bpm

        s = status_by_key.get(exec_key(g["entity_name"], g.get("platform")))
        if s is not None:
            case["latest_status"] = s["latest_status"]
            if s.get("regressed_since_pass"):
                # Force the case log's "Analyzed" flag back to False: this
                # case was passing and has failed again, so whatever review
                # state it had before (including a human-set "True") is
                # stale and needs a fresh look. Deliberately NOT set in any
                # other case - see post_confluence_cases.py for how an
                # absent field here means "leave the existing value alone."
                case["analyzed"] = "False"

        ft = failure_type_by_key.get(exec_key(g["entity_name"], g.get("platform")))
        if ft is not None and ft.get("failure_type"):
            case["failure_type"] = ft["failure_type"]

        cases.append(case)
    return cases


def pr_sort_key(pr: str):
    return (0, int(pr)) if pr.isdigit() else (1, pr)


def execution_date(e: dict, fallback: str) -> str:
    """Each nested-table build row gets its own Date, from that specific
    execution's own `ts` (its actual run/post time, in milliseconds since
    epoch) - not a case-level "when this report was built" stamp. Falls
    back to `fallback` (the script run date) if an execution has no `ts`
    (shouldn't normally happen) or there's no execution data at all (stage
    entities, PR placeholders)."""
    ts = e.get("ts")
    if not ts:
        return fallback
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()


def build_nested_cases(groups: list, exec_by_key: dict, status_by_key: dict, failure_type_by_key: dict, date: str) -> list:
    name_counts = Counter(g["entity_name"] for g in groups)
    nested_cases = []
    for g in groups:
        display_name = display_case_name(g, name_counts)
        key = exec_key(g["entity_name"], g.get("platform"))
        r = exec_by_key.get(key)
        prs = []

        if r is not None and not r.get("skipped") and r.get("executions"):
            by_pr = defaultdict(dict)  # pr -> {build: execution}, last write wins for a repeated build
            for e in r["executions"]:
                pr = e.get("mr") or "PostMerge (no PR)"
                build = e.get("build") or "n/a"
                by_pr[pr][build] = e

            for pr in sorted(by_pr.keys(), key=pr_sort_key):
                builds = []
                for build in sorted(by_pr[pr].keys(), key=lambda x: int(x) if x.isdigit() else 0):
                    e = by_pr[pr][build]
                    builds.append({
                        "build": build,
                        "date": execution_date(e, date),
                        "failure_message": e.get("short_error_msg") or "N/A (no error message recorded)",
                        "waived": waived_cell(e.get("is_waived")),
                        "bug": e.get("waive_bug_url") or "none",
                    })
                prs.append({"pr": pr, "builds": builds})
        else:
            obs = g.get("latest_observation") or {}
            pr_ids = obs.get("failure_pr_identifiers") or []
            note = "N/A (stage entity, no test-history)" if g.get("entity_kind") == "stage" else "N/A (no execution data)"
            for pr in pr_ids or ["n/a"]:
                prs.append({"pr": pr, "builds": [{"build": "n/a", "date": date, "failure_message": note, "waived": "N/A", "bug": "N/A"}]})

        nested_case = {"case_name": display_name, "prs": prs}
        s = status_by_key.get(key)
        if s is not None:
            nested_case["latest_status"] = s["latest_status"]
            if s.get("regressed_since_pass"):
                # See build_flat_cases for the reasoning - only ever forces
                # a reset to False, never sets it otherwise. post_confluence_cases.py's
                # nested publish path carries forward the existing page's
                # Analyzed value for any case where this key is absent.
                nested_case["analyzed"] = "False"

        ft = failure_type_by_key.get(key)
        if ft is not None and ft.get("failure_type"):
            nested_case["failure_type"] = ft["failure_type"]

        nested_cases.append(nested_case)
    return nested_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--groups-json", required=True, help="Condensed main-break JSON from fetch_failures.py")
    parser.add_argument("--executions-json", help="Execution-detail JSON from fetch_execution_details.py (required for --mode nested; optional but recommended for --mode flat)")
    parser.add_argument("--status-json", help="Latest-status JSON from fetch_latest_status.py (optional, both modes)")
    parser.add_argument("--failure-types-json", help="Step 4 (Analyze failure reasons) conclusions, hand-written as {\"results\": [{\"entity_name\": ..., \"platform\": ..., \"failure_type\": ...}, ...]} (optional, both modes)")
    parser.add_argument("--mode", choices=["flat", "nested"], default="flat")
    parser.add_argument("--date", default=None, help="Date string to stamp cases with (default: today, YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output cases JSON path (feed this to post_confluence_cases.py)")
    args = parser.parse_args()

    if args.mode == "nested" and not args.executions_json:
        sys.exit("--mode nested requires --executions-json")

    groups_payload = json.loads(Path(args.groups_json).read_text())
    groups = groups_payload.get("main_broken_groups", [])

    exec_by_key = {}
    if args.executions_json:
        exec_payload = json.loads(Path(args.executions_json).read_text())
        exec_by_key = {exec_key(r["entity_name"], r.get("platform")): r for r in exec_payload.get("results", [])}

        if args.mode == "nested" and any(r.get("skipped_known_builds") for r in exec_by_key.values()):
            sys.exit(
                "--mode nested with an --executions-json that used --known-builds-json is unsafe: "
                "post_confluence_cases.py --nested-json always fully replaces the table, so any "
                "already-published build this run skipped fetching would silently disappear from "
                "the republished page instead of being preserved. Re-run fetch_execution_details.py "
                "WITHOUT --known-builds-json to get the complete picture before building nested cases."
            )

    status_by_key = {}
    if args.status_json:
        status_payload = json.loads(Path(args.status_json).read_text())
        status_by_key = {exec_key(r["entity_name"], r.get("platform")): r for r in status_payload.get("results", [])}

    failure_type_by_key = {}
    if args.failure_types_json:
        failure_types_payload = json.loads(Path(args.failure_types_json).read_text())
        failure_type_by_key = {exec_key(r["entity_name"], r.get("platform")): r for r in failure_types_payload.get("results", [])}

    date = args.date or date_cls.today().isoformat()

    if args.mode == "flat":
        cases = build_flat_cases(groups, exec_by_key, status_by_key, failure_type_by_key, date)
    else:
        cases = build_nested_cases(groups, exec_by_key, status_by_key, failure_type_by_key, date)

    Path(args.out).write_text(json.dumps(cases, indent=2))

    if args.mode == "flat":
        print(f"Wrote {len(cases)} flat case(s) to {args.out}")
    else:
        row_count = sum(len(pr["builds"]) for c in cases for pr in c["prs"])
        print(f"Wrote {len(cases)} case(s) / {row_count} nested row(s) to {args.out}")


if __name__ == "__main__":
    main()
