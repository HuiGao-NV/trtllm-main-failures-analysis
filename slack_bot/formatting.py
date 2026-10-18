"""Markdown -> Slack mrkdwn, and splitting long replies into Slack-sized posts.

Slack hard-limits ``chat.postMessage`` text to 40k characters, but anything
over ~4k renders badly and gets a "show more" fold, so we post 3,900-char
chunks and never split inside a fenced code block.
"""
from __future__ import annotations

import re

MAX_CHUNK = 3900
FILE_UPLOAD_THRESHOLD = 15000

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", re.M)
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+", re.M)
_NUM_RE = re.compile(r"^(\s*)(\d+)\.\s+", re.M)


def _convert_segment(md: str) -> str:
    md = _HEADER_RE.sub(lambda m: f"*{m.group(2).strip()}*", md)
    md = _BOLD_RE.sub(r"*\1*", md)
    md = _LINK_RE.sub(r"<\2|\1>", md)
    md = _HR_RE.sub("", md)
    md = _BULLET_RE.sub(r"\1• ", md)
    md = _NUM_RE.sub(r"\1\2. ", md)
    return md


def _wrap_tables(md: str) -> str:
    """Slack has no tables; keep them readable by fencing each one."""
    out, buf = [], []
    in_table = False
    for line in md.splitlines():
        is_row = line.lstrip().startswith("|") and line.rstrip().endswith("|")
        if is_row:
            buf.append(line)
            in_table = True
            continue
        if in_table:
            out.append("```\n" + "\n".join(buf) + "\n```")
            buf, in_table = [], False
        out.append(line)
    if buf:
        out.append("```\n" + "\n".join(buf) + "\n```")
    return "\n".join(out)


def md_to_mrkdwn(md: str) -> str:
    md = _wrap_tables(md or "")
    parts = md.split("```")
    # even indices are prose, odd are inside code fences
    for i in range(0, len(parts), 2):
        parts[i] = _convert_segment(parts[i])
    for i in range(1, len(parts), 2):
        # drop the language tag Slack would otherwise print as text
        first_nl = parts[i].find("\n")
        if first_nl > 0 and re.fullmatch(r"[A-Za-z0-9_+-]+", parts[i][:first_nl].strip()):
            parts[i] = parts[i][first_nl:]
    return "```".join(parts).strip()


def chunk_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split on paragraph/line boundaries, keeping fenced blocks intact by
    closing and reopening them across chunk edges."""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    in_fence = False
    for line in text.split("\n"):
        add = len(line) + 1
        if cur_len + add > limit and cur:
            if in_fence:
                cur.append("```")
            chunks.append("\n".join(cur).strip())
            cur = ["```"] if in_fence else []
            cur_len = 4 if in_fence else 0
        # a single over-long line is hard-split
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        cur.append(line)
        cur_len += add
        if line.strip().startswith("```"):
            in_fence = not in_fence
    if cur:
        chunks.append("\n".join(cur).strip())
    return [c for c in chunks if c]


def summary_for_file_upload(text: str, head_chars: int = 1500) -> str:
    head = text[:head_chars]
    cut = head.rfind("\n")
    if cut > head_chars // 2:
        head = head[:cut]
    return head.rstrip() + "\n\n_(full reply attached as a file)_"
