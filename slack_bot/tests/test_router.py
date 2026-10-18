from pathlib import Path

from slack_bot.router import clean_text, discover_skills, help_text, route


def _skills(tmp_path: Path):
    for name, desc in [("trtllm-main-failures-analysis", "Analyzes TRT-LLM main CI breaks"), ("trtllm-opensearch-queries", "OpenSearch recipes")]:
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n")
    return discover_skills(tmp_path)


def test_discover_reads_frontmatter(tmp_path):
    skills = _skills(tmp_path)
    assert set(skills) == {"trtllm-main-failures-analysis", "trtllm-opensearch-queries"}
    assert skills["trtllm-opensearch-queries"].description == "OpenSearch recipes"


def test_clean_text_strips_mention_and_links():
    t = clean_text("<@U123> check <https://x.y/z|this build> &amp; <https://a.b> please")
    assert t == "check https://x.y/z & https://a.b please"


def test_explicit_skill_with_slash(tmp_path):
    r = route("<@U1> /trtllm-main-failures-analysis review case foo::bar", _skills(tmp_path))
    assert r.kind == "skill" and r.skill.name == "trtllm-main-failures-analysis"
    assert r.prompt == "review case foo::bar"


def test_unique_prefix_and_colon_form(tmp_path):
    skills = _skills(tmp_path)
    assert route("/trtllm-main review x", skills).skill.name == "trtllm-main-failures-analysis"
    assert route("trtllm-opensearch-queries: how to list builds", skills).kind == "skill"
    # ambiguous prefix -> chat
    assert route("/trtllm anything", skills).kind == "chat"


def test_plain_question_is_chat(tmp_path):
    r = route("what's breaking on main?", _skills(tmp_path))
    assert r.kind == "chat" and r.prompt == "what's breaking on main?"


def test_stop_and_help(tmp_path):
    skills = _skills(tmp_path)
    assert route("stop", skills).kind == "stop"
    assert route("Cancel!", skills).kind == "stop"
    assert route("help", skills).kind == "help"
    assert route("", skills).kind == "help"
    assert "trtllm-opensearch-queries" in help_text(skills, "trtllm-main-failures-analysis")


def test_parse_days_forms():
    from slack_bot.router import parse_days

    assert parse_days("", 7) == (7, "")
    assert parse_days("30", 7) == (30, "")
    assert parse_days("7d", 1) == (7, "")
    assert parse_days("last 3 days", 1) == (3, "")
    assert parse_days("days=2", 1) == (2, "")
    assert parse_days("--days 2", 1) == (2, "")
    assert parse_days("2 focus on B200", 1) == (2, "focus on B200")
    assert parse_days("focus on B200 for the last 2 days", 1) == (2, "focus on B200")
    assert parse_days("B200 only", 1) == (1, "B200 only")


def test_route_command():
    from slack_bot.router import command_help_text, route_command

    assert route_command("/fetch", "") == route_command("/fetch", "   ") and route_command("/fetch", "").days == 7
    assert route_command("/fetch", "30").days == 30
    r = route_command("/analyze", "")
    assert r.kind == "analyze" and r.days == 1 and r.extra == ""
    r = route_command("/analyze", "2 focus on B200")
    assert (r.kind, r.days, r.extra) == ("analyze", 2, "focus on B200")
    assert route_command("/analyze", "help").kind == "help"
    assert route_command("/unknown", "x").kind == "help"
    assert "/fetch [days]" in command_help_text() and "/analyze [days]" in command_help_text()
