#!/usr/bin/env python3
"""Collect the PRs that must rebase past a fix commit, resolve their authors,
dump them to a file and render the "Rebase Action:" section for the report
and the Slack DM.

Which PRs need a rebase (Step 5 `regression-fixed` -> `rebase-affected-prs`):
a PR whose failing build ran on a `main` base that PREDATES the fix commit.
That is exactly what get_base_commit.py records per build as
`base_is_pre_fix: true` (with `fix_candidate` = the fix sha), so the primary
input is one or more of its output files. PR lists written by hand into an
actions file (`{"action": "rebase-affected-prs", "prs": [...]}`) and PRs
passed on the command line (`--pr`) are unioned in.

Per PR the script asks GitHub (`gh api repos/<repo>/pulls/<n>`) for the
author login, display name, PR title and state. Merged/closed PRs do not
need a rebase any more: they are kept in the dump under `skipped` (with the
reason) but left out of the rendered section.

Inputs:
  --base-commits-json <get_base_commit.py output>   (repeatable)
  --actions-json <actions_<date>.json>              (repeatable; `rebase-affected-prs` prs lists)
  --pr <number>                                     (repeatable)
  --fix-commit <sha>      label for the section; defaults to the fix_candidate
                          found in the base-commits files (error if ambiguous
                          and not given)
  --entity <text>         optional one-line problem label (e.g. "P20 KVCacheManagerV2 fixtures")
  --repo <owner/name>     default NVIDIA/TensorRT-LLM
  --out <path>            JSON dump (required)
  --md-out <path>         markdown "## Rebase Action:" section (report)
  --slack-out <path>      Slack mrkdwn "*Rebase Action:*" block

JSON dump shape:
  {"fix_commit": "...", "repo": "...", "entity": "...", "generated": "<UTC iso>",
   "prs": [{"pr": "19249", "author": "login", "author_name": "Name or null",
            "title": "...", "state": "open", "builds": ["61021"],
            "base_commits": ["bcbcc0ea..."], "url": "https://github.com/..."}],
   "skipped": [{"pr": "...", "author": "...", "state": "merged|closed", "reason": "..."}],
   "authors": {"login": ["19249", ...]}}
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = "NVIDIA/TensorRT-LLM"


def gh_json(args: list[str], timeout: int = 30):
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"  gh invocation failed: {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"  gh {' '.join(args)} failed: {result.stderr.strip()[:300]}", file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def load_json(path: str):
    return json.loads(Path(path).read_text())


def collect_from_base_commits(paths: list[str]) -> tuple[dict[str, dict], set[str]]:
    """PR -> {"builds": set, "base_commits": set}; plus the set of fix candidates seen."""
    prs: dict[str, dict] = {}
    fixes: set[str] = set()
    for p in paths:
        data = load_json(p)
        for pr, entries in data.items():
            if not pr:  # post-merge builds have no PR
                continue
            for entry in entries:
                for build, info in entry.items():
                    if info.get("fix_candidate"):
                        fixes.add(info["fix_candidate"])
                    if info.get("base_is_pre_fix") is True:
                        rec = prs.setdefault(str(pr), {"builds": set(), "base_commits": set()})
                        rec["builds"].add(str(build))
                        if info.get("base_commit"):
                            rec["base_commits"].add(info["base_commit"])
    return prs, fixes


def collect_from_actions(paths: list[str]) -> set[str]:
    prs: set[str] = set()
    for p in paths:
        data = load_json(p)
        results = data.get("results", [data]) if isinstance(data, dict) else data
        for r in results:
            if not isinstance(r, dict):
                continue
            for a in r.get("actions", []):
                if isinstance(a, dict) and a.get("action") == "rebase-affected-prs":
                    for n in a.get("prs", []) or []:
                        prs.add(str(n).lstrip("#"))
    return prs


def fetch_pr(repo: str, pr: str) -> dict:
    data = gh_json(["api", f"repos/{repo}/pulls/{pr}"])
    if not isinstance(data, dict):
        return {"author": None, "author_name": None, "title": None, "state": "unknown",
                "url": f"https://github.com/{repo}/pull/{pr}"}
    login = (data.get("user") or {}).get("login")
    state = "merged" if data.get("merged_at") else data.get("state", "unknown")
    return {"author": login, "author_name": None, "title": data.get("title"),
            "state": state, "url": data.get("html_url") or f"https://github.com/{repo}/pull/{pr}"}


def fetch_name(login: str, cache: dict[str, str | None]) -> str | None:
    if login in cache:
        return cache[login]
    data = gh_json(["api", f"users/{login}", "--jq", "{name}"])
    cache[login] = data.get("name") if isinstance(data, dict) else None
    return cache[login]


def render_md(dump: dict) -> str:
    fix = dump["fix_commit"] or "<fix commit>"
    lines = ["## Rebase Action:", ""]
    head = f"PRs whose failing builds ran on a `main` base older than the fix `{fix}`"
    if dump.get("entity"):
        head += f" ({dump['entity']})"
    lines.append(head + ". Each author should rebase past the fix; nothing else is needed.")
    lines.append("")
    if not dump["prs"]:
        lines.append("_None._")
    else:
        lines.append("| PR | Author | Title | Failing builds |")
        lines.append("|---|---|---|---|")
        for p in dump["prs"]:
            author = f"@{p['author']}" if p["author"] else "unknown"
            if p.get("author_name"):
                author += f" ({p['author_name']})"
            title = (p.get("title") or "").replace("|", "\\|")
            builds = ", ".join(p.get("builds") or []) or "—"
            lines.append(f"| [#{p['pr']}]({p['url']}) | {author} | {title} | {builds} |")
        lines.append("")
        lines.append("By author:")
        for login, prs in sorted(dump["authors"].items()):
            lines.append(f"- @{login}: " + " ".join(f"#{n}" for n in prs))
    if dump["skipped"]:
        lines.append("")
        lines.append("Already merged/closed (no rebase needed): "
                     + " ".join(f"#{s['pr']}" for s in dump["skipped"]))
    lines.append("")
    return "\n".join(lines)


def render_slack(dump: dict) -> str:
    fix = dump["fix_commit"] or "<fix commit>"
    out = [f"*Rebase Action:* rebase past `{fix}`"
           + (f" — {dump['entity']}" if dump.get("entity") else "")]
    if not dump["prs"]:
        out.append("• _none_")
    for p in dump["prs"]:
        author = f"@{p['author']}" if p["author"] else "unknown author"
        out.append(f"• <{p['url']}|#{p['pr']}> — {author}")
    if dump["skipped"]:
        out.append("_already merged/closed: " + " ".join(f"#{s['pr']}" for s in dump["skipped"]) + "_")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-commits-json", action="append", default=[])
    ap.add_argument("--actions-json", action="append", default=[])
    ap.add_argument("--pr", action="append", default=[])
    ap.add_argument("--fix-commit")
    ap.add_argument("--entity")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--out", required=True)
    ap.add_argument("--md-out")
    ap.add_argument("--slack-out")
    ap.add_argument("--no-names", action="store_true", help="skip the extra users/<login> lookup")
    args = ap.parse_args()

    if not (args.base_commits_json or args.actions_json or args.pr):
        ap.error("give at least one of --base-commits-json / --actions-json / --pr")

    prs, fixes = collect_from_base_commits(args.base_commits_json)
    for n in collect_from_actions(args.actions_json) | {p.lstrip("#") for p in args.pr}:
        prs.setdefault(n, {"builds": set(), "base_commits": set()})

    fix_commit = args.fix_commit
    if not fix_commit:
        if len(fixes) == 1:
            fix_commit = next(iter(fixes))
        elif len(fixes) > 1:
            ap.error(f"several fix candidates in the base-commits files {sorted(fixes)}; pass --fix-commit")

    name_cache: dict[str, str | None] = {}
    keep, skipped, authors = [], [], {}
    for pr in sorted(prs, key=int):
        info = fetch_pr(args.repo, pr)
        print(f"  #{pr}: {info['state']} by {info['author']}", file=sys.stderr)
        if info["author"] and not args.no_names:
            info["author_name"] = fetch_name(info["author"], name_cache)
        rec = {"pr": pr, **info,
               "builds": sorted(prs[pr]["builds"], key=lambda b: int(b) if b.isdigit() else 0),
               "base_commits": sorted(prs[pr]["base_commits"])}
        if info["state"] in ("merged", "closed"):
            skipped.append({"pr": pr, "author": info["author"], "state": info["state"],
                            "reason": f"PR is {info['state']}; no rebase needed"})
            continue
        keep.append(rec)
        authors.setdefault(info["author"] or "unknown", []).append(pr)

    dump = {"fix_commit": fix_commit, "repo": args.repo, "entity": args.entity,
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "prs": keep, "skipped": skipped, "authors": authors}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(dump, indent=2) + "\n")
    print(f"wrote {args.out}: {len(keep)} PRs need a rebase, {len(skipped)} skipped, "
          f"{len(authors)} authors", file=sys.stderr)
    if args.md_out:
        Path(args.md_out).write_text(render_md(dump))
        print(f"wrote {args.md_out}", file=sys.stderr)
    if args.slack_out:
        Path(args.slack_out).write_text(render_slack(dump))
        print(f"wrote {args.slack_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
