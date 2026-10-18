"""Run one Claude Code request as a ``claude -p`` subprocess.

Mirrors repair-bot's ``core/agent.py`` contract (prompt + options -> result
text, session id, cost) but without the Agent SDK: the CLI is already on
this machine and its JSON output carries everything the bot needs.

Key behaviours
- cwd is the CI_analysis repo so the skills under ``.claude/skills`` and the
  agents under ``.claude/agents`` are discovered natively; the launcher syncs
  them from ``skills/`` and ``agents/`` on every start.
- ``--append-system-prompt`` carries the Slack context (reply in mrkdwn,
  never publish / DM unless the user asks).
- A follow-up in the same Slack thread passes ``--resume <session_id>``.
- The child runs in its own process group so a cancel kills the whole tree.
- ``CLAUDECODE`` is removed from the environment (a nested session refuses to
  start otherwise) and the Slack tokens are blanked so skill scripts cannot
  read them.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

SLACK_SYSTEM_PROMPT = """\
You are answering a request that arrived from Slack and your final message is \
posted back into the Slack thread verbatim.
- Write for Slack: short paragraphs, bullet lists, fenced code blocks for \
commands, errors and tables. Keep the whole reply under ~3500 characters \
unless the user asked for a full report; put long detail in files under the \
run directory and name them.
- Do not publish to Confluence, Google Sheets, or send Slack messages \
yourself unless the user's message explicitly asks for it. The bot delivers \
your reply.
- Do not ask clarifying questions you can resolve with sensible defaults; \
state the assumption instead. If something truly needs the user, end with \
one clear question.
- Never print credentials or tokens.
"""


@dataclass
class AgentResult:
    ok: bool
    text: str
    session_id: str = ""
    cost_usd: float | None = None
    duration_s: float = 0.0
    num_turns: int | None = None
    returncode: int | None = None
    stderr_tail: str = ""
    cancelled: bool = False
    timed_out: bool = False


@dataclass
class AgentRequest:
    prompt: str
    skill: str | None = None
    resume_session_id: str | None = None
    slack_user: str = ""
    slack_channel: str = ""
    slack_thread_ts: str = ""
    extra_system_prompt: str = ""
    deliver_to_user: str | None = None  # also DM the final reply to this Slack user id (slash commands)
    extra: dict = field(default_factory=dict)  # worker-specific parameters (e.g. {"days": 7} for /fetch)
    cancel: threading.Event = field(default_factory=threading.Event)


def build_prompt(req: AgentRequest) -> str:
    """Prefix an explicit skill invocation; Claude Code treats a leading
    ``/<skill>`` as a request to invoke that skill."""
    body = req.prompt.strip()
    if req.skill:
        return f"/{req.skill} {body}".strip()
    return body


def build_system_prompt(req: AgentRequest, skills_summary: str) -> str:
    parts = [SLACK_SYSTEM_PROMPT.strip()]
    if skills_summary:
        parts.append("Skills available in this repository (invoke with the Skill tool when relevant):\n" + skills_summary)
    if req.slack_user or req.slack_channel:
        parts.append(
            f"Slack context: requester <@{req.slack_user}>, channel {req.slack_channel}, thread {req.slack_thread_ts}."
        )
    if req.extra_system_prompt:
        parts.append(req.extra_system_prompt.strip())
    return "\n\n".join(parts)


def build_command(settings: Settings, req: AgentRequest, system_prompt: str) -> list[str]:
    cmd = [
        settings.claude_bin,
        "-p",
        build_prompt(req),
        "--output-format",
        "json",
        "--permission-mode",
        settings.permission_mode,
        "--append-system-prompt",
        system_prompt,
    ]
    if settings.allowed_tools:
        cmd += ["--allowedTools", *settings.allowed_tools]
    if settings.claude_model:
        cmd += ["--model", settings.claude_model]
    if req.resume_session_id:
        cmd += ["--resume", req.resume_session_id]
    return cmd


def child_env(settings: Settings, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    for key in settings.scrub_env_keys:
        env.pop(key, None)
    # Slack tokens must not leak into skill scripts even via inheritance
    env["SLACK_BOT_TOKEN"] = ""
    env["SLACK_APP_TOKEN"] = ""
    return env


def parse_output(stdout: str) -> dict:
    """``--output-format json`` prints one JSON object; tolerate leading
    noise and a trailing newline."""
    stdout = stdout.strip()
    if not stdout:
        return {}
    data = None
    try:
        data = json.loads(stdout)
    except ValueError:
        start = min((i for i in (stdout.find("{"), stdout.find("[")) if i >= 0), default=-1)
        end = max(stdout.rfind("}"), stdout.rfind("]"))
        if start >= 0 and end > start:
            try:
                data = json.loads(stdout[start : end + 1])
            except ValueError:
                data = None
    if data is None:
        return {"result": stdout}
    return _normalize(data)


def _normalize(data) -> dict:
    """Newer CLIs emit a list of messages (system/assistant/result); older
    ones a single result object. Return the result object, filling ``result``
    text from assistant text blocks when the result lacks it."""
    if isinstance(data, dict):
        return data
    if not isinstance(data, list):
        return {"result": str(data)}
    result = next((it for it in data if isinstance(it, dict) and it.get("type") == "result"), None) or {}
    if not result.get("result"):
        texts = []
        for it in data:
            if isinstance(it, dict) and it.get("type") == "assistant":
                for block in (it.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                        texts.append(block["text"])
        if texts:
            result = {**result, "result": texts[-1] if len(texts) == 1 else "\n\n".join(texts)}
    if not result.get("session_id"):
        sid = next((it.get("session_id") for it in data if isinstance(it, dict) and it.get("session_id")), "")
        if sid:
            result = {**result, "session_id": sid}
    return result


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def run_agent(settings: Settings, req: AgentRequest, skills_summary: str = "", log_path: Path | None = None) -> AgentResult:
    if shutil.which(settings.claude_bin) is None and not Path(settings.claude_bin).exists():
        return AgentResult(ok=False, text=f"`{settings.claude_bin}` not found on PATH for the bot process.")

    system_prompt = build_system_prompt(req, skills_summary)
    cmd = build_command(settings, req, system_prompt)
    started = time.monotonic()
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "a") if log_path else None
    if log:
        log.write(f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%S')} prompt={build_prompt(req)!r} resume={req.resume_session_id}\n")
        log.flush()

    proc = subprocess.Popen(
        cmd,
        cwd=str(settings.repo_root),
        stdout=subprocess.PIPE,
        stderr=log or subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        env=child_env(settings),
        start_new_session=True,
    )

    out_chunks: list[str] = []

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            out_chunks.append(line)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    timed_out = cancelled = False
    deadline = started + settings.timeout_seconds
    while proc.poll() is None:
        if req.cancel.is_set():
            cancelled = True
            break
        if time.monotonic() > deadline:
            timed_out = True
            break
        time.sleep(0.5)

    if cancelled or timed_out:
        _kill_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGKILL)
            proc.wait()

    reader.join(timeout=5)
    stderr_tail = ""
    if proc.stderr is not None and not log:
        stderr_tail = (proc.stderr.read() or "")[-2000:]
    if log:
        log.close()

    duration = time.monotonic() - started
    data = parse_output("".join(out_chunks))
    text = (data.get("result") or "").strip()
    session_id = data.get("session_id", "") or ""

    if cancelled:
        return AgentResult(False, "Cancelled.", session_id, duration_s=duration, cancelled=True, returncode=proc.returncode)
    if timed_out:
        return AgentResult(
            False,
            f"Timed out after {settings.timeout_seconds // 60} min. Partial output, if any, is in the bot log.",
            session_id,
            duration_s=duration,
            timed_out=True,
            returncode=proc.returncode,
        )
    is_error = bool(data.get("is_error")) or proc.returncode not in (0, None)
    if is_error and not text:
        text = f"Claude exited with code {proc.returncode}." + (f"\n```{stderr_tail.strip()[-1500:]}```" if stderr_tail.strip() else "")
    return AgentResult(
        ok=not is_error,
        text=text or "(empty reply)",
        session_id=session_id,
        cost_usd=data.get("total_cost_usd"),
        duration_s=duration,
        num_turns=data.get("num_turns"),
        returncode=proc.returncode,
        stderr_tail=stderr_tail,
    )
