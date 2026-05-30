import json
from pathlib import Path

import pytest

from app.services.agent_config import (
    AgentConfigItemSelection,
    AgentConfigSectionSelection,
    AgentConfigSelection,
    apply_agent_config_selection,
    list_agent_config,
    set_agent_config_item_enabled,
)


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


def test_apply_agent_config_selection_materializes_per_window_home(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    write_skill(codex_home / "skills", "docker")
    write_skill(codex_home / "skills.disabled", "sleepy")
    (codex_home / "config.toml").write_text(
        '[plugins."superpowers@openai-curated"]\nenabled = true\n',
        encoding="utf-8",
    )

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
    assert (codex_home / "skills" / "docker" / "SKILL.md").is_file()
    assert (codex_home / "skills.disabled" / "sleepy" / "SKILL.md").is_file()


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
