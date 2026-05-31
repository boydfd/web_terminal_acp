from __future__ import annotations

import contextlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

AgentKind = Literal["codex", "claude", "cursor"]
SectionKind = Literal["skills", "plugins", "hooks"]
DISABLED_HOOKS_FILE = "hooks.disabled.json"


@dataclass(frozen=True)
class AgentConfigItem:
    id: str
    name: str
    enabled: bool
    path: str | None = None


@dataclass(frozen=True)
class AgentConfigSection:
    id: SectionKind
    name: str
    items: list[AgentConfigItem]


@dataclass(frozen=True)
class AgentConfig:
    agent: AgentKind
    sections: list[AgentConfigSection]


@dataclass(frozen=True)
class AgentConfigItemSelection:
    id: str
    enabled: bool


@dataclass(frozen=True)
class AgentConfigSectionSelection:
    id: SectionKind
    items: list[AgentConfigItemSelection]


@dataclass(frozen=True)
class AgentConfigSelection:
    agent: AgentKind
    sections: list[AgentConfigSectionSelection]


def list_agent_config(agent: str, *, home: Path | None = None) -> AgentConfig:
    agent_kind = _agent_kind(agent)
    root = _agent_root(agent_kind, home or Path.home())
    return AgentConfig(
        agent=agent_kind,
        sections=[
            AgentConfigSection("skills", "Skills", _list_directory_items(root, _skills_directory(agent_kind))),
            AgentConfigSection("plugins", "Plugins", _list_plugins(agent_kind, root)),
            AgentConfigSection("hooks", "Hooks", _list_hooks(agent_kind, root)),
        ],
    )


def set_agent_config_item_enabled(
    agent: str,
    section_id: str,
    item_id: str,
    enabled: bool,
    *,
    home: Path | None = None,
) -> AgentConfig:
    agent_kind = _agent_kind(agent)
    root = _agent_root(agent_kind, home or Path.home())
    if section_id == "skills":
        _set_directory_item_enabled(root, _skills_directory(agent_kind), item_id, enabled)
    elif section_id == "plugins":
        _set_plugin_enabled(agent_kind, root, item_id, enabled)
    elif section_id == "hooks":
        _set_hook_enabled(agent_kind, root, item_id, enabled)
    else:
        raise ValueError(f"unsupported config section: {section_id}")
    return list_agent_config(agent_kind, home=home)


def normalize_agent_kind(agent: str) -> AgentKind:
    return _agent_kind(agent)


def apply_agent_config_selection(
    selection: AgentConfigSelection,
    *,
    window_id: str,
    home: Path | None = None,
) -> AgentConfig:
    agent_kind = _agent_kind(selection.agent)
    if agent_kind != selection.agent:
        raise ValueError(f"selection agent mismatch: {selection.agent}")

    user_home = home or Path.home()
    source_root = _agent_root(agent_kind, user_home)
    managed_root = _managed_agent_root(agent_kind, window_id, user_home)
    _materialize_agent_config_root(agent_kind, source_root, managed_root)
    for section in selection.sections:
        for item in section.items:
            try:
                set_agent_config_item_enabled(
                    agent_kind,
                    section.id,
                    item.id,
                    item.enabled,
                    home=_managed_home_root(managed_root),
                )
            except ValueError:
                continue
    return list_agent_config(agent_kind, home=_managed_home_root(managed_root))


def _agent_kind(agent: str) -> AgentKind:
    normalized = agent.strip().lower()
    if normalized == "codex":
        return "codex"
    if normalized in {"claude", "claude_code"}:
        return "claude"
    if normalized in {"cursor", "cursor_cli", "agent"}:
        return "cursor"
    raise ValueError(f"unsupported agent: {agent}")


def _agent_root(agent: AgentKind, home: Path) -> Path:
    roots: dict[AgentKind, str] = {
        "codex": ".codex",
        "claude": ".claude",
        "cursor": ".cursor",
    }
    return home / roots[agent]


