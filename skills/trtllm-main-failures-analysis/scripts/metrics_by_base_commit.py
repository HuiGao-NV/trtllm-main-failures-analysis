#!/usr/bin/env python3
"""Join harvested per-build metrics with each build's true `main` base commit and decide
whether the metric follows the BASE COMMIT or the RUN TIME (Method D in SKILL.md).

Inputs
  --metrics-tsv   output of harvest_pbss_metrics.py
  --base-json     output of get_base_commit.py (repeatable; PR builds and post-merge builds
                  are usually resolved in separate runs). Builds are matched on the short job
                  name (LLM/main/L0_MergeRequest_PR -> L0_MergeRequest_PR). For post-merge
                  builds base_commit is null and head_commit (the tested `main` commit) is used.
  --test-regex    which harvested test to analyse (default: all rows)
  --metric        metric column to analyse (default: first metric column with values)
  --threshold     optional pass threshold: values below it are marked FAIL

Outputs
  <out>.tsv   one row per (build, attempt): base, base time, base title, build, PR, run time,
              node, metric, verdict, sorted by base-commit time then run time
  <out>.md    the same as a markdown table plus the analysis below
  stdout      the analysis:
    * same-base groups whose metric differs between runs (=> the metric is not a function
      of the source tree; lists the run days)
    * adjacent-base steps where the metric changes with the base while run days overlap
      (=> commit-driven candidates, with the compare range)
    * per-day histogram of the metric
    * VERDICT: commit-driven | time-driven | mixed | insufficient-data, with the rule used

Rule of thumb encoded here
  time-driven   : >= 2 bases each run on >= 2 days give different values, AND the failing
                  value appears on bases both older and newer than a passing base run on the
                  same or a later day.
  commit-driven : every base has one value (or values that only differ across clusters/
                  retries by noise) and the failing value starts at one base and persists.
  mixed         : both signals present. insufficient-data otherwise.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from durations import duration_between  # noqa: E402


def gh_commit(sha: str, cache: dict) -> tuple[str, str]:
    if not sha:
        return "", ""
    if sha not in cache:
        r = subprocess.run(["gh", "api", f"repos/NVIDIA/TensorRT-LLM/commits/{sha}", "--jq",
                            '[.commit.committer.date,(.commit.message|split("\n")[0])]|@tsv'],
                           capture_output=True, text=True)
        parts = r.stdout.strip().split("\t") if r.returncode == 0 and r.stdout.strip() else ["", ""]
        cache[sha] = (parts[0], parts[1] if len(parts) > 1 else "")
    return cache[sha]


def load_bases(files: list[str]) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for fn in files:
        d = json.load(open(fn))
        # get_base_commit.py output: {"<pr or ''>": [{"<build>": {...}}, ...]} ; job name is not stored per entry,
        # so callers pass PR-build and post-merge resolutions in separate files and we tag by content.
        for pr, lst in d.items():
            for e in lst:
                for b, v in e.items():
                    base = v.get("base_commit") or v.get("head_commit") or ""
                    out[(pr, b)] = dict(pr=pr, head=v.get("head_commit") or "", base=base)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metrics-tsv", required=True)
    ap.add_argument("--base-json", action="append", required=True)
    ap.add_argument("--test-regex", default=".")
    ap.add_argument("--metric", default=None)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--out", required=True, help="output path without extension")
    a = ap.parse_args()
    import re
    trx = re.compile(a.test_regex)

    rows = [r for r in csv.DictReader(open(a.metrics_tsv), delimiter="\t") if r.get("log_path") and trx.search(r["test"])]
    if not rows:
        print("no harvested rows match", file=sys.stderr)
        return 1
    metric = a.metric
    if metric is None:
        fixed = {"job", "build", "stage", "test", "attempt_utc", "attempt_epoch", "host", "log_path", "note"}
        for c in rows[0].keys():
            if c not in fixed and any(r.get(c) for r in rows):
                metric = c
                break
    if metric is None:
        print("no metric column with values", file=sys.stderr)
        return 1

    bases = load_bases(a.base_json)
    by_build: dict[str, dict] = {}
    for (pr, b), v in bases.items():
        by_build[b] = v  # build ids are unique across PR and post-merge jobs in practice; PM ids are small
    cache: dict = {}
    table = []
    for r in rows:
        v = by_build.get(r["build"], dict(pr="", head="", base=""))
        bdate, btitle = gh_commit(v["base"], cache)
        val = r.get(metric, "")
        verdict = ""
        if a.threshold is not None and val:
            verdict = "FAIL" if float(val) < a.threshold else "PASS"
        table.append(dict(base=v["base"][:12], base_time=bdate[:16].replace("T", " "), base_title=btitle[:60],
                          job=r["job"], build=r["build"], pr=v["pr"], run_utc=r["attempt_utc"], host=r.get("host", ""),
                          metric=val, verdict=verdict))
    table.sort(key=lambda t: (t["base_time"], t["run_utc"]))
    cols = ["base", "base_time", "job", "build", "pr", "run_utc", "host", metric, "verdict", "base_title"]
    with open(a.out + ".tsv", "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(cols)
        for t in table:
            w.writerow([t["base"], t["base_time"], t["job"], t["build"], t["pr"], t["run_utc"], t["host"], t["metric"], t["verdict"], t["base_title"]])

    # ---- analysis
    lines: list[str] = []
    valued = [t for t in table if t["metric"]]
    by_base: dict[str, list] = defaultdict(list)
    for t in valued:
        by_base[(t["base"], t["base_time"])].append(t)
    same_base_diff = []
    for (b, bt), lst in by_base.items():
        vals = {t["metric"] for t in lst}
        days = {t["run_utc"][:10] for t in lst}
        if len(vals) > 1 and len(days) > 1:
            same_base_diff.append((bt, b, sorted((t["run_utc"], t["metric"], t["build"]) for t in lst)))
    same_base_diff.sort()
    lines.append(f"# Method D — {metric} for {len(valued)} runs / {len(by_base)} distinct main bases")
    lines.append("")
    lines.append(f"## Same base, different run day → different {metric}  ({len(same_base_diff)} bases)")
    for bt, b, lst in same_base_diff:
        lines.append(f"- `{b}` ({bt}): " + "; ".join(f"{run} → {val} (build {bl})" for run, val, bl in lst))
    # day histogram
    hist: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for t in valued:
        hist[t["run_utc"][:10]][t["metric"]] += 1
    lines.append("")
    lines.append(f"## {metric} by run day")
    for day in sorted(hist):
        lines.append(f"- {day}: " + ", ".join(f"{v}×{n}" for v, n in sorted(hist[day].items(), key=lambda x: -x[1])))
    # failing-value interleave test
    verdict = "insufficient-data"
    reason = ""
    if a.threshold is not None and valued:
        fails = [t for t in valued if t["verdict"] == "FAIL"]
        passes = [t for t in valued if t["verdict"] == "PASS"]
        if fails and passes:
            first_fail_base = min(t["base_time"] for t in fails)
            first_fail_run = min(t["run_utc"] for t in fails)
            # passes on bases NEWER than the oldest failing base, run at/after the first failing run
            newer_pass_after = [t for t in passes if t["base_time"] > first_fail_base and t["run_utc"] >= first_fail_run]
            # passes on bases newer than the oldest failing base run BEFORE the first failing run
            newer_pass_before = [t for t in passes if t["base_time"] > first_fail_base and t["run_utc"] < first_fail_run]
            older_fail = [t for t in fails if t["base_time"] < max(p["base_time"] for p in passes)]
            lines.append("")
            lines.append("## Threshold analysis")
            last_pass_before = max((t["run_utc"] for t in passes if t["run_utc"] < first_fail_run), default=None)
            last_fail_run = max(t["run_utc"] for t in fails)
            def _iso(run_utc):  # run_utc is 'YYYY-MM-DD HH:MM' UTC
                return run_utc.replace(" ", "T") + ":00+00:00"
            lines.append(f"- first failing run {first_fail_run} on base {first_fail_base}; {len(fails)} FAIL / {len(passes)} PASS runs")
            if last_pass_before:
                lines.append(f"- last passing run before it: {last_pass_before} → onset gap {duration_between(_iso(last_pass_before), _iso(first_fail_run))}")
            lines.append(f"- failing span: {first_fail_run} → {last_fail_run} = {duration_between(_iso(first_fail_run), _iso(last_fail_run))}")
            oldest_fail_base_t = min(t["base_time"] for t in fails); newest_fail_base_t = max(t["base_time"] for t in fails)
            lines.append(f"- failing bases span {oldest_fail_base_t} → {newest_fail_base_t} = {duration_between(_iso(oldest_fail_base_t), _iso(newest_fail_base_t))} of main history")
            lines.append(f"- passes on bases newer than the oldest failing base: {len(newer_pass_before)} before the first failing run, "
                         f"{len(newer_pass_after)} at/after it")
            lines.append(f"- failing runs on bases older than the newest passing base: {len(older_fail)}")
            after_first_fail = [t for t in valued if t["run_utc"] >= first_fail_run]
            escape_ratio = len(newer_pass_after) / max(1, len(after_first_fail))
            lines.append(f"- runs at/after the first failure: {len(after_first_fail)}, of which passes on newer bases: "
                         f"{len(newer_pass_after)} ({escape_ratio:.0%}; retries/odd nodes are tolerated up to 20%)")
            if same_base_diff and older_fail and newer_pass_before and escape_ratio <= 0.2:
                verdict, reason = "time-driven", ("same bases give different values on different days, failing values sit on bases "
                                                  "both older and newer than passing bases, and passes after the first failure are "
                                                  f"rare escapes ({len(newer_pass_after)})")
            elif not same_base_diff and not older_fail:
                verdict, reason = "commit-driven", "every base has one value and the failing value starts at one base and persists"
            elif same_base_diff or older_fail:
                verdict, reason = "mixed", "both same-base drift and a base-ordered step are present; bound the step, then check it survives Method D"
    elif same_base_diff:
        verdict, reason = "time-driven (no threshold given)", "same bases give different values on different days"
    elif len(by_base) >= 3:
        verdict, reason = "commit-driven (no threshold given)", "every base has a single value"
    lines.append("")
    lines.append(f"## VERDICT: {verdict}")
    if reason:
        lines.append(f"- {reason}")
    if verdict.startswith("time-driven"):
        lines.append("- Do NOT attribute to a main commit. Compare run-time inputs (container/image, driver, node, shared "
                     "model/data mounts, test order in the stage, retries) between adjacent passing/failing runs; "
                     "classify as flaky/environment and propose the environment check, not a revert.")
    elif verdict.startswith("commit-driven"):
        steps = []
        prev = None
        for (b, bt), lst in sorted(by_base.items(), key=lambda x: x[0][1]):
            val = sorted({t["metric"] for t in lst})[0]
            if prev and prev[2] != val:
                steps.append(f"`{prev[0]}`..`{b}` ({prev[1]} → {bt}, {duration_between(prev[1].replace(' ', 'T') + ':00+00:00', bt.replace(' ', 'T') + ':00+00:00')}): {prev[2]} → {val}")
            prev = (b, bt, val)
        lines.append("- adjacent-base steps to bound with Method C: " + ("; ".join(steps) if steps else "none"))

    md = ["# Metrics by main base commit", "", "| base | base time (UTC) | build | PR | run (UTC) | node | " + metric + " | verdict |",
          "|---|---|---|---|---|---|---|---|"]
    for t in table:
        b = ("PM " if "PostMerge" in t["job"] else "") + t["build"]
        md.append(f"| `{t['base']}` | {t['base_time']} | {b} | {('#' + t['pr']) if t['pr'] else 'post-merge'} | {t['run_utc']} | "
                  f"{t['host']} | {t['metric']} | {t['verdict']} |")
    with open(a.out + ".md", "w") as f:
        f.write("\n".join(md) + "\n\n" + "\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwrote {a.out}.tsv and {a.out}.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
