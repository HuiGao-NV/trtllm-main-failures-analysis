"""Handler-level tests with a fake Slack client and a stubbed run_agent."""
import threading
import time
from pathlib import Path

import pytest

from slack_bot import app as app_mod
from slack_bot.agent import AgentResult
from slack_bot.config import Settings


class FakeClient:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.uploads: list[dict] = []
        self._n = 0

    def chat_postMessage(self, **kw):
        self._n += 1
        kw["ts"] = f"100.{self._n}"
        self.posts.append(kw)
        return {"ts": kw["ts"]}

    def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def files_upload_v2(self, **kw):
        self.uploads.append(kw)
        return {"ok": True}


def _bot(tmp_path: Path, monkeypatch, result: AgentResult | None = None, delay: float = 0.0, **kw):
    skills = tmp_path / "skills" / "trtllm-main-failures-analysis"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: trtllm-main-failures-analysis\ndescription: main breaks\n---\n")
    settings = Settings(
        slack_bot_token="xoxb-t", slack_app_token="xapp-t", repo_root=tmp_path, skills_dir=tmp_path / "skills",
        state_dir=tmp_path / ".state", log_dir=tmp_path / "out", progress_interval=1, **kw,
    )
    calls: list = []

    def fake_run_agent(settings_, req, skills_summary="", log_path=None):
        calls.append(req)
        t0 = time.monotonic()
        while time.monotonic() - t0 < delay:
            if req.cancel.is_set():
                return AgentResult(False, "Cancelled.", "sess-c", cancelled=True, duration_s=delay)
            time.sleep(0.05)
        return result or AgentResult(True, "## Done\n**ok**", "sess-1", cost_usd=0.5, num_turns=2, duration_s=1.0)

    monkeypatch.setattr(app_mod, "run_agent", fake_run_agent)
    client = FakeClient()
    return app_mod.Bot(settings, client), client, calls


def _wait(bot, key, timeout=5):
    t0 = time.monotonic()
    while key in bot.active and time.monotonic() - t0 < timeout:
        time.sleep(0.02)
    time.sleep(0.1)


def test_help_and_dedupe(tmp_path, monkeypatch):
    bot, client, calls = _bot(tmp_path, monkeypatch)
    ev = {"channel": "C1", "ts": "1.0", "user": "U1", "client_msg_id": "m1", "text": "<@UB> help"}
    bot.handle(ev, ev["text"])
    bot.handle(ev, ev["text"])  # redelivery
    assert len(client.posts) == 1 and "Skills available" in client.posts[0]["text"]
    assert client.posts[0]["thread_ts"] == "1.0"
    assert not calls


def test_skill_request_runs_and_replies_in_thread(tmp_path, monkeypatch):
    bot, client, calls = _bot(tmp_path, monkeypatch)
    ev = {"channel": "C1", "ts": "2.0", "user": "U1", "text": "<@UB> /trtllm-main review case x"}
    bot.handle(ev, ev["text"])
    _wait(bot, "C1:2.0")
    assert calls[0].skill == "trtllm-main-failures-analysis" and calls[0].prompt == "review case x"
    assert calls[0].resume_session_id is None
    assert client.posts[0]["text"].startswith(":hourglass_flowing_sand:")
    assert client.updates[-1]["text"].startswith(":white_check_mark:") and "$0.50" in client.updates[-1]["text"]
    final = client.posts[-1]
    assert final["thread_ts"] == "2.0" and "*Done*" in final["text"] and "*ok*" in final["text"]
    # session stored for the thread
    assert bot.store.get_session("C1:2.0") == "sess-1"
    rows = bot.store.recent()
    assert rows[0][4] == "ok"


def test_followup_resumes_session(tmp_path, monkeypatch):
    bot, client, calls = _bot(tmp_path, monkeypatch)
    bot.store.set_session("C1:3.0", "old-sess", None)
    ev = {"channel": "C1", "ts": "3.5", "thread_ts": "3.0", "user": "U1", "text": "and the base commit?"}
    bot.handle(ev, ev["text"])
    _wait(bot, "C1:3.0")
    assert calls[0].resume_session_id == "old-sess" and calls[0].skill is None
    assert "resuming" in client.posts[0]["text"]


