"""Configuration for the CI-analysis Slack bot.

Everything comes from environment variables (loaded from ``slack_bot/.env`` if
present). The Slack bot token also falls back to the credentials file that
``skills/trtllm-main-failures-analysis/scripts/post_slack_message.py`` already
uses (``~/.config/slack/credentials.json`` -> ``{"bot_token": "xoxb-..."}``),
so an existing DM setup needs only the new ``xapp-`` app-level token.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
REPO_ROOT = BOT_DIR.parent
DEFAULT_CREDS_PATH = Path.home() / ".config" / "slack" / "credentials.json"


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE, '#' comments, optional quotes). Values in
    the real environment win over the file."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _tokens_from_file(path: Path) -> dict[str, str]:
    """``{"bot_token": "xoxb-...", "app_token": "xapp-..."}``; both keys optional."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in data.items() if isinstance(v, str)}


def _split(value: str) -> list[str]:
    return [v for v in value.replace(",", " ").split() if v]


@dataclass
class Settings:
    slack_bot_token: str
    slack_app_token: str
    repo_root: Path = REPO_ROOT
    skills_dir: Path = REPO_ROOT / "skills"
    state_dir: Path = BOT_DIR / ".state"
    log_dir: Path = BOT_DIR / "out"
    claude_bin: str = "claude"
    claude_model: str = ""
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    timeout_seconds: int = 3600
    fetch_timeout_seconds: int = 300  # /fetch runs fetch_failures.py directly
    max_concurrent: int = 2
    progress_interval: int = 60
    allowed_users: list[str] = field(default_factory=list)  # Slack user ids; empty = anyone
    default_skill: str = "trtllm-main-failures-analysis"
    scrub_env_keys: list[str] = field(default_factory=lambda: ["CLAUDECODE", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])

    @property
    def db_path(self) -> Path:
        return self.state_dir / "slack_bot.db"


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or BOT_DIR / ".env")
    creds = _tokens_from_file(Path(os.environ.get("SLACK_CREDENTIALS_FILE", DEFAULT_CREDS_PATH)))
    bot_token = os.environ.get("SLACK_BOT_TOKEN") or creds.get("bot_token", "")
    app_token = os.environ.get("SLACK_APP_TOKEN") or creds.get("app_token", "")
    allowed = _split(
        os.environ.get(
            "CLAUDE_ALLOWED_TOOLS",
            "Bash Read Write Edit Glob Grep Agent Skill ToolSearch WebFetch WebSearch",
        )
    )
    return Settings(
        slack_bot_token=bot_token,
        slack_app_token=app_token,
        repo_root=Path(os.environ.get("CI_ANALYSIS_REPO", REPO_ROOT)).resolve(),
        skills_dir=Path(os.environ.get("CI_ANALYSIS_SKILLS_DIR", REPO_ROOT / "skills")).resolve(),
        state_dir=Path(os.environ.get("SLACK_BOT_STATE_DIR", BOT_DIR / ".state")),
        log_dir=Path(os.environ.get("SLACK_BOT_LOG_DIR", BOT_DIR / "out")),
        claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
        claude_model=os.environ.get("CLAUDE_MODEL", ""),
        permission_mode=os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits"),
        allowed_tools=allowed,
        timeout_seconds=int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "3600")),
        fetch_timeout_seconds=int(os.environ.get("SLACK_BOT_FETCH_TIMEOUT", "300")),
        max_concurrent=int(os.environ.get("SLACK_BOT_MAX_CONCURRENT", "2")),
        progress_interval=int(os.environ.get("SLACK_BOT_PROGRESS_INTERVAL", "60")),
        allowed_users=_split(os.environ.get("SLACK_BOT_ALLOWED_USERS", "")),
        default_skill=os.environ.get("SLACK_BOT_DEFAULT_SKILL", "trtllm-main-failures-analysis"),
    )
