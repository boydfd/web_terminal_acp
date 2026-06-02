import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from app.services.agent_config import (
    AgentConfigItemSelection,
    AgentConfigSectionSelection,
    AgentConfigSelection,
    apply_agent_config_selection,
    list_agent_config,
    set_agent_config_item_enabled,
)
from app.services import agent_config as agent_config_service
from app.services import agent_profiles as agent_profile_service


def section_items(config, section: str):
    for candidate in config.sections:
        if candidate.id == section:
            return {item.id: item for item in candidate.items}
    raise AssertionError(f"missing section: {section}")


def write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


def test_codex_config_lists_user_skills_plugins_and_updates_enablement(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    write_skill(codex_home / "skills", "docker")
    write_skill(codex_home / "skills.disabled", "sleepy")
    plugin_manifest = (
        codex_home
        / "plugins"
        / "cache"
        / "openai-curated"
        / "superpowers"
        / "acdd3141"
        / ".codex-plugin"
        / "plugin.json"
    )
    plugin_manifest.parent.mkdir(parents=True)
    plugin_manifest.write_text(
        json.dumps(
            {
                "name": "superpowers",
                "interface": {"displayName": "Superpowers"},
            }
        ),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        '[plugins."superpowers@openai-curated"]\nenabled = false\n',
        encoding="utf-8",
    )

    config = list_agent_config("codex", home=tmp_path)

    assert config.agent == "codex"
    assert section_items(config, "skills")["docker"].enabled is True
    assert section_items(config, "skills")["sleepy"].enabled is False
    plugin = section_items(config, "plugins")["superpowers@openai-curated"]
    assert plugin.name == "Superpowers"
    assert plugin.enabled is False

    set_agent_config_item_enabled("codex", "skills", "sleepy", True, home=tmp_path)
    assert (codex_home / "skills" / "sleepy" / "SKILL.md").is_file()
    assert not (codex_home / "skills.disabled" / "sleepy").exists()

    set_agent_config_item_enabled(
        "codex",
        "plugins",
        "superpowers@openai-curated",
        True,
        home=tmp_path,
    )
    assert 'enabled = true' in (codex_home / "config.toml").read_text(encoding="utf-8")


def test_codex_plugin_config_does_not_read_or_rewrite_other_toml_sections(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    plugin_manifest = (
        codex_home
        / "plugins"
        / "cache"
        / "openai-curated"
        / "superpowers"
        / "acdd3141"
        / ".codex-plugin"
        / "plugin.json"
    )
    plugin_manifest.parent.mkdir(parents=True)
    plugin_manifest.write_text(json.dumps({"name": "superpowers"}), encoding="utf-8")
    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                '[plugins."superpowers@openai-curated"]',
                "enabled = true",
                "",
                "[profiles.default]",
                "enabled = false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = list_agent_config("codex", home=tmp_path)

    assert section_items(config, "plugins")["superpowers@openai-curated"].enabled is True

    set_agent_config_item_enabled(
        "codex",
        "plugins",
        "superpowers@openai-curated",
        False,
        home=tmp_path,
    )
    updated = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert '[plugins."superpowers@openai-curated"]\nenabled = false' in updated
    assert "[profiles.default]\nenabled = false" in updated


def test_apply_agent_config_selection_materializes_per_window_home(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    write_skill(codex_home / "skills", "docker")
    write_skill(codex_home / "skills.disabled", "sleepy")
    (codex_home / "config.toml").write_text(
        '[plugins."superpowers@openai-curated"]\nenabled = true\n',
        encoding="utf-8",
    )
    (codex_home / "history.jsonl").write_text('{"prompt":"fix"}\n', encoding="utf-8")

    config = apply_agent_config_selection(
        AgentConfigSelection(
            agent="codex",
            sections=[
                AgentConfigSectionSelection(
                    id="skills",
                    items=[
                        AgentConfigItemSelection(id="docker", enabled=False),
                        AgentConfigItemSelection(id="sleepy", enabled=True),
                    ],
                ),
                AgentConfigSectionSelection(
                    id="plugins",
                    items=[
                        AgentConfigItemSelection(id="superpowers@openai-curated", enabled=False),
                    ],
                ),
            ],
        ),
        window_id="window-1",
        home=tmp_path,
    )

    managed = tmp_path / ".web-terminal-acp" / "codex-homes" / "window-1"
    assert config.agent == "codex"
    assert (managed / "skills.disabled" / "docker" / "SKILL.md").is_file()
    assert (managed / "skills" / "sleepy" / "SKILL.md").is_file()
    assert 'enabled = false' in (managed / "config.toml").read_text(encoding="utf-8")
    assert (managed / "history.jsonl").resolve() == codex_home / "history.jsonl"
    assert (codex_home / "skills" / "docker" / "SKILL.md").is_file()
    assert (codex_home / "skills.disabled" / "sleepy" / "SKILL.md").is_file()


def test_list_window_agent_config_reads_existing_managed_home(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    write_skill(codex_home / "skills", "docker")
    write_skill(codex_home / "skills", "review")
    managed = tmp_path / ".web-terminal-acp" / "codex-homes" / "window-1"
    write_skill(managed / "skills.disabled", "docker")

    config = agent_config_service.list_window_agent_config(
        "codex",
        window_id="window-1",
        home=tmp_path,
    )

    assert section_items(config, "skills")["docker"].enabled is False
    assert section_items(config, "skills")["review"].enabled is True


def test_set_window_agent_config_detaches_shell_symlinked_skill_config(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    write_skill(codex_home / "skills", "docker")
    (codex_home / "skills.disabled").mkdir(parents=True)
    managed = tmp_path / ".web-terminal-acp" / "codex-homes" / "window-1"
    managed.mkdir(parents=True)
    (managed / "skills").symlink_to(codex_home / "skills")
    (managed / "skills.disabled").symlink_to(codex_home / "skills.disabled")

    config = agent_config_service.set_window_agent_config_item_enabled(
        "codex",
        "skills",
        "docker",
        False,
        window_id="window-1",
        home=tmp_path,
    )

    assert section_items(config, "skills")["docker"].enabled is False
    assert not (managed / "skills").is_symlink()
    assert not (managed / "skills.disabled").is_symlink()
    assert (managed / "skills.disabled" / "docker" / "SKILL.md").is_file()
    assert (codex_home / "skills" / "docker" / "SKILL.md").is_file()
    assert not (codex_home / "skills.disabled" / "docker").exists()


def test_set_window_agent_config_detaches_shell_symlinked_codex_plugin_config(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    plugin_manifest = (
        codex_home
        / "plugins"
        / "cache"
        / "openai-curated"
        / "superpowers"
        / "acdd3141"
        / ".codex-plugin"
        / "plugin.json"
    )
    plugin_manifest.parent.mkdir(parents=True)
    plugin_manifest.write_text(json.dumps({"name": "superpowers"}), encoding="utf-8")
    (codex_home / "config.toml").write_text(
        '[plugins."superpowers@openai-curated"]\nenabled = true\n',
        encoding="utf-8",
    )
    managed = tmp_path / ".web-terminal-acp" / "codex-homes" / "window-1"
    managed.mkdir(parents=True)
    (managed / "plugins").symlink_to(codex_home / "plugins")
    (managed / "config.toml").symlink_to(codex_home / "config.toml")

    config = agent_config_service.set_window_agent_config_item_enabled(
        "codex",
        "plugins",
        "superpowers@openai-curated",
        False,
        window_id="window-1",
        home=tmp_path,
    )

    assert section_items(config, "plugins")["superpowers@openai-curated"].enabled is False
    assert not (managed / "config.toml").is_symlink()
    assert 'enabled = false' in (managed / "config.toml").read_text(encoding="utf-8")
    assert 'enabled = true' in (codex_home / "config.toml").read_text(encoding="utf-8")


def test_apply_agent_config_selection_links_history_for_claude_and_cursor(tmp_path: Path) -> None:
    claude_home = tmp_path / ".claude"
    cursor_home = tmp_path / ".cursor"
    claude_home.mkdir()
    cursor_home.mkdir()
    (claude_home / "history.jsonl").write_text('{"display":"fix"}\n', encoding="utf-8")
    (claude_home / "file-history").mkdir()
    (cursor_home / "chats").mkdir()
    (cursor_home / "chats" / "marker").write_text("cursor chat", encoding="utf-8")

    apply_agent_config_selection(
        AgentConfigSelection(agent="claude", sections=[]),
        window_id="window-1",
        home=tmp_path,
    )
    apply_agent_config_selection(
        AgentConfigSelection(agent="cursor", sections=[]),
        window_id="window-1",
        home=tmp_path,
    )

    managed_claude = tmp_path / ".web-terminal-acp" / "claude-code-homes" / "window-1"
    managed_cursor = tmp_path / ".web-terminal-acp" / "cursor-homes" / "window-1"
    assert (managed_claude / "history.jsonl").resolve() == claude_home / "history.jsonl"
    assert (managed_claude / "file-history").resolve() == claude_home / "file-history"
    assert (managed_cursor / "chats").resolve() == cursor_home / "chats"


def test_directory_config_rejects_path_traversal_item_id(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    write_skill(codex_home / "skills", "docker")

    with pytest.raises(ValueError, match="invalid config item id"):
        set_agent_config_item_enabled("codex", "skills", "../escape", False, home=tmp_path)

    assert (codex_home / "skills" / "docker" / "SKILL.md").is_file()
    assert not (tmp_path / "escape").exists()


def test_codex_plugin_config_rejects_unsafe_toml_item_id(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()

    unsafe_ids = [
        'safe"]\nenabled = true\n[plugins."injected',
        r"marketplace\plugin",
    ]
    for item_id in unsafe_ids:
        with pytest.raises(ValueError, match="invalid config item id"):
            set_agent_config_item_enabled(
                "codex",
                "plugins",
                item_id,
                False,
                home=tmp_path,
            )

    assert not (codex_home / "config.toml").exists()


def test_claude_plugin_config_rejects_control_character_item_id(tmp_path: Path) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()

    with pytest.raises(ValueError, match="invalid config item id"):
        set_agent_config_item_enabled(
            "claude",
            "plugins",
            "unsafe\x00plugin",
            False,
            home=tmp_path,
        )

    assert not (claude_home / "settings.json").exists()


def test_claude_plugin_config_concurrent_updates_preserve_distinct_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"one": True, "two": True}}),
        encoding="utf-8",
    )
    first_write_started = Event()
    release_first_write = Event()
    second_write_started = Event()
    original_write = agent_config_service._write_text_file_atomic
    delayed = False

    def slow_first_write(path: Path, content: str) -> None:
        nonlocal delayed
        if path == claude_home / "settings.json":
            if not delayed:
                delayed = True
                first_write_started.set()
                assert release_first_write.wait(timeout=5)
            else:
                second_write_started.set()
        original_write(path, content)

    monkeypatch.setattr(agent_config_service, "_write_text_file_atomic", slow_first_write)

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_one = executor.submit(
            set_agent_config_item_enabled,
            "claude",
            "plugins",
            "one",
            False,
            home=tmp_path,
        )
        assert first_write_started.wait(timeout=5)
        future_two = executor.submit(
            set_agent_config_item_enabled,
            "claude",
            "plugins",
            "two",
            False,
            home=tmp_path,
        )
        assert not second_write_started.wait(timeout=0.2)
        release_first_write.set()
        future_one.result()
        future_two.result()

    settings = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
    assert settings["enabledPlugins"] == {"one": False, "two": False}


def test_agent_profile_config_concurrent_updates_preserve_distinct_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_home = tmp_path / ".claude"
    (claude_home / "settings.json").parent.mkdir(parents=True)
    (claude_home / "plugins").mkdir(parents=True)
    (claude_home / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"one": [], "two": []}}),
        encoding="utf-8",
    )
    (claude_home / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"one": True, "two": True}}),
        encoding="utf-8",
    )
    profile = agent_profile_service.create_agent_profile(
        name="Builder",
        default_agent_client="claude",
        home=tmp_path,
    )
    profile_manifest = tmp_path / ".web-terminal-acp" / "agents" / profile.id / "profile.json"
    first_write_started = Event()
    release_first_write = Event()
    second_write_started = Event()
    original_write = agent_config_service._write_text_file_atomic
    delayed = False

    def slow_first_write(path: Path, content: str) -> None:
        nonlocal delayed
        if path == profile_manifest:
            if not delayed:
                delayed = True
                first_write_started.set()
                assert release_first_write.wait(timeout=5)
            else:
                second_write_started.set()
        original_write(path, content)

    monkeypatch.setattr(agent_config_service, "_write_text_file_atomic", slow_first_write)

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_one = executor.submit(
            agent_profile_service.set_agent_profile_config_item_enabled,
            profile.id,
            "claude",
            "plugins",
            "one",
            False,
            home=tmp_path,
        )
        assert first_write_started.wait(timeout=5)
        future_two = executor.submit(
            agent_profile_service.set_agent_profile_config_item_enabled,
            profile.id,
            "claude",
            "plugins",
            "two",
            False,
            home=tmp_path,
        )
        assert not second_write_started.wait(timeout=0.2)
        release_first_write.set()
        future_one.result()
        future_two.result()

    config = agent_profile_service.list_agent_profile_config(profile.id, "claude", home=tmp_path)
    plugins = section_items(config, "plugins")
    assert plugins["one"].enabled is False
    assert plugins["two"].enabled is False


