from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.services import agent_config
from app.agent_plugins import get_agent_plugin_registry

PROFILE_VERSION = 1
PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
COMMON_SKILLS_DIR = "skills"
COMMON_DISABLED_SKILLS_DIR = "skills.disabled"
COMMON_AGENT_MD = "AGENT.md"
_UNSET = object()


@dataclass(frozen=True)
class AgentProfile:
    id: str
    name: str
    description: str | None
    default_agent_client: agent_config.AgentKind
    agent_md: str
    created_at: str
    updated_at: str


def list_agent_profiles(*, home: Path | None = None) -> list[AgentProfile]:
    root = _profiles_root(home or Path.home())
    if not root.exists():
        return []
    profiles = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        with _ignore_bad_profile(child):
            profiles.append(_profile_from_root(child))
    return sorted(profiles, key=lambda profile: (profile.name.lower(), profile.id))


def get_agent_profile(profile_id: str, *, home: Path | None = None) -> AgentProfile:
    return _profile_from_root(_profile_root(profile_id, home or Path.home()))


def create_agent_profile(
    *,
    name: str,
    description: str | None = None,
    default_agent_client: str = "codex",
    source_agent_client: str | None = None,
    home: Path | None = None,
) -> AgentProfile:
    user_home = home or Path.home()
    default_client = agent_config.normalize_agent_kind(default_agent_client)
    source_client = agent_config.normalize_agent_kind(source_agent_client or default_client)
    profile_id = uuid4().hex
    root = _profile_root(profile_id, user_home, must_exist=False)
    root.mkdir(parents=True)
    now = _now()
    _copy_initial_common_skills(source_client, root, user_home)
    _write_agent_md(root, _read_initial_agent_md(source_client, user_home))
    _write_manifest(
        root,
        {
            "version": PROFILE_VERSION,
            "id": profile_id,
            "name": _require_name(name),
            "description": _clean_description(description),
            "default_agent_client": default_client,
            "client_configs": _initial_client_configs(user_home),
            "created_at": now,
            "updated_at": now,
        },
    )
    return _profile_from_root(root)


def update_agent_profile(
    profile_id: str,
    *,
    name: str | None = None,
    description: str | None | object = _UNSET,
    default_agent_client: str | None = None,
    agent_md: str | None | object = _UNSET,
    home: Path | None = None,
) -> AgentProfile:
    root = _profile_root(profile_id, home or Path.home())
    with agent_config._locked_config_writes(root / "profile.json", root / COMMON_AGENT_MD):
        manifest = _read_manifest(root)
        if name is not None:
            manifest["name"] = _require_name(name)
        if description is not _UNSET:
            manifest["description"] = _clean_description(description if isinstance(description, str) else None)
        if default_agent_client is not None:
            manifest["default_agent_client"] = agent_config.normalize_agent_kind(default_agent_client)
        if agent_md is not _UNSET:
            _write_agent_md(root, agent_md if isinstance(agent_md, str) else "")
        manifest["updated_at"] = _now()
        _write_manifest(root, manifest)
    return _profile_from_root(root)


def delete_agent_profile(profile_id: str, *, home: Path | None = None) -> None:
    root = _profile_root(profile_id, home or Path.home())
    shutil.rmtree(root)


def list_agent_profile_config(
    profile_id: str,
    agent: str,
    *,
    home: Path | None = None,
) -> agent_config.AgentConfig:
    user_home = home or Path.home()
    root = _profile_root(profile_id, user_home)
    agent_kind = agent_config.normalize_agent_kind(agent)
    profile_config = _client_config_selection(root, agent_kind)
    overrides = _selection_overrides(profile_config)
    global_config = agent_config.list_agent_config(agent_kind, home=user_home)
    sections: list[agent_config.AgentConfigSection] = [
        agent_config.AgentConfigSection(
            "skills",
            "Skills",
            agent_config._list_directory_items(root, COMMON_SKILLS_DIR),
        )
    ]
    for section in global_config.sections:
        if section.id == "skills":
            continue
        section_overrides = overrides.get(section.id, {})
        sections.append(
            agent_config.AgentConfigSection(
                section.id,
                section.name,
                [
                    agent_config.AgentConfigItem(
                        item.id,
                        item.name,
                        section_overrides.get(item.id, item.enabled),
                        item.path,
                    )
                    for item in section.items
                ],
            )
        )
    return agent_config.AgentConfig(agent=agent_kind, sections=sections)


def set_agent_profile_config_item_enabled(
    profile_id: str,
    agent: str,
    section_id: str,
    item_id: str,
    enabled: bool,
    *,
    home: Path | None = None,
) -> agent_config.AgentConfig:
    user_home = home or Path.home()
    root = _profile_root(profile_id, user_home)
    agent_kind = agent_config.normalize_agent_kind(agent)
    if section_id == "skills":
        agent_config._set_directory_item_enabled(root, COMMON_SKILLS_DIR, item_id, enabled)
        _touch_profile(root)
    elif section_id in {"plugins", "hooks"}:
        _set_client_config_override(root, agent_kind, section_id, item_id, enabled)
    else:
        raise ValueError(f"unsupported config section: {section_id}")
    return list_agent_profile_config(profile_id, agent_kind, home=user_home)


