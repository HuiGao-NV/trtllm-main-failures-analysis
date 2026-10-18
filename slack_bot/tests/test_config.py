import json

from slack_bot.config import load_settings


def test_tokens_from_credentials_file(tmp_path, monkeypatch):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"bot_token": "xoxb-file", "app_token": "xapp-file"}))
    for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SLACK_CREDENTIALS_FILE", str(creds))
    s = load_settings(env_file=tmp_path / "nonexistent.env")
    assert s.slack_bot_token == "xoxb-file" and s.slack_app_token == "xapp-file"


def test_env_overrides_file_and_dotenv_fills_gaps(tmp_path, monkeypatch):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"bot_token": "xoxb-file", "app_token": "xapp-file"}))
    monkeypatch.setenv("SLACK_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-env")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_TIMEOUT_SECONDS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('CLAUDE_TIMEOUT_SECONDS="120"\n# comment\nSLACK_APP_TOKEN=xapp-dotenv\n')
    s = load_settings(env_file=env_file)
    assert s.slack_app_token == "xapp-env"  # real env wins over .env
    assert s.slack_bot_token == "xoxb-file"
    assert s.timeout_seconds == 120


def test_missing_everything_is_empty(tmp_path, monkeypatch):
    for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SLACK_CREDENTIALS_FILE", str(tmp_path / "nope.json"))
    s = load_settings(env_file=tmp_path / "nonexistent.env")
    assert s.slack_bot_token == "" and s.slack_app_token == ""