def test_stop_cancels_running(tmp_path, monkeypatch):
    bot, client, calls = _bot(tmp_path, monkeypatch, delay=5)
    ev = {"channel": "C1", "ts": "4.0", "user": "U1", "text": "<@UB> /trtllm-main long task"}
    bot.handle(ev, ev["text"])
    time.sleep(0.3)
    assert "C1:4.0" in bot.active
    bot.handle({"channel": "C1", "ts": "4.1", "thread_ts": "4.0", "user": "U1", "text": "stop"}, "stop")
    _wait(bot, "C1:4.0")
    assert any("Cancelling" in p["text"] for p in client.posts)
    assert client.updates[-1]["text"].startswith(":octagonal_sign:")
    assert bot.store.recent()[0][4] == "cancelled"


def test_second_request_in_same_thread_is_rejected(tmp_path, monkeypatch):
    bot, client, calls = _bot(tmp_path, monkeypatch, delay=2)
    bot.handle({"channel": "C1", "ts": "5.0", "user": "U1", "text": "q1"}, "q1")
    time.sleep(0.2)
    bot.handle({"channel": "C1", "ts": "5.1", "thread_ts": "5.0", "user": "U1", "text": "q2"}, "q2")
    assert any("already running" in p["text"] for p in client.posts)
    bot.cancel_all()
    _wait(bot, "C1:5.0")
    assert len(calls) == 1


def test_allow_list(tmp_path, monkeypatch):
    bot, client, calls = _bot(tmp_path, monkeypatch, allowed_users=["U9"])
    bot.handle({"channel": "D1", "ts": "6.0", "user": "U1", "text": "hi"}, "hi")
    assert "allow-list" in client.posts[0]["text"] and not calls


def test_long_reply_uploads_file(tmp_path, monkeypatch):
    long = AgentResult(True, "x\n" * 20000, "s", duration_s=1.0)
    bot, client, calls = _bot(tmp_path, monkeypatch, result=long)
    bot.handle({"channel": "C1", "ts": "7.0", "user": "U1", "text": "q"}, "q")
    _wait(bot, "C1:7.0")
    assert client.uploads and client.uploads[0]["thread_ts"] == "7.0"
    assert "attached as a file" in client.posts[-1]["text"]


def test_bot_subtype_and_bot_id_ignored(tmp_path, monkeypatch):
    bot, client, calls = _bot(tmp_path, monkeypatch)
    bot.handle({"channel": "C1", "ts": "8.0", "user": "U1", "bot_id": "B1", "text": "q"}, "q")
    bot.handle({"channel": "C1", "ts": "8.1", "user": "U1", "subtype": "message_changed", "text": "q"}, "q")
    assert not client.posts and not calls


def test_create_app_registers_handlers(tmp_path, monkeypatch):
    pytest.importorskip("slack_bolt")
    settings = Settings(slack_bot_token="", slack_app_token="", repo_root=tmp_path, skills_dir=tmp_path, state_dir=tmp_path / ".state", log_dir=tmp_path / "out")
    app, bot = app_mod.create_app(settings)
    assert app._ci_bot is bot


# ---- slash commands ---------------------------------------------------------

class CommandClient(FakeClient):
    def __init__(self, not_in_channel: set[str] = frozenset(), dm_for: dict[str, str] | None = None):
        super().__init__()
        self.not_in_channel = set(not_in_channel)
        self.dm_for = dm_for or {}
        self.opened: list[str] = []

    def chat_postMessage(self, **kw):
        if kw.get("channel") in self.not_in_channel:
            from slack_sdk.errors import SlackApiError

            raise SlackApiError("not_in_channel", {"ok": False, "error": "not_in_channel"})
        return super().chat_postMessage(**kw)

    def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": self.dm_for.get(users, f"D-{users}")}}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack.test/{channel}/{message_ts}"}


def _cmd(name, text="", user="U1", channel="C1", trigger="T1"):
    return {"command": name, "text": text, "user_id": user, "channel_id": channel, "trigger_id": trigger}


def _cmd_bot(tmp_path, monkeypatch, client=None, **kw):
    bot, _, calls = _bot(tmp_path, monkeypatch, **kw)
    bot.client = client or CommandClient()
    return bot, bot.client, calls


def test_analyze_command_runs_skill_and_dms_requester(tmp_path, monkeypatch):
    bot, client, calls = _cmd_bot(tmp_path, monkeypatch)
    eph = []
    bot.handle_command(_cmd("/analyze", "2 focus on B200"), respond=eph.append)
    anchor = client.posts[0]
    assert anchor["text"].startswith(":zap:") and "/analyze 2 focus on B200" in anchor["text"] and anchor.get("thread_ts") is None
    _wait(bot, f"C1:{anchor['ts']}")
    req = calls[0]
    assert req.skill == "trtllm-main-failures-analysis" and "last 2 day(s)" in req.prompt and "--days 2" in req.prompt
    assert "focus on B200" in req.prompt and req.deliver_to_user == "U1"
    # progress + result in the anchor thread
    threaded = [p for p in client.posts if p.get("thread_ts") == anchor["ts"]]
    assert threaded and "*Done*" in threaded[-1]["text"]
    # DM copy to the requester with a permalink header
    assert client.opened == ["U1"]
    dm_posts = [p for p in client.posts if p["channel"] == "D-U1"]
    assert len(dm_posts) == 2 and "slack.test/C1" in dm_posts[0]["text"] and "*Done*" in dm_posts[1]["text"]
    assert not eph
    assert bot.store.get_session(f"C1:{anchor['ts']}") == "sess-1"