def build_agent_profile_selection(
    profile_id: str,
    agent: str,
    *,
    home: Path | None = None,
) -> agent_config.AgentConfigSelection:
    config = list_agent_profile_config(profile_id, agent, home=home)
    return agent_config.AgentConfigSelection(
        agent=config.agent,
        sections=[
            agent_config.AgentConfigSectionSelection(
                section.id,
                [
                    agent_config.AgentConfigItemSelection(item.id, item.enabled)
                    for item in section.items
                ],
            )
            for section in config.sections
        ],
    )


def materialize_agent_profile_for_window(
    profile_id: str,
    agent: str,
    *,
    window_id: str,
    home: Path | None = None,
) -> agent_config.AgentConfig:
    user_home = home or Path.home()
    root = _profile_root(profile_id, user_home)
    agent_kind = agent_config.normalize_agent_kind(agent)
    selection = build_agent_profile_selection(profile_id, agent_kind, home=user_home)
    config = agent_config.apply_agent_config_selection(
        selection,
        window_id=window_id,
        home=user_home,
    )
    managed_root = agent_config._managed_agent_root(agent_kind, window_id, user_home)
    _copy_profile_common_config(root, managed_root, agent_kind)
    return agent_config.list_agent_config(
        agent_kind,
        home=agent_config._managed_home_root(managed_root),
    )


def _profiles_root(home: Path) -> Path:
    return home / ".web-terminal-acp" / "agents"


def _agent_clients() -> tuple[agent_config.AgentKind, ...]:
    return tuple(
        plugin.agent_client_id for plugin in get_agent_plugin_registry().all()
    )


def _profile_root(profile_id: str, home: Path, *, must_exist: bool = True) -> Path:
    _validate_profile_id(profile_id)
    root = _profiles_root(home) / profile_id
    if must_exist and not root.is_dir():
        raise ValueError(f"agent profile not found: {profile_id}")
    return root


def _validate_profile_id(profile_id: str) -> None:
    if not profile_id or not PROFILE_ID_PATTERN.fullmatch(profile_id) or profile_id in {".", ".."}:
        raise ValueError(f"invalid agent profile id: {profile_id}")


