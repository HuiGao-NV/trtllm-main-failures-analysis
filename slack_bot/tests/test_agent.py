import json
import sys
from pathlib import Path

from slack_bot.agent import AgentRequest, build_command, build_prompt, build_system_prompt, child_env, parse_output, run_agent
from slack_bot.config import Settings


def _settings(tmp_path: Path, **kw) -> Settings:
    base = dict(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        repo_root=tmp_path,
        skills_dir=tmp_path / "skills",
        state_dir=tmp_path / ".state",
        log_dir=tmp_path / "out",
        allowed_tools=["Bash", "Read"],
        timeout_seconds=5,
    )
    base.update(kw)
    return Settings(**base)


def test_build_prompt_prefixes_skill():
    assert build_prompt(AgentRequest(prompt="review x", skill="trtllm-main-failures-analysis")) == "/trtllm-main-failures-analysis review x"
    assert build_prompt(AgentRequest(prompt="plain q")) == "plain q"


def test_build_command_flags(tmp_path):
    s = _settings(tmp_path, claude_model="opus", permission_mode="acceptEdits")
    req = AgentRequest(prompt="q", resume_session_id="abc-123")
    cmd = build_command(s, req, "SYS")
    assert cmd[:3] == ["claude", "-p", "q"]
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert cmd[cmd.index("--append-system-prompt") + 1] == "SYS"
    i = cmd.index("--allowedTools")
    assert cmd[i + 1 : i + 3] == ["Bash", "Read"]
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--resume") + 1] == "abc-123"


def test_system_prompt_mentions_slack_rules_and_skills():
    sp = build_system_prompt(AgentRequest(prompt="q", slack_user="U1", slack_channel="C1", slack_thread_ts="1.2"), "- /a: b")
    assert "Do not publish to Confluence" in sp
    assert "- /a: b" in sp and "<@U1>" in sp


def test_child_env_scrubs(tmp_path):
    env = child_env(_settings(tmp_path), {"CLAUDECODE": "1", "SLACK_BOT_TOKEN": "xoxb", "PATH": "/bin", "GITHUB_TOKEN": "gh"})
    assert "CLAUDECODE" not in env
    assert env["SLACK_BOT_TOKEN"] == "" and env["SLACK_APP_TOKEN"] == ""
    assert env["PATH"] == "/bin" and env["GITHUB_TOKEN"] == "gh"


def test_parse_output_message_list():
    msgs = [
        {"type": "system", "subtype": "init", "session_id": "s9"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "pong"}]}, "session_id": "s9"},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "pong", "session_id": "s9", "total_cost_usd": 0.05},
    ]
    d = parse_output("some warning\n" + json.dumps(msgs))
    assert d["result"] == "pong" and d["session_id"] == "s9" and d["num_turns"] == 1
    # result object without text -> fall back to assistant text
    msgs[2].pop("result")
    assert parse_output(json.dumps(msgs))["result"] == "pong"


def test_parse_output_tolerates_noise():
    assert parse_output("")["result"] if parse_output("") else True
    d = parse_output('warning line\n{"result": "ok", "session_id": "s1"}\n')
    assert d["result"] == "ok" and d["session_id"] == "s1"
    assert parse_output("not json at all")["result"] == "not json at all"


def _fake_claude(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake_claude.py"
    script.write_text(body)
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(0o755)
    return str(wrapper)


def test_run_agent_success(tmp_path):
    out = json.dumps({"result": "hello **world**", "session_id": "sess-1", "total_cost_usd": 0.12, "num_turns": 3})
    bin_ = _fake_claude(tmp_path, f"import sys\nprint({out!r})\n")
    res = run_agent(_settings(tmp_path, claude_bin=bin_), AgentRequest(prompt="q"), log_path=tmp_path / "out" / "x.log")
    assert res.ok and res.text == "hello **world**" and res.session_id == "sess-1"
    assert res.cost_usd == 0.12 and res.num_turns == 3


def test_run_agent_error_exit(tmp_path):
    bin_ = _fake_claude(tmp_path, "import sys\nsys.stderr.write('boom\\n')\nsys.exit(2)\n")
    res = run_agent(_settings(tmp_path, claude_bin=bin_), AgentRequest(prompt="q"))
    assert not res.ok and "code 2" in res.text and "boom" in res.text


def test_run_agent_timeout(tmp_path):
    bin_ = _fake_claude(tmp_path, "import time\ntime.sleep(30)\n")
    res = run_agent(_settings(tmp_path, claude_bin=bin_, timeout_seconds=1), AgentRequest(prompt="q"))
    assert res.timed_out and not res.ok and res.duration_s < 15


def test_run_agent_cancel(tmp_path):
    import threading

    bin_ = _fake_claude(tmp_path, "import time\ntime.sleep(30)\n")
    req = AgentRequest(prompt="q")
    threading.Timer(0.7, req.cancel.set).start()
    res = run_agent(_settings(tmp_path, claude_bin=bin_, timeout_seconds=20), req)
    assert res.cancelled and res.duration_s < 15


def test_missing_binary(tmp_path):
    res = run_agent(_settings(tmp_path, claude_bin="definitely-not-a-binary-xyz"), AgentRequest(prompt="q"))
    assert not res.ok and "not found" in res.text
