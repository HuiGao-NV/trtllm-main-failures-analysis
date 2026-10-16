#!/usr/bin/env python3
"""Commit info + commit time for every commit/PR involved in one problem.

Input: one or more `ROLE=REF` pairs, where ROLE is a free label (culprit,
early, late, fix, ...) and REF is a commit sha or a merged PR number
(`#20362` / `20362`). Output: a table with, per entry, the role, short sha,
PR, title, author, commit time (UTC; the GitHub committer date on `main`),
PR merged time (UTC) and the gap to the first entry (x days x hours x minutes).

    problem_commits.py --entity "P3 phase_selective_forward" \
        --commit early=7c44c537acd9 --commit late=03a7b7f8655f --commit fix=d955ed3028dc \
        --out <run_dir>/problem_commits_P3.json --md-out <run_dir>/problem_commits_P3.md \
        --slack-out <run_dir>/problem_commits_slack_P3.md

All times are UTC (GitHub). Do not mix with dashboard times (US-Pacific).
"""

import argparse
import datetime as dt
import json
import subprocess
import sys

from durations import human
from github_config import gh_env

REPO = "NVIDIA/TensorRT-LLM"


def gh(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, env=gh_env())
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def resolve(ref):
    """-> (sha, pr_number) from a sha or PR number."""
    r = ref.lstrip("#")
    if r.isdigit() and len(r) < 7:
        pr = gh(f"repos/{REPO}/pulls/{r}") or {}
        return pr.get("merge_commit_sha"), int(r)
    return r, None


def info(role, ref):
    sha, pr_num = resolve(ref)
    c = (gh(f"repos/{REPO}/commits/{sha}") or {}) if sha else {}
    if pr_num is None and sha:
        prs = gh(f"repos/{REPO}/commits/{sha}/pulls") or []
        pr_num = prs[0]["number"] if prs else None
    pr = (gh(f"repos/{REPO}/pulls/{pr_num}") or {}) if pr_num else {}
    cm = c.get("commit", {})
    return {
        "role": role,
        "sha": sha,
        "short_sha": (sha or "")[:12],
        "pr": pr_num,
        "title": pr.get("title") or (cm.get("message") or "").splitlines()[0:1] and cm["message"].splitlines()[0],
        "author": (pr.get("user") or {}).get("login"),
        "commit_time": (cm.get("committer") or {}).get("date"),
        "pr_created": pr.get("created_at"),
        "pr_merged": pr.get("merged_at"),
        "url": pr.get("html_url") or c.get("html_url"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="append", required=True, metavar="ROLE=REF")
    ap.add_argument("--entity", default="")
    ap.add_argument("--out")
    ap.add_argument("--md-out")
    ap.add_argument("--slack-out")
    a = ap.parse_args()

    rows = []
    for item in a.commit:
        role, _, ref = item.partition("=")
        if not ref:
            sys.exit(f"bad --commit {item!r}; want ROLE=REF")
        rows.append(info(role, ref))
    t0 = iso(rows[0]["commit_time"])
    for r in rows:
        t = iso(r["commit_time"])
        r["gap_from_first"] = human((t - t0).total_seconds() / 3600) if t and t0 else None
        r["gap_from_first_hours"] = round((t - t0).total_seconds() / 3600, 2) if t and t0 else None

    md = [f"**{a.entity}** — commits involved (times UTC)" if a.entity else "Commits involved (times UTC)", "",
          "| Role | Commit | PR | Title | Author | Commit time (UTC) | PR merged (UTC) | After first |",
          "|---|---|---|---|---|---|---|---|"]
    sl = [f"*Commits involved* — {a.entity} (UTC)" if a.entity else "*Commits involved* (UTC)"]
    for r in rows:
        pr = f"[#{r['pr']}]({r['url']})" if r["pr"] else "-"
        md.append(f"| {r['role']} | `{r['short_sha']}` | {pr} | {r['title'] or ''} | @{r['author'] or '?'} | "
                  f"{r['commit_time'] or '?'} | {r['pr_merged'] or '?'} | {r['gap_from_first'] or '-'} |")
        sl.append(f"• {r['role']}: `{r['short_sha']}` #{r['pr'] or '?'} @{r['author'] or '?'} — "
                  f"commit {r['commit_time'] or '?'}, PR merged {r['pr_merged'] or '?'}")

    if a.out:
        json.dump({"entity": a.entity, "commits": rows}, open(a.out, "w"), indent=2)
    if a.md_out:
        open(a.md_out, "w").write("\n".join(md) + "\n")
    if a.slack_out:
        open(a.slack_out, "w").write("\n".join(sl) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