def _profile_from_root(root: Path) -> AgentProfile:
    manifest = _read_manifest(root)
    profile_id = _string(manifest.get("id")) or root.name
    default_agent_client = agent_config.normalize_agent_kind(
        _string(manifest.get("default_agent_client")) or "codex"
    )
    return AgentProfile(
        id=profile_id,
        name=_string(manifest.get("name")) or profile_id,
        description=_string(manifest.get("description")),
        default_agent_client=default_agent_client,
        agent_md=_read_agent_md(root),
        created_at=_string(manifest.get("created_at")) or _now(),
        updated_at=_string(manifest.get("updated_at")) or _now(),
    )


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / "profile.json"
    if not path.is_file():
        raise ValueError(f"agent profile manifest not found: {root.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid agent profile manifest: {root.name}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid agent profile manifest: {root.name}")
    return data


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    agent_config._write_text_file_atomic(
        root / "profile.json",
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _touch_profile(root: Path) -> None:
    with agent_config._locked_config_writes(root / "profile.json"):
        manifest = _read_manifest(root)
        manifest["updated_at"] = _now()
        _write_manifest(root, manifest)


def _read_agent_md(root: Path) -> str:
    path = root / COMMON_AGENT_MD
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_agent_md(root: Path, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    agent_config._write_text_file_atomic(root / COMMON_AGENT_MD, content)


def _copy_initial_common_skills(
    source_agent: agent_config.AgentKind,
    root: Path,
    home: Path,
) -> None:
    source_root = agent_config._agent_root(source_agent, home)
    source_skills = source_root / agent_config._skills_directory(source_agent)
    disabled_skills = source_root / f"{agent_config._skills_directory(source_agent)}.disabled"
    if source_skills.exists():
        shutil.copytree(source_skills, root / COMMON_SKILLS_DIR, symlinks=True, dirs_exist_ok=True)
    else:
        (root / COMMON_SKILLS_DIR).mkdir(parents=True, exist_ok=True)
    if disabled_skills.exists():
        shutil.copytree(
            disabled_skills,
            root / COMMON_DISABLED_SKILLS_DIR,
            symlinks=True,
            dirs_exist_ok=True,
        )
    else:
        (root / COMMON_DISABLED_SKILLS_DIR).mkdir(parents=True, exist_ok=True)


def _read_initial_agent_md(source_agent: agent_config.AgentKind, home: Path) -> str:
    source_root = agent_config._agent_root(source_agent, home)
    candidates = (
        get_agent_plugin_registry()
        .by_agent_id(source_agent)
        .native_config.initial_agent_md_candidates
    )
    for candidate in candidates:
        path = source_root / candidate
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""
    return ""


def _initial_client_configs(home: Path) -> dict[str, Any]:
    configs: dict[str, Any] = {}
    for agent in _agent_clients():
        try:
            config = agent_config.list_agent_config(agent, home=home)
        except ValueError:
            continue
        configs[agent] = {
            "sections": [
                {
                    "id": section.id,
                    "items": [
                        {"id": item.id, "enabled": item.enabled}
                        for item in section.items
                        if section.id != "skills"
                    ],
                }
                for section in config.sections
                if section.id != "skills"
            ]
        }
    return configs


def _client_config_selection(root: Path, agent: agent_config.AgentKind) -> dict[str, Any]:
    manifest = _read_manifest(root)
    configs = manifest.setdefault("client_configs", {})
    if not isinstance(configs, dict):
        configs = {}
        manifest["client_configs"] = configs
    config = configs.setdefault(agent, {"sections": []})
    if not isinstance(config, dict):
        config = {"sections": []}
        configs[agent] = config
    return config


def _selection_overrides(client_config: dict[str, Any]) -> dict[str, dict[str, bool]]:
    overrides: dict[str, dict[str, bool]] = {}
    sections = client_config.get("sections")
    if not isinstance(sections, list):
        return overrides
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = section.get("id")
        if section_id not in {"plugins", "hooks"}:
            continue
        section_overrides = overrides.setdefault(section_id, {})
        items = section.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            enabled = item.get("enabled")
            if isinstance(item_id, str) and item_id and isinstance(enabled, bool):
                section_overrides[item_id] = enabled
    return overrides


def _set_client_config_override(
    root: Path,
    agent: agent_config.AgentKind,
    section_id: str,
    item_id: str,
    enabled: bool,
) -> None:
    if section_id == "plugins":
        strategy = get_agent_plugin_registry().by_agent_id(agent).native_config.plugin_strategy
        if strategy == "codex_toml":
            agent_config._validate_codex_plugin_id(item_id)
        elif strategy == "claude_settings":
            agent_config._validate_json_key_item_id(item_id)
        else:
            agent_config._validate_path_item_id(item_id)
    elif section_id == "hooks":
        agent_config._validate_json_key_item_id(item_id)
    with agent_config._locked_config_writes(root / "profile.json"):
        manifest = _read_manifest(root)
        configs = manifest.setdefault("client_configs", {})
        if not isinstance(configs, dict):
            configs = {}
            manifest["client_configs"] = configs
        client_config = configs.setdefault(agent, {"sections": []})
        if not isinstance(client_config, dict):
            client_config = {"sections": []}
            configs[agent] = client_config
        sections = client_config.setdefault("sections", [])
        if not isinstance(sections, list):
            sections = []
            client_config["sections"] = sections
        section = next(
            (candidate for candidate in sections if isinstance(candidate, dict) and candidate.get("id") == section_id),
            None,
        )
        if section is None:
            section = {"id": section_id, "items": []}
            sections.append(section)
        items = section.setdefault("items", [])
        if not isinstance(items, list):
            items = []
            section["items"] = items
        item = next(
            (candidate for candidate in items if isinstance(candidate, dict) and candidate.get("id") == item_id),
            None,
        )
        if item is None:
            items.append({"id": item_id, "enabled": enabled})
        else:
            item["enabled"] = enabled
        manifest["updated_at"] = _now()
        _write_manifest(root, manifest)


def _copy_profile_common_config(
    root: Path,
    managed_root: Path,
    agent: agent_config.AgentKind,
) -> None:
    skills_dir = agent_config._skills_directory(agent)
    _replace_tree(root / COMMON_SKILLS_DIR, managed_root / skills_dir)
    _replace_tree(root / COMMON_DISABLED_SKILLS_DIR, managed_root / f"{skills_dir}.disabled")
    agent_md = root / COMMON_AGENT_MD
    if not agent_md.is_file():
        return
    targets = (
        get_agent_plugin_registry()
        .by_agent_id(agent)
        .native_config.profile_agent_md_targets
    )
    for target_name in targets:
        target = managed_root / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copy2(agent_md, target, follow_symlinks=True)


def _replace_tree(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    if source.exists():
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
    else:
        target.mkdir(parents=True, exist_ok=True)


def _require_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("agent profile name is required")
    if len(cleaned) > 120:
        raise ValueError("agent profile name is too long")
    return cleaned


def _clean_description(description: str | None) -> str | None:
    if description is None:
        return None
    cleaned = description.strip()
    if not cleaned:
        return None
    if len(cleaned) > 500:
        raise ValueError("agent profile description is too long")
    return cleaned


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ignore_bad_profile:
    def __init__(self, _root: Path) -> None:
        self._suppressed = (ValueError, OSError)

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, _exc, _traceback) -> bool:
        return exc_type is not None and issubclass(exc_type, self._suppressed)