def _managed_agent_root(agent: AgentKind, window_id: str, home: Path) -> Path:
    roots: dict[AgentKind, str] = {
        "codex": ".web-terminal-acp/codex-homes",
        "claude": ".web-terminal-acp/claude-code-homes",
        "cursor": ".web-terminal-acp/cursor-homes",
    }
    return home / roots[agent] / window_id


def _managed_home_root(managed_root: Path) -> Path:
    return managed_root.parent / ".managed-home" / managed_root.name


def _materialize_agent_config_root(agent: AgentKind, source_root: Path, managed_root: Path) -> None:
    managed_root.mkdir(parents=True, exist_ok=True)
    item_names = _agent_config_item_names(agent)
    for item_name in item_names:
        source = source_root / item_name
        target = managed_root / item_name
        if not source.exists() or target.exists():
            continue
        if source.is_dir():
            _copy_config_directory(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=True)
    _link_agent_history_items(agent, source_root, managed_root)

    home_root = _managed_home_root(managed_root)
    alias = home_root / _agent_root(agent, Path()).name
    alias.parent.mkdir(parents=True, exist_ok=True)
    if not alias.exists() and not alias.is_symlink():
        with contextlib.suppress(OSError):
            alias.symlink_to(managed_root)
    if not alias.exists() and not alias.is_symlink():
        _copy_config_directory(managed_root, alias)


def _copy_config_directory(source: Path, target: Path) -> None:
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)


def _link_agent_history_items(agent: AgentKind, source_root: Path, managed_root: Path) -> None:
    for item_name in _agent_history_item_names(agent):
        _link_existing_item(source_root / item_name, managed_root / item_name)


def _link_existing_item(source: Path, target: Path) -> None:
    if not source.exists() or target.exists() or target.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        target.symlink_to(source)


def _agent_config_item_names(agent: AgentKind) -> tuple[str, ...]:
    common = ("hooks", "hooks.json", DISABLED_HOOKS_FILE, "plugins", "plugins.disabled")
    if agent == "codex":
        return (
            "config.toml",
            "AGENTS.md",
            "skills",
            "skills.disabled",
            "plugin_marketplaces.json",
            *common,
        )
    if agent == "claude":
        return (
            "settings.json",
            "settings.local.json",
            "commands",
            "skills",
            "skills.disabled",
            "api-key-helper.sh",
            *common,
        )
    return (
        "agent-cli-state.json",
        "cli-config.json",
        "skills-cursor",
        "skills-cursor.disabled",
        *common,
    )


def _agent_history_item_names(agent: AgentKind) -> tuple[str, ...]:
    if agent == "codex":
        return ("history.json", "history.jsonl")
    if agent == "claude":
        return ("history.json", "history.jsonl", "file-history")
    return ("history.json", "history.jsonl", "chats")


def _skills_directory(agent: AgentKind) -> str:
    return "skills-cursor" if agent == "cursor" else "skills"


def _list_directory_items(root: Path, section: str) -> list[AgentConfigItem]:
    items: dict[str, AgentConfigItem] = {}
    for enabled, base in ((False, root / f"{section}.disabled"), (True, root / section)):
        if not base.exists():
            continue
        for path in sorted(child for child in base.iterdir() if child.is_dir()):
            marker = path / "SKILL.md"
            if section in {"skills", "skills-cursor"} and not marker.exists():
                continue
            items[path.name] = AgentConfigItem(
                id=path.name,
                name=_name_from_skill(marker) or path.name,
                enabled=enabled,
                path=str(path),
            )
    return sorted(items.values(), key=lambda item: item.name.lower())


