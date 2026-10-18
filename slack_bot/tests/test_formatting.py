from slack_bot.formatting import chunk_text, md_to_mrkdwn, summary_for_file_upload


def test_md_to_mrkdwn_basics():
    md = "## Verdict\n**Regression** from [PR #1](https://g.h/1).\n- a\n- b\n1. one\n---\n"
    out = md_to_mrkdwn(md)
    assert "*Verdict*" in out
    assert "*Regression*" in out
    assert "<https://g.h/1|PR #1>" in out
    assert "• a" in out and "1. one" in out
    assert "---" not in out


def test_tables_are_fenced_and_code_untouched():
    md = "Intro\n| a | b |\n|---|---|\n| 1 | 2 |\nAfter\n```python\n**not bold**\n```"
    out = md_to_mrkdwn(md)
    assert "```\n| a | b |" in out
    assert "**not bold**" in out  # inside fence: unchanged
    assert "```python" not in out  # language tag dropped


def test_chunk_keeps_fences_balanced():
    body = "```\n" + "\n".join(f"line {i} " + "x" * 50 for i in range(200)) + "\n```\ntail"
    chunks = chunk_text(body, limit=1000)
    assert len(chunks) > 3
    for c in chunks[:-1]:
        assert c.count("```") % 2 == 0, c[-50:]
    assert "".join(chunks).count("line 199") == 1


def test_chunk_short_passthrough_and_overlong_line():
    assert chunk_text("hi") == ["hi"]
    assert chunk_text("") == []
    chunks = chunk_text("a" * 2500, limit=1000)
    assert [len(c) for c in chunks] == [1000, 1000, 500]


def test_summary_for_upload():
    s = summary_for_file_upload("para one\n" * 400)
    assert s.endswith("_(full reply attached as a file)_") and len(s) < 1600
