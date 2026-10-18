"""Turn a Slack message into (skill, prompt).

Routing rules, first match wins:
1. ``/<skill-name> rest`` or ``<skill-name>: rest`` -> that skill (name must
   exist under ``skills/``; a unique prefix is accepted).
2. ``help`` / ``skills`` -> bot help text, no agent call.
3. ``stop`` / ``cancel`` -> cancel the running request in this thread.
4. Otherwise: no explicit skill. The agent gets the question as-is plus the
   list of available skills in its system prompt, so Claude Code's own skill
   matching decides (the main-failures skill triggers on "what's breaking on
   trtllm main"-style questions by itself).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")
_LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
_SLACK_SUBTEAM_RE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|[^>]*)?>")
_STOP_RE = re.compile(r"^\s*(stop|cancel|abort|kill)\s*[.!]?\s*$", re.I)
_HELP_RE = re.compile(r"^\s*(help|skills|\?)\s*[.!?]?\s*$", re.I)
_SKILL_PREFIX_RE = re.compile(r"^\s*/?([a-z0-9][a-z0-9_-]*)\s*(?::\s*|\s+|$)(.*)$", re.I | re.S)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


@dataclass(frozen=True)
class Route:
    kind: str  # "skill" | "chat" | "help" | "stop"
    skill: Skill | None = None
    prompt: str = ""


def clean_text(text: str) -> str:
    """Strip the bot mention and unwrap Slack's <url|label> link markup."""
    text = _MENTION_RE.sub("", text or "")
    text = _SLACK_SUBTEAM_RE.sub("", text)
    text = _LINK_RE.sub(lambda m: m.group(1), text)
    # Slack escapes these three in message text
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def _frontmatter(md: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not md.startswith("---"):
        return out
    end = md.find("\n---", 3)
    if end < 0:
        return out
    for line in md[3:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def discover_skills(skills_dir: Path) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    if not skills_dir.is_dir():
        return skills
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        name = skill_md.parent.name
        fm = _frontmatter(skill_md.read_text(errors="replace"))
        desc = fm.get("description", "")
        if not desc:
            # first non-heading, non-empty line after the frontmatter
            body = skill_md.read_text(errors="replace")
            for line in body.splitlines():
                s = line.strip()
                if s and not s.startswith(("#", "---")):
                    desc = s
                    break
        skills[name] = Skill(name=fm.get("name", name) or name, description=desc[:200], path=skill_md)
    return skills


def _match_skill(token: str, skills: dict[str, Skill]) -> Skill | None:
    token = token.lower()
    if token in skills:
        return skills[token]
    candidates = [s for n, s in skills.items() if n.lower().startswith(token)]
    return candidates[0] if len(candidates) == 1 else None


def route(text: str, skills: dict[str, Skill]) -> Route:
    text = clean_text(text)
    if not text:
        return Route("help")
    if _STOP_RE.match(text):
        return Route("stop")
    if _HELP_RE.match(text):
        return Route("help")
    m = _SKILL_PREFIX_RE.match(text)
    if m:
        skill = _match_skill(m.group(1), skills)
        # a bare word that happens to prefix-match a skill is only a skill
        # call when written with "/" or ":" or when it is the full name
        explicit = text.lstrip().startswith("/") or ":" in text[: len(m.group(1)) + 2] or m.group(1).lower() in skills
        if skill and explicit:
            return Route("skill", skill=skill, prompt=m.group(2).strip())
    return Route("chat", prompt=text)


_DAYS_RE = re.compile(r"(?:^|\s|=)-{0,2}(?:days?\s*[=:]?\s*)?(\d{1,3})\s*(?:d|days?)?(?=\s|$)", re.I)

COMMAND_DEFAULT_DAYS = {"fetch": 7, "analyze": 1}


@dataclass(frozen=True)
class CommandRoute:
    kind: str  # "fetch" | "analyze" | "help"
    days: int = 0
    extra: str = ""  # leftover words (extra instructions for /analyze)


def parse_days(text: str, default: int) -> tuple[int, str]:
    """``"7"``, ``"7d"``, ``"7 days"``, ``"last 3 days"``, ``"days=30"``,
    ``"--days 2"`` -> (days, remaining text). No integer -> (default, text)."""
    text = (text or "").strip()
    m = _DAYS_RE.search(text)
    if not m:
        return default, text
    days = int(m.group(1))
    rest = (text[: m.start()] + " " + text[m.end():]).strip()
    rest = re.sub(r"(?:^|\s)(?:(?:for|in|over)\s+)?(?:the\s+)?last\s*$", "", rest, flags=re.I).strip()
    return max(days, 1), rest


def route_command(command: str, text: str) -> CommandRoute:
    """Route a slash command. ``/fetch [days]`` (default 7 days) fetches the
    main-break detections; ``/analyze [days] [extra]`` (default 1 day) runs the
    main-failures skill. ``help`` text or an unknown command -> help."""
    name = (command or "").strip().lstrip("/").lower()
    text = clean_text(text)
    if name not in COMMAND_DEFAULT_DAYS or _HELP_RE.match(text):
        return CommandRoute("help")
    days, extra = parse_days(text, COMMAND_DEFAULT_DAYS[name])
    return CommandRoute(name, days=days, extra=extra)


def command_help_text() -> str:
    return (
        "*Slash commands*\n"
        "• `/fetch [days]` — main-break detections of the last N days (default 7; dashboard windows are 1, 2, 7, 30, "
        "other values round up).\n"
        "• `/analyze [days] [extra instructions]` — run the main-failures analysis for the last N days (default 1) "
        "and DM you the report.\n"
        "Progress and the result land in a thread under the command's anchor message; reply there to follow up, "
        "or reply `stop` to cancel."
    )


def help_text(skills: dict[str, Skill], default_skill: str) -> str:
    lines = [
        "*CI-analysis bot* — mention me or DM me. Replies land in the thread; one request per thread at a time.",
        "",
        "*Run a skill explicitly:* `/<skill> <what to do>` (unique prefix ok), e.g.",
        f"`/{default_skill} review test failures of case unittest/foo.py::test_bar`",
        "",
        "*Ask a question:* plain text, e.g. `what's breaking on trtllm main in the last day?`",
        "",
        "*Follow-ups:* reply in the same thread; the agent session is resumed.",
        "*Cancel:* reply `stop` in the thread.",
        "",
        command_help_text(),
        "",
        "*Skills available:*",
    ]
    for name, s in skills.items():
        lines.append(f"• `{name}` — {s.description}" if s.description else f"• `{name}`")
    return "\n".join(lines)