def test_analyze_in_own_dm_does_not_double_post(tmp_path, monkeypatch):
    bot, client, calls = _cmd_bot(tmp_path, monkeypatch, client=CommandClient(dm_for={"U1": "D1"}))
    bot.handle_command(_cmd("/analyze", channel="D1"))
    anchor_ts = client.posts[0]["ts"]
    _wait(bot, f"D1:{anchor_ts}")
    assert "last 1 day(s)" in calls[0].prompt
    assert all(p["channel"] == "D1" for p in client.posts)
    assert sum("*Done*" in p["text"] for p in client.posts) == 1


def test_fetch_command_runs_script_and_posts_table(tmp_path, monkeypatch):
    bot, client, calls = _cmd_bot(tmp_path, monkeypatch)

    class P:
        returncode = 0
        stdout = "Wrote 2 main-break groups to x.json\n\nCONF    WAIVED  PLATFORM\nhigh    no      A100X\n"
        stderr = "note: 3 isn't a supported window ([1, 2, 7, 30]); using 7 instead\n"

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return P()

    monkeypatch.setattr(app_mod.subprocess, "run", fake_run)
    bot.handle_command(_cmd("/fetch", "3"))
    anchor_ts = client.posts[0]["ts"]
    _wait(bot, f"C1:{anchor_ts}")
    assert not calls  # no Claude run
    assert "--days" in seen["cmd"] and seen["cmd"][seen["cmd"].index("--days") + 1] == "3"
    assert seen["cmd"][1].endswith("scripts/fetch_failures.py") and seen["kw"]["cwd"] == str(tmp_path)
    assert "SLACK_BOT_TOKEN" not in seen["kw"]["env"] or not seen["kw"]["env"]["SLACK_BOT_TOKEN"]
    final = client.posts[-1]
    assert final["thread_ts"] == anchor_ts and "last 3 day(s)" in final["text"]
    assert "Wrote 2 main-break groups" in final["text"] and "using 7 instead" in final["text"] and "A100X" in final["text"]
    assert client.updates[-1]["text"].startswith(":white_check_mark:")
    assert bot.store.recent()[0][4] == "ok"


def test_fetch_command_failure_reports_stderr(tmp_path, monkeypatch):
    bot, client, calls = _cmd_bot(tmp_path, monkeypatch)

    class P:
        returncode = 2
        stdout = ""
        stderr = "Traceback: boom"

    monkeypatch.setattr(app_mod.subprocess, "run", lambda *a, **k: P())
    bot.handle_command(_cmd("/fetch"))
    anchor_ts = client.posts[0]["ts"]
    _wait(bot, f"C1:{anchor_ts}")
    assert "exited 2" in client.posts[-1]["text"] and "boom" in client.posts[-1]["text"]
    assert client.updates[-1]["text"].startswith(":x:")
    assert bot.store.recent()[0][4] == "error"


def test_command_help_dedupe_allowlist_and_not_in_channel(tmp_path, monkeypatch):
    bot, client, calls = _cmd_bot(tmp_path, monkeypatch, client=CommandClient(not_in_channel={"C9"}))
    eph = []
    bot.handle_command(_cmd("/fetch", "help", trigger="T-h"), respond=eph.append)
    assert eph and "/fetch [days]" in eph[0] and not client.posts
    bot.handle_command(_cmd("/fetch", "help", trigger="T-h"), respond=eph.append)  # redelivery
    assert len(eph) == 1
    bot.handle_command(_cmd("/fetch", channel="C9", trigger="T-c"), respond=eph.append)
    assert "not in this channel" in eph[-1] and not client.posts and not calls

    bot2, client2, calls2 = _cmd_bot(tmp_path / "b", monkeypatch, allowed_users=["U9"])
    eph2 = []
    bot2.handle_command(_cmd("/analyze"), respond=eph2.append)
    assert "allow-list" in eph2[0] and not client2.posts and not calls2
