from app.agent_tools.user_input import extract_real_user_input


def test_extracts_cursor_user_query_tag() -> None:
    assert (
        extract_real_user_input(
            "<user_query>\n修复 summary 只保留真实用户输入\n</user_query>",
            provider="cursor_cli",
        )
        == "修复 summary 只保留真实用户输入"
    )


def test_extracts_codex_goal_objective_tag() -> None:
    assert (
        extract_real_user_input(
            "<goal_context>\n<objective>\n看一下最近 summary 的消息\n</objective>\n</goal_context>",
            provider="codex",
        )
        == "看一下最近 summary 的消息"
    )


def test_rejects_agent_default_user_context_blocks() -> None:
    assert extract_real_user_input("<user_info>\nOS Version: linux\n</user_info>", provider="cursor_cli") is None
    assert extract_real_user_input("# AGENTS.md instructions for /workspace\n\n<INSTRUCTIONS>...</INSTRUCTIONS>") is None
    assert extract_real_user_input("<turn_aborted>\nThe user interrupted the previous turn.\n</turn_aborted>") is None
    assert extract_real_user_input("<bash-input>env | grep claude</bash-input>", provider="claude_code") is None


def test_keeps_plain_human_input() -> None:
    assert extract_real_user_input("帮忙修复 docker build 报错", provider="codex") == "帮忙修复 docker build 报错"
