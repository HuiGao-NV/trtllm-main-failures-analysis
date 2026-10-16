"""Single source of truth for how the scripts that shell out to `gh`
(get_base_commit.py, rebase_actions.py, semantic_failures_analysis.py)
authenticate to GitHub.

The token lives outside the repo in ~/.config/github/credentials.json:

    {"token": "ghp_..."}        # "github_token" / "GITHUB_TOKEN" keys also accepted

A classic PAT with no scopes, or a fine-grained PAT with read access to
NVIDIA/TensorRT-LLM, is enough (the repo is read-only for this skill).
If GH_TOKEN / GITHUB_TOKEN is already set in the environment it wins; if
neither it nor the file provides a token, `gh` falls back to its own login.
"""

import json
import os
from pathlib import Path

GITHUB_CREDS_PATH = Path.home() / ".config" / "github" / "credentials.json"
_PLACEHOLDER_PREFIX = "<"


def load_token(path: Path = GITHUB_CREDS_PATH):
    """Return the token from env or the credentials file, else None."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    for key in ("token", "github_token", "GITHUB_TOKEN"):
        tok = (data.get(key) or "").strip() if isinstance(data, dict) else ""
        if tok and not tok.startswith(_PLACEHOLDER_PREFIX):
            return tok
    return None


def gh_env():
    """Environment for `subprocess.run(["gh", ...], env=gh_env())`."""
    env = dict(os.environ)
    tok = load_token()
    if tok:
        env["GH_TOKEN"] = tok
    return env