def _set_directory_item_enabled(root: Path, section: str, item_id: str, enabled: bool) -> None:
    _validate_path_item_id(item_id)
    active = root / section / item_id
    disabled = root / f"{section}.disabled" / item_id
    source = disabled if enabled else active
    target = active if enabled else disabled
    if not source.exists():
        if target.exists():
            return
        raise ValueError(f"config item not found: {item_id}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError(f"config item already exists at target: {item_id}")
    shutil.move(str(source), str(target))


def _name_from_skill(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
            match = re.match(r"\s*name:\s*[\"']?([^\"']+)[\"']?\s*$", line)
            if match:
                return match.group(1).strip()
    except OSError:
        return None
    return None


def _list_plugins(agent: AgentKind, root: Path) -> list[AgentConfigItem]:
    if agent == "codex":
        return _list_codex_plugins(root)
    if agent == "claude":
        return _list_claude_plugins(root)
    return _list_cursor_plugins(root)


def _list_codex_plugins(root: Path) -> list[AgentConfigItem]:
    enabled = _read_codex_plugin_enabled(root / "config.toml")
    items: list[AgentConfigItem] = []
    for manifest in sorted((root / "plugins" / "cache").glob("*/*/*/.codex-plugin/plugin.json")):
        marketplace = manifest.parents[3].name
        plugin_name = manifest.parents[2].name
        plugin_id = f"{plugin_name}@{marketplace}"
        data = _read_json_file(manifest)
        label = _deep_string(data, "interface", "displayName") or _string(data.get("name")) or plugin_name
        items.append(AgentConfigItem(plugin_id, label, enabled.get(plugin_id, True), str(manifest.parent.parent)))
    return sorted(items, key=lambda item: item.name.lower())


def _list_claude_plugins(root: Path) -> list[AgentConfigItem]:
    installed = _read_json_file(root / "plugins" / "installed_plugins.json").get("plugins", {})
    enabled = _read_json_file(root / "settings.json").get("enabledPlugins", {})
    if not isinstance(installed, dict):
        return []
    items = []
    for plugin_id in sorted(key for key in installed if isinstance(key, str)):
        items.append(AgentConfigItem(plugin_id, plugin_id, bool(enabled.get(plugin_id, True))))
    return items


def _list_cursor_plugins(root: Path) -> list[AgentConfigItem]:
    items: dict[str, AgentConfigItem] = {}
    for enabled, base in ((False, root / "plugins.disabled"), (True, root / "plugins")):
        if not base.exists():
            continue
        for path in sorted(child for child in base.iterdir() if child.is_dir()):
            manifest = path / ".cursor-plugin" / "plugin.json"
            if not manifest.is_file():
                continue
            data = _read_json_file(manifest)
            plugin_id = _string(data.get("name")) or path.name
            label = _string(data.get("displayName")) or _string(data.get("name")) or path.name
            items[plugin_id] = AgentConfigItem(plugin_id, label, enabled, str(path))
    return sorted(items.values(), key=lambda item: item.name.lower())


def _set_plugin_enabled(agent: AgentKind, root: Path, item_id: str, enabled: bool) -> None:
    if agent == "codex":
        _write_codex_plugin_enabled(root / "config.toml", item_id, enabled)
        return
    if agent == "claude":
        _validate_json_key_item_id(item_id)
        settings_path = root / "settings.json"
        settings = _read_json_file(settings_path)
        enabled_plugins = settings.setdefault("enabledPlugins", {})
        if not isinstance(enabled_plugins, dict):
            enabled_plugins = {}
            settings["enabledPlugins"] = enabled_plugins
        enabled_plugins[item_id] = enabled
        _write_json_file(settings_path, settings)
        return
    _set_directory_item_enabled(root, "plugins", item_id, enabled)


def _list_hooks(agent: AgentKind, root: Path) -> list[AgentConfigItem]:
    settings_path = _hooks_config_path(agent, root)
    settings = _read_json_file(settings_path)
    active_hooks = _hooks_root(settings)
    disabled_hooks = _read_json_file(root / DISABLED_HOOKS_FILE)
    items = {item.id: item for item in _hook_items(disabled_hooks, enabled=False)}
    items.update({item.id: item for item in _hook_items(active_hooks, enabled=True)})
    return sorted(items.values(), key=lambda item: (item.name.lower(), item.id.lower(), item.enabled))


def _hook_items(hooks: dict[str, Any], *, enabled: bool) -> list[AgentConfigItem]:
    items: list[AgentConfigItem] = []
    for event_name, definitions in hooks.items():
        if not isinstance(event_name, str) or not isinstance(definitions, list):
            continue
        for entry_index, entry in enumerate(definitions):
            if not isinstance(entry, dict):
                continue
            nested_hooks = entry.get("hooks")
            if isinstance(nested_hooks, list):
                for hook_index, hook in enumerate(nested_hooks):
                    if not isinstance(hook, dict):
                        continue
                    command = _hook_command(hook, fallback=f"{entry_index}.{hook_index}")
                    item_id = f"{event_name}:{command}"
                    item_enabled = enabled and entry.get("enabled") is not False and hook.get("enabled") is not False
                    items.append(AgentConfigItem(item_id, event_name, item_enabled, command))
                continue
            command = _hook_command(entry, fallback=str(entry_index))
            item_id = f"{event_name}:{command}"
            item_enabled = enabled and entry.get("enabled") is not False
            items.append(AgentConfigItem(item_id, event_name, item_enabled, command))
    return items


def _set_hook_enabled(agent: AgentKind, root: Path, item_id: str, enabled: bool) -> None:
    settings_path = _hooks_config_path(agent, root)
    disabled_path = root / DISABLED_HOOKS_FILE
    settings = _read_json_file(settings_path)
    hooks = _ensure_hooks_root(settings)
    disabled_hooks = _read_json_file(disabled_path)
    event_name, separator, command = item_id.partition(":")
    if not separator:
        raise ValueError(f"invalid hook id: {item_id}")

    if enabled:
        removed = _remove_hook_entry(disabled_hooks, event_name, command)
        if removed is not None:
            _set_hook_entry_enabled(removed, True)
            _append_hook_entry(hooks, event_name, removed)
            _write_json_file(settings_path, settings)
            _write_json_file(disabled_path, disabled_hooks)
            return
        active = _find_hook_entry(hooks, event_name, command)
        if active is not None:
            _set_hook_entry_enabled(active, True)
            _write_json_file(settings_path, settings)
            return
        raise ValueError(f"hook not found: {item_id}")

    removed = _remove_hook_entry(hooks, event_name, command)
    if removed is not None:
        _set_hook_entry_enabled(removed, False)
        _append_hook_entry(disabled_hooks, event_name, removed)
        _write_json_file(settings_path, settings)
        _write_json_file(disabled_path, disabled_hooks)
        return
    if _find_hook_entry(disabled_hooks, event_name, command) is not None:
        return
    raise ValueError(f"hook not found: {item_id}")


def _hooks_config_path(agent: AgentKind, root: Path) -> Path:
    if agent == "claude":
        return root / "settings.json"
    return root / "hooks.json"


def _hooks_root(settings: dict[str, Any]) -> dict[str, Any]:
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        return hooks
    return {}


def _ensure_hooks_root(settings: dict[str, Any]) -> dict[str, Any]:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    return hooks


def _hook_command(definition: dict[str, Any], *, fallback: str) -> str:
    return _string(definition.get("command")) or _string(definition.get("prompt")) or fallback


def _find_hook_entry(hooks: dict[str, Any], event_name: str, command: str) -> dict[str, Any] | None:
    definitions = hooks.get(event_name)
    if not isinstance(definitions, list):
        return None
    for entry in definitions:
        if not isinstance(entry, dict):
            continue
        nested_hooks = entry.get("hooks")
        if isinstance(nested_hooks, list):
            for hook in nested_hooks:
                if isinstance(hook, dict) and _hook_command(hook, fallback="") == command:
                    return hook
            continue
        if _hook_command(entry, fallback="") == command:
            return entry
    return None


def _remove_hook_entry(hooks: dict[str, Any], event_name: str, command: str) -> dict[str, Any] | None:
    definitions = hooks.get(event_name)
    if not isinstance(definitions, list):
        return None
    for entry_index, entry in enumerate(definitions):
        if not isinstance(entry, dict):
            continue
        nested_hooks = entry.get("hooks")
        if isinstance(nested_hooks, list):
            for hook_index, hook in enumerate(nested_hooks):
                if not isinstance(hook, dict) or _hook_command(hook, fallback="") != command:
                    continue
                removed_hook = nested_hooks.pop(hook_index)
                removed_entry = {key: value for key, value in entry.items() if key != "hooks"}
                removed_entry["hooks"] = [removed_hook]
                if not nested_hooks:
                    definitions.pop(entry_index)
                _cleanup_hook_event(hooks, event_name)
                return removed_entry
            continue
        if _hook_command(entry, fallback="") == command:
            removed_entry = definitions.pop(entry_index)
            _cleanup_hook_event(hooks, event_name)
            return removed_entry
    return None


def _append_hook_entry(hooks: dict[str, Any], event_name: str, entry: dict[str, Any]) -> None:
    definitions = hooks.setdefault(event_name, [])
    if not isinstance(definitions, list):
        definitions = []
        hooks[event_name] = definitions
    definitions.append(entry)


def _cleanup_hook_event(hooks: dict[str, Any], event_name: str) -> None:
    definitions = hooks.get(event_name)
    if isinstance(definitions, list) and not definitions:
        hooks.pop(event_name, None)


def _set_hook_entry_enabled(entry: dict[str, Any], enabled: bool) -> None:
    nested_hooks = entry.get("hooks")
    if isinstance(nested_hooks, list):
        for hook in nested_hooks:
            if isinstance(hook, dict):
                _set_hook_entry_enabled(hook, enabled)
        if enabled:
            entry.pop("enabled", None)
        else:
            entry["enabled"] = False
        return
    if enabled:
        entry.pop("enabled", None)
    else:
        entry["enabled"] = False


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _deep_string(data: object, *keys: str) -> str | None:
    value = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _string(value)


def _read_codex_plugin_enabled(path: Path) -> dict[str, bool]:
    if not path.is_file():
        return {}
    values: dict[str, bool] = {}
    current_plugin: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        header = re.match(r'\s*\[plugins\."([^"]+)"\]\s*$', line)
        if header:
            current_plugin = header.group(1)
            continue
        if current_plugin is None:
            continue
        enabled = re.match(r"\s*enabled\s*=\s*(true|false)\s*(?:#.*)?$", line, re.IGNORECASE)
        if enabled:
            values[current_plugin] = enabled.group(1).lower() == "true"
    return values


def _write_codex_plugin_enabled(path: Path, item_id: str, enabled: bool) -> None:
    _validate_codex_plugin_id(item_id)
    value = "true" if enabled else "false"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f'[plugins."{item_id}"]\nenabled = {value}\n', encoding="utf-8")
        return

    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    in_target = False
    target_seen = False
    enabled_written = False
    for line in lines:
        header = re.match(r'\s*\[plugins\."([^"]+)"\]\s*$', line)
        if header:
            if in_target and not enabled_written:
                output.append(f"enabled = {value}")
                enabled_written = True
            in_target = header.group(1) == item_id
            target_seen = target_seen or in_target
            output.append(line)
            continue
        if in_target and re.match(r"\s*enabled\s*=", line):
            output.append(f"enabled = {value}")
            enabled_written = True
            continue
        output.append(line)
    if in_target and not enabled_written:
        output.append(f"enabled = {value}")
    if not target_seen:
        if output and output[-1].strip():
            output.append("")
        output.extend([f'[plugins."{item_id}"]', f"enabled = {value}"])
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _validate_path_item_id(item_id: str) -> None:
    if (
        not item_id
        or item_id in {".", ".."}
        or "/" in item_id
        or "\\" in item_id
        or "\x00" in item_id
    ):
        raise ValueError(f"invalid config item id: {item_id}")


def _validate_codex_plugin_id(item_id: str) -> None:
    if (
        not item_id
        or "\\" in item_id
        or '"' in item_id
        or any(ord(character) < 32 for character in item_id)
    ):
        raise ValueError(f"invalid config item id: {item_id}")


def _validate_json_key_item_id(item_id: str) -> None:
    if not item_id or any(ord(character) < 32 for character in item_id):
        raise ValueError(f"invalid config item id: {item_id}")