def test_claude_config_lists_plugins_hooks_and_updates_settings_json(tmp_path: Path) -> None:
    claude_home = tmp_path / ".claude"
    write_skill(claude_home / "skills", "review")
    (claude_home / "plugins").mkdir(parents=True)
    (claude_home / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "superpowers@superpowers-marketplace": [
                        {"scope": "user", "version": "5.1.0"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (claude_home / "settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {"superpowers@superpowers-marketplace": True},
                "hooks": {
                    "beforeShellExecution": [
                        {"command": "hooks/preflight.sh", "matcher": "pytest"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    config = list_agent_config("claude_code", home=tmp_path)

    assert section_items(config, "skills")["review"].enabled is True
    assert section_items(config, "plugins")["superpowers@superpowers-marketplace"].enabled is True
    hooks = section_items(config, "hooks")
    assert hooks["beforeShellExecution:hooks/preflight.sh"].name == "beforeShellExecution"
    assert hooks["beforeShellExecution:hooks/preflight.sh"].enabled is True

    set_agent_config_item_enabled(
        "claude_code",
        "plugins",
        "superpowers@superpowers-marketplace",
        False,
        home=tmp_path,
    )
    settings = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
    assert settings["enabledPlugins"]["superpowers@superpowers-marketplace"] is False


def test_codex_config_disables_and_restores_nested_hooks(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "~/.codex/skills/self-improvement/scripts/activator.sh",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    hook_id = "UserPromptSubmit:~/.codex/skills/self-improvement/scripts/activator.sh"
    config = list_agent_config("codex", home=tmp_path)

    assert section_items(config, "hooks")[hook_id].enabled is True

    set_agent_config_item_enabled("codex", "hooks", hook_id, False, home=tmp_path)
    active = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    disabled = json.loads((codex_home / "hooks.disabled.json").read_text(encoding="utf-8"))
    assert "UserPromptSubmit" not in active["hooks"]
    assert disabled["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("activator.sh")
    assert section_items(list_agent_config("codex", home=tmp_path), "hooks")[hook_id].enabled is False

    set_agent_config_item_enabled("codex", "hooks", hook_id, True, home=tmp_path)
    restored = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    assert restored["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("activator.sh")
    assert "enabled" not in restored["hooks"]["UserPromptSubmit"][0]["hooks"][0]


def test_cursor_config_lists_user_skills_and_hooks_json(tmp_path: Path) -> None:
    cursor_home = tmp_path / ".cursor"
    write_skill(cursor_home / "skills-cursor", "canvas")
    write_skill(cursor_home / "skills-cursor.disabled", "loop")
    plugin_manifest = cursor_home / "plugins" / "superpowers" / ".cursor-plugin" / "plugin.json"
    plugin_manifest.parent.mkdir(parents=True)
    plugin_manifest.write_text(
        json.dumps(
            {
                "name": "superpowers",
                "displayName": "Superpowers",
            }
        ),
        encoding="utf-8",
    )
    disabled_plugin_manifest = (
        cursor_home
        / "plugins.disabled"
        / "offline"
        / ".cursor-plugin"
        / "plugin.json"
    )
    disabled_plugin_manifest.parent.mkdir(parents=True)
    disabled_plugin_manifest.write_text(
        json.dumps({"name": "offline", "displayName": "Offline Tools"}),
        encoding="utf-8",
    )
    (cursor_home / "hooks.disabled.json").write_text(
        json.dumps(
            {
                "afterFileEdit": [
                    {"command": "hooks/format.sh", "enabled": False}
                ]
            }
        ),
        encoding="utf-8",
    )

    config = list_agent_config("cursor_cli", home=tmp_path)

    assert config.agent == "cursor"
    assert section_items(config, "skills")["canvas"].enabled is True
    assert section_items(config, "skills")["loop"].enabled is False
    assert section_items(config, "plugins")["superpowers"].name == "Superpowers"
    assert section_items(config, "plugins")["superpowers"].enabled is True
    assert section_items(config, "plugins")["offline"].enabled is False
    hook = section_items(config, "hooks")["afterFileEdit:hooks/format.sh"]
    assert hook.name == "afterFileEdit"
    assert hook.enabled is False

    set_agent_config_item_enabled("cursor_cli", "plugins", "offline", True, home=tmp_path)
    assert (cursor_home / "plugins" / "offline" / ".cursor-plugin" / "plugin.json").is_file()
    assert not (cursor_home / "plugins.disabled" / "offline").exists()

    set_agent_config_item_enabled(
        "cursor_cli",
        "hooks",
        "afterFileEdit:hooks/format.sh",
        True,
        home=tmp_path,
    )
    hooks = json.loads((cursor_home / "hooks.json").read_text(encoding="utf-8"))
    assert hooks["hooks"]["afterFileEdit"][0]["command"] == "hooks/format.sh"
    assert "enabled" not in hooks["hooks"]["afterFileEdit"][0]


def test_cursor_plugin_config_uses_directory_id_when_manifest_name_differs(tmp_path: Path) -> None:
    cursor_home = tmp_path / ".cursor"
    plugin_manifest = cursor_home / "plugins.disabled" / "superpowers-dir" / ".cursor-plugin" / "plugin.json"
    plugin_manifest.parent.mkdir(parents=True)
    plugin_manifest.write_text(
        json.dumps({"name": "superpowers", "displayName": "Superpowers"}),
        encoding="utf-8",
    )

    config = list_agent_config("cursor_cli", home=tmp_path)

    plugin = section_items(config, "plugins")["superpowers-dir"]
    assert plugin.name == "Superpowers"
    assert plugin.enabled is False

    set_agent_config_item_enabled("cursor_cli", "plugins", "superpowers-dir", True, home=tmp_path)

    assert (cursor_home / "plugins" / "superpowers-dir" / ".cursor-plugin" / "plugin.json").is_file()
    assert not (cursor_home / "plugins.disabled" / "superpowers-dir").exists()


def test_antigravity_config_uses_gemini_antigravity_cli_root(tmp_path: Path) -> None:
    agy_home = tmp_path / ".gemini" / "antigravity-cli"
    write_skill(agy_home / "skills", "browser")
    plugin_manifest = agy_home / "plugins.disabled" / "review" / ".antigravity-plugin" / "plugin.json"
    plugin_manifest.parent.mkdir(parents=True)
    plugin_manifest.write_text(
        json.dumps({"name": "review", "displayName": "Review Tools"}),
        encoding="utf-8",
    )
    (agy_home / "hooks.json").write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [{"command": "hooks/audit.sh"}]}}),
        encoding="utf-8",
    )

    config = list_agent_config("agy", home=tmp_path)

    assert config.agent == "antigravity"
    assert section_items(config, "skills")["browser"].enabled is True
    assert section_items(config, "plugins")["review"].name == "Review Tools"
    assert section_items(config, "plugins")["review"].enabled is False
    assert section_items(config, "hooks")["UserPromptSubmit:hooks/audit.sh"].enabled is True

    set_agent_config_item_enabled("antigravity-cli", "plugins", "review", True, home=tmp_path)
    assert (agy_home / "plugins" / "review" / ".antigravity-plugin" / "plugin.json").is_file()
    assert not (agy_home / "plugins.disabled" / "review").exists()


def test_antigravity_window_config_materializes_nested_managed_home_alias(tmp_path: Path) -> None:
    agy_home = tmp_path / ".gemini" / "antigravity-cli"
    write_skill(agy_home / "skills", "browser")
    (agy_home / "settings.json").write_text("{}", encoding="utf-8")
    (agy_home / "antigravity-oauth-token").write_text("token", encoding="utf-8")

    config = agent_config_service.list_window_agent_config(
        "antigravity",
        window_id="window-1",
        home=tmp_path,
    )

    managed = tmp_path / ".web-terminal-acp" / "antigravity-cli-homes" / "window-1"
    command_home = tmp_path / ".web-terminal-acp" / "antigravity-cli-homes" / ".managed-home" / "window-1"
    assert config.agent == "antigravity"
    assert (managed / "settings.json").is_file()
    assert (managed / "skills" / "browser" / "SKILL.md").is_file()
    assert (managed / "antigravity-oauth-token").resolve() == agy_home / "antigravity-oauth-token"
    assert (command_home / ".gemini" / "antigravity-cli").resolve() == managed


def test_agent_profile_initializes_common_skills_and_agent_md_from_global_home(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    write_skill(codex_home / "skills", "docker")
    write_skill(codex_home / "skills.disabled", "sleepy")
    (codex_home / "AGENTS.md").write_text("Use project rules.\n", encoding="utf-8")

    profile = agent_profile_service.create_agent_profile(
        name="Builder",
        default_agent_client="codex",
        home=tmp_path,
    )

    profile_root = tmp_path / ".web-terminal-acp" / "agents" / profile.id
    assert profile.name == "Builder"
    assert (profile_root / "skills" / "docker" / "SKILL.md").is_file()
    assert (profile_root / "skills.disabled" / "sleepy" / "SKILL.md").is_file()
    assert (profile_root / "AGENT.md").read_text(encoding="utf-8") == "Use project rules.\n"


def test_agent_profile_materializes_common_config_to_agent_client_home(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    write_skill(codex_home / "skills", "docker")
    write_skill(codex_home / "skills.disabled", "sleepy")
    (codex_home / "config.toml").write_text(
        '[plugins."superpowers@openai-curated"]\nenabled = true\n',
        encoding="utf-8",
    )
    profile = agent_profile_service.create_agent_profile(
        name="Builder",
        default_agent_client="codex",
        home=tmp_path,
    )
    agent_profile_service.set_agent_profile_config_item_enabled(
        profile.id,
        "codex",
        "skills",
        "docker",
        False,
        home=tmp_path,
    )
    agent_profile_service.set_agent_profile_config_item_enabled(
        profile.id,
        "codex",
        "skills",
        "sleepy",
        True,
        home=tmp_path,
    )
    agent_profile_service.update_agent_profile(
        profile.id,
        agent_md="Common rules\n",
        home=tmp_path,
    )

    agent_profile_service.materialize_agent_profile_for_window(
        profile.id,
        "codex",
        window_id="window-1",
        home=tmp_path,
    )

    managed = tmp_path / ".web-terminal-acp" / "codex-homes" / "window-1"
    assert (managed / "skills.disabled" / "docker" / "SKILL.md").is_file()
    assert (managed / "skills" / "sleepy" / "SKILL.md").is_file()
    assert (managed / "AGENTS.md").read_text(encoding="utf-8") == "Common rules\n"
    assert (managed / "AGENT.md").read_text(encoding="utf-8") == "Common rules\n"
