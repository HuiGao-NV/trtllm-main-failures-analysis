"""CI-analysis Slack bot (Socket Mode).

Mention the bot in a channel or DM it. Each request runs ``claude -p`` in the
CI_analysis repo (see agent.py) on a worker thread and the reply is posted in
the thread. Follow-ups in the same thread resume the same Claude session;
``stop`` cancels the running request.

Ported from repair-bot/app.py: same transport (slack_bolt Socket Mode), same
dedupe-on-redelivery guard, same "reply always in thread" rule. Differences:
one worker thread per request with a concurrency cap and a progress message,
skill routing, chunked mrkdwn replies, and a SQLite thread->session map.
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slack_bot.agent import AgentRequest, child_env, run_agent  # noqa: E402
from slack_bot.config import Settings, load_settings  # noqa: E402
from slack_bot.formatting import FILE_UPLOAD_THRESHOLD, chunk_text, md_to_mrkdwn, summary_for_file_upload  # noqa: E402
from slack_bot.router import (  # noqa: E402
    CommandRoute,
    Route,
    command_help_text,
    discover_skills,
    help_text,
    route,
    route_command,
)

FETCH_SKILL = "trtllm-main-failures-analysis"
FETCH_SCRIPT = Path(FETCH_SKILL) / "scripts" / "fetch_failures.py"
from slack_bot.store import Store  # noqa: E402

log = logging.getLogger("slack_bot")


class Dedupe:
    """Socket Mode redelivers events it did not get an ack for in time."""

    def __init__(self, ttl: float = 300.0, cap: int = 1000):
        self.ttl, self.cap = ttl, cap
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, event: dict) -> bool:
        key = event.get("client_msg_id") or f"{event.get('channel')}:{event.get('ts')}"
        now = time.time()
        with self._lock:
            while self._seen and (now - next(iter(self._seen.values())) > self.ttl or len(self._seen) > self.cap):
                self._seen.popitem(last=False)
            if key in self._seen:
                return True
            self._seen[key] = now
            return False


class Bot:
    def __init__(self, settings: Settings, client):
        self.settings = settings
        self.client = client
        self.store = Store(settings.db_path)
        self.dedupe = Dedupe()
        self.skills = discover_skills(settings.skills_dir)
        self.active: dict[str, AgentRequest] = {}  # thread_key -> running request
        self._active_lock = threading.Lock()
        self._dm_channels: dict[str, str] = {}  # user id -> DM channel id
        self._slots = threading.BoundedSemaphore(settings.max_concurrent)
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        orphans = self.store.mark_orphans()
        if orphans:
            log.warning("marked %d request(s) from a previous run as orphaned", orphans)
        log.info("skills: %s", ", ".join(self.skills) or "(none found under %s)" % settings.skills_dir)

    # -- helpers -----------------------------------------------------------
    def skills_summary(self) -> str:
        return "\n".join(f"- /{n}: {s.description}" for n, s in self.skills.items())

    def post(self, channel: str, thread_ts: str, text: str, **kw):
        return self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, **kw)

    def post_long(self, channel: str, thread_ts: str, markdown: str, title: str = "reply") -> None:
        text = md_to_mrkdwn(markdown)
        if len(text) > FILE_UPLOAD_THRESHOLD:
            self.post(channel, thread_ts, summary_for_file_upload(text))
            try:
                self.client.files_upload_v2(
                    channel=channel, thread_ts=thread_ts, filename=f"{title}.md", content=markdown, title=title
                )
                return
            except Exception as e:  # files:write scope missing, etc. - fall back to chunks
                log.warning("file upload failed (%s); posting chunks", e)
        for chunk in chunk_text(text):
            self.post(channel, thread_ts, chunk)

    def allowed(self, user_id: str) -> bool:
        return not self.settings.allowed_users or user_id in self.settings.allowed_users

    def dm_channel(self, user_id: str) -> str | None:
        """Open (or reuse) the bot's DM with a user; None if Slack refuses."""
        ch = self._dm_channels.get(user_id)
        if ch:
            return ch
        try:
            ch = self.client.conversations_open(users=user_id)["channel"]["id"]
        except Exception as e:  # noqa: BLE001
            log.warning("conversations.open failed for %s: %s", user_id, e)
            return None
        self._dm_channels[user_id] = ch
        return ch

    def permalink(self, channel: str, ts: str) -> str:
        try:
            return self.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
        except Exception as e:  # noqa: BLE001
            log.debug("chat.getPermalink failed: %s", e)
            return ""

    def deliver_to_user(self, user_id: str, channel: str, thread_ts: str, markdown: str, title: str) -> None:
        """DM a copy of a reply to the requester unless the reply is already in that DM."""
        dm = self.dm_channel(user_id)
        if not dm or dm == channel:
            return
        link = self.permalink(channel, thread_ts)
        header = f"Here is the report you requested with `{title}`" + (f" (<{link}|thread>)." if link else ".")
        self.post(dm, None, header)
        self.post_long(dm, None, markdown, title=title)

    # -- entry point from handlers ------------------------------------------
    def handle(self, event: dict, text: str) -> None:
        if event.get("bot_id") or event.get("subtype"):
            return
        if self.dedupe.seen(event):
            return
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user = event.get("user", "")
        thread_key = f"{channel}:{thread_ts}"

        if not self.allowed(user):
            self.post(channel, thread_ts, "Sorry, you are not on this bot's allow-list.")
            return

        r: Route = route(text, self.skills)
        if r.kind == "help":
            self.post(channel, thread_ts, help_text(self.skills, self.settings.default_skill))
            return
        if r.kind == "stop":
            with self._active_lock:
                req = self.active.get(thread_key)
            if req:
                req.cancel.set()
                self.post(channel, thread_ts, ":octagonal_sign: Cancelling…")
            else:
                self.post(channel, thread_ts, "Nothing is running in this thread.")
            return

        self._dispatch(
            channel,
            thread_ts,
            user,
            AgentRequest(
                prompt=r.prompt,
                skill=r.skill.name if r.skill else None,
                resume_session_id=self.store.get_session(thread_key),
                slack_user=user,
                slack_channel=channel,
                slack_thread_ts=thread_ts,
            ),
        )

    def _dispatch(self, channel: str, thread_ts: str, user: str, req: AgentRequest, target=None) -> bool:
        """Register the request for its thread and start the worker. One
        request per thread; ``target`` defaults to the Claude worker."""
        thread_key = f"{channel}:{thread_ts}"
        with self._active_lock:
            if thread_key in self.active:
                self.post(channel, thread_ts, "A request is already running in this thread. Reply `stop` to cancel it first.")
                return False
            self.active[thread_key] = req
        threading.Thread(target=target or self._run, args=(thread_key, channel, thread_ts, user, req), daemon=True).start()
        return True

    # -- slash commands -------------------------------------------------------
    def handle_command(self, cmd: dict, respond=None) -> None:
        """``/fetch [days]`` and ``/analyze [days] [extra]`` (already ack'ed by
        the Bolt handler). ``respond`` posts an ephemeral message to the invoker."""

        def ephemeral(text: str) -> None:
            if respond is not None:
                try:
                    respond(text)
                    return
                except Exception as e:  # noqa: BLE001
                    log.debug("respond failed: %s", e)
            if cmd.get("channel_id"):
                self.post(cmd["channel_id"], None, text)

        if self.dedupe.seen({"client_msg_id": cmd.get("trigger_id"), "channel": cmd.get("channel_id"), "ts": cmd.get("command")}):
            return
        channel = cmd.get("channel_id", "")
        user = cmd.get("user_id", "")
        command = (cmd.get("command") or "").strip()
        text = (cmd.get("text") or "").strip()
        if not self.allowed(user):
            ephemeral("Sorry, you are not on this bot's allow-list.")
            return
        r: CommandRoute = route_command(command, text)
        if r.kind == "help":
            ephemeral(command_help_text())
            return
        shown = f"{command} {text}".strip()
        try:
            anchor = self.post(channel, None, f":zap: <@{user}> ran `{shown}`")
        except Exception as e:  # noqa: BLE001
            resp = getattr(e, "response", None)  # SlackResponse or dict; both support .get
            try:
                code = resp.get("error", "") if resp is not None else ""
            except Exception:  # noqa: BLE001
                code = ""
            if code in ("not_in_channel", "channel_not_found") or "not_in_channel" in str(e):
                ephemeral("I am not in this channel. Invite me (`/invite @Main break failures notifications`) or run the command in my DM.")
            else:
                ephemeral(f"Sorry, I could not post here: {e}")
            return
        thread_ts = anchor["ts"]
        if r.kind == "fetch":
            req = AgentRequest(
                prompt=f"fetch --days {r.days}", skill="fetch", slack_user=user, slack_channel=channel,
                slack_thread_ts=thread_ts, extra={"days": r.days},
            )
            self._dispatch(channel, thread_ts, user, req, target=self._run_fetch)
            return
        prompt = (
            f"Analyze TRT-LLM main-branch CI failures for the last {r.days} day(s) (use --days {r.days}). "
            "Report-only run: do not publish to Confluence or Google Sheets and do not send Slack messages; "
            "the bot delivers the report. Write the full report as your final message."
        )
        if r.extra:
            prompt += f"\nAdditional instructions from the requester: {r.extra}"
        req = AgentRequest(
            prompt=prompt,
            skill=FETCH_SKILL,
            slack_user=user,
            slack_channel=channel,
            slack_thread_ts=thread_ts,
            deliver_to_user=user,
        )
        self._dispatch(channel, thread_ts, user, req)

    def _run_fetch(self, thread_key: str, channel: str, thread_ts: str, user: str, req: AgentRequest) -> None:
        days = int(req.extra.get("days", 7))
        status = self.post(channel, thread_ts, f":hourglass_flowing_sand: Fetching main-break detections for the last {days} day(s)…")
        req_id = self.store.start_request(thread_key, user, "fetch", req.prompt)
        started = time.monotonic()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        run_dir = self.settings.repo_root / today
        out_path = run_dir / f"trtllm-failures-{days}d-{today}.json"
        script = self.settings.skills_dir / FETCH_SCRIPT
        cmd = [sys.executable, str(script), "--days", str(days), "--out", str(out_path)]
        ok, text = False, ""
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                cmd, cwd=str(self.settings.repo_root), env=child_env(self.settings), capture_output=True, text=True,
                timeout=self.settings.fetch_timeout_seconds,
            )
            stderr = proc.stderr.strip()
            note = "\n".join(line for line in stderr.splitlines() if line.startswith("note:"))
            if proc.returncode == 0:
                ok = True
                lines = proc.stdout.strip().splitlines()
                header = lines[0] if lines else ""
                table = "\n".join(lines[1:]).strip()
                rel = out_path.relative_to(self.settings.repo_root) if out_path.is_relative_to(self.settings.repo_root) else out_path
                text = f"*Main-break detections, last {days} day(s)*\n{header}\nJSON: `{rel}`"
                if note:
                    text += f"\n_{note}_"
                if table:
                    text += f"\n```\n{table}\n```"
            else:
                text = f"fetch_failures.py exited {proc.returncode}\n```\n{stderr[-1500:] or proc.stdout[-1500:]}\n```"
        except subprocess.TimeoutExpired:
            text = f"fetch_failures.py timed out after {self.settings.fetch_timeout_seconds}s."
        except Exception as e:  # noqa: BLE001
            text = f"fetch failed: {e}"
        finally:
            with self._active_lock:
                self.active.pop(thread_key, None)
        secs = time.monotonic() - started
        try:
            self.client.chat_update(
                channel=channel, ts=status["ts"],
                text=(f":white_check_mark: Fetched in {secs:.0f}s." if ok else f":x: Fetch failed after {secs:.0f}s."),
            )
        except Exception as e:  # noqa: BLE001
            log.debug("chat_update failed: %s", e)
        self.store.finish_request(req_id, "ok" if ok else "error", "", None, secs)
        for chunk in chunk_text(text):
            self.post(channel, thread_ts, chunk)

    # -- worker --------------------------------------------------------------
    def _run(self, thread_key: str, channel: str, thread_ts: str, user: str, req: AgentRequest) -> None:
        label = f"`/{req.skill}`" if req.skill else "your question"
        resumed = " (resuming this thread's session)" if req.resume_session_id else ""
        status = self.post(channel, thread_ts, f":hourglass_flowing_sand: Working on {label}{resumed}…")
        status_ts = status["ts"]
        req_id = self.store.start_request(thread_key, user, req.skill, req.prompt)
        started = time.monotonic()

        def update(msg: str) -> None:
            try:
                self.client.chat_update(channel=channel, ts=status_ts, text=msg)
            except Exception as e:  # noqa: BLE001
                log.debug("chat_update failed: %s", e)

        got_slot = self._slots.acquire(timeout=0.1)
        if not got_slot:
            update(f":hourglass: Queued behind {self.settings.max_concurrent} running request(s)…")
            self._slots.acquire()
        try:
            if req.cancel.is_set():
                update(":octagonal_sign: Cancelled before start.")
                self.store.finish_request(req_id, "cancelled", "", None, 0.0)
                return
            update(f":hourglass_flowing_sand: Working on {label}{resumed}… (0 min)")
            progress_stop = threading.Event()

            def progress() -> None:
                while not progress_stop.wait(self.settings.progress_interval):
                    mins = int((time.monotonic() - started) // 60)
                    update(f":hourglass_flowing_sand: Working on {label}{resumed}… ({mins} min)")

            threading.Thread(target=progress, daemon=True).start()
            log_path = self.settings.log_dir / f"claude_{thread_key.replace(':', '_').replace('.', '_')}.log"
            result = run_agent(self.settings, req, self.skills_summary(), log_path=log_path)
            progress_stop.set()
        finally:
            self._slots.release()
            with self._active_lock:
                self.active.pop(thread_key, None)

        mins = result.duration_s / 60
        if result.session_id:
            self.store.set_session(thread_key, result.session_id, req.skill)
        if result.cancelled:
            update(f":octagonal_sign: Cancelled after {mins:.1f} min.")
            self.store.finish_request(req_id, "cancelled", result.session_id, result.cost_usd, result.duration_s)
            return
        if result.timed_out:
            update(f":alarm_clock: {result.text}")
            self.store.finish_request(req_id, "timeout", result.session_id, result.cost_usd, result.duration_s)
            return
        footer = f"_{mins:.1f} min"
        if result.num_turns:
            footer += f" · {result.num_turns} turns"
        if result.cost_usd is not None:
            footer += f" · ${result.cost_usd:.2f}"
        footer += "_"
        if result.ok:
            update(f":white_check_mark: Done — {label}. {footer}")
            self.store.finish_request(req_id, "ok", result.session_id, result.cost_usd, result.duration_s)
        else:
            update(f":x: Failed — {label}. {footer}")
            self.store.finish_request(req_id, "error", result.session_id, result.cost_usd, result.duration_s)
        self.post_long(channel, thread_ts, result.text, title=(req.skill or "reply"))
        if req.deliver_to_user:
            try:
                self.deliver_to_user(req.deliver_to_user, channel, thread_ts, result.text, title=(req.skill or "reply"))
            except Exception as e:  # noqa: BLE001
                log.warning("DM delivery to %s failed: %s", req.deliver_to_user, e)

    def cancel_all(self) -> None:
        with self._active_lock:
            for req in self.active.values():
                req.cancel.set()


def create_app(settings: Settings | None = None):
    from slack_bolt import App

    settings = settings or load_settings()
    # Bolt eagerly calls auth.test on the token; skip that when running without
    # a real token (tests / import) - Socket Mode start still fails loudly.
    app = App(
        token=settings.slack_bot_token or "xoxb-placeholder-for-import",
        token_verification_enabled=bool(settings.slack_bot_token),
    )
    bot = Bot(settings, app.client)

    @app.event("app_mention")
    def on_mention(event, logger):  # noqa: ANN001
        try:
            bot.handle(event, event.get("text", ""))
        except Exception as e:  # noqa: BLE001
            logger.exception("mention handler failed")
            bot.post(event["channel"], event.get("thread_ts") or event["ts"], f"Sorry, an error occurred: {e}")

    @app.event("message")
    def on_message(event, logger):  # noqa: ANN001
        # DMs only; channel messages must mention the bot (handled above)
        if event.get("channel_type") != "im":
            return
        try:
            bot.handle(event, event.get("text", ""))
        except Exception as e:  # noqa: BLE001
            logger.exception("message handler failed")
            bot.post(event["channel"], event.get("thread_ts") or event["ts"], f"Sorry, an error occurred: {e}")

    @app.command("/analyze")
    def on_analyze(ack, command, respond, logger):  # noqa: ANN001
        ack()  # Slack requires the ack within 3 s; the work runs on a worker thread
        try:
            bot.handle_command(command, respond)
        except Exception as e:  # noqa: BLE001
            logger.exception("/analyze handler failed")
            respond(f"Sorry, an error occurred: {e}")

    @app.command("/fetch")
    def on_fetch(ack, command, respond, logger):  # noqa: ANN001
        ack()
        try:
            bot.handle_command(command, respond)
        except Exception as e:  # noqa: BLE001
            logger.exception("/fetch handler failed")
            respond(f"Sorry, an error occurred: {e}")

    app._ci_bot = bot  # for tests / introspection
    return app, bot


def main() -> int:
    logging.basicConfig(level=os.environ.get("SLACK_BOT_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    where = "env / slack_bot/.env / ~/.config/slack/credentials.json (keys bot_token, app_token)"
    if not settings.slack_bot_token:
        print(f"SLACK_BOT_TOKEN (xoxb-…) missing; looked in {where}", file=sys.stderr)
        return 1
    if not settings.slack_app_token:
        print(f"SLACK_APP_TOKEN (xapp-…, scope connections:write) missing; looked in {where}", file=sys.stderr)
        return 1
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app, bot = create_app(settings)

    def shutdown(*_):
        bot.cancel_all()

    atexit.register(shutdown)
    signal.signal(signal.SIGTERM, lambda *a: (shutdown(), sys.exit(0)))
    log.info("starting Socket Mode; repo=%s skills=%s", settings.repo_root, settings.skills_dir)
    SocketModeHandler(app, settings.slack_app_token).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
