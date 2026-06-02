# Agent-Client Plugin Contract

Web Terminal ACP models executable tools such as Codex, Claude Code, and Cursor CLI as **agent-client plugins**. Plugin metadata is intentionally client-safe: the remote client bundle can import `app.agent_plugins` before it connects, without importing server ORM models or `AgentToolAdapter` implementations. Server-only event normalization still lives under `app.agent_tools`.

## Registration

Built-in plugins are registered in `backend/app/agent_plugins/builtins.py` by returning an `AgentPlugin` from `builtin_agent_plugins()`.

Each plugin must provide:

- `agent_client_id`: stable UI/profile id, for example `codex`.
- `provider_id`: stable event/session provider id, for example `claude_code`.
- `label`: user-facing name.
- `aliases`: accepted legacy ids or command-style ids.
- `command`: default launch command, executable command names, optional permission bypass flag, and non-task subcommands.
- `storage`: native config root, per-window managed root, skill directory name, copied config/history item names, runtime env vars, shell alias env vars, and optional shell prepare function name.
- `native_config`: hooks config filename, AGENT.md materialization targets, initial AGENT.md candidates, and plugin/hook config strategy.
- `capabilities`: booleans for launch/config support and optional record/runtime/presence integrations. New plugins default to launch and config support, but not agent records, runtime tags, or work-presence reporting.
- `watch_collector_name`: optional client-agent watcher collector function name for provider-specific event collection.
- `tool_adapter_module` and `tool_adapter_class`: optional server-only adapter coordinates under `app.agent_tools.adapters`. Declare these when the plugin supports server-side agent record normalization or projections.

The registry rejects duplicate provider ids, agent ids, aliases, and command names at startup. Do not rely on registration order to resolve conflicts.

## Adapter Responsibilities

An `AgentToolAdapter` remains the source of truth for server-side event behavior:

- claim provider and event source types;
- normalize raw records into generic event rows;
- expose chat/detail/summary/search projections;
- describe storage needed by watcher code;
- keep legacy source types readable when applicable.

New managed records should use the generic `agent_tool_record` source type unless there is a strong compatibility reason not to.

## Config Strategies

`AgentNativeConfigSpec.plugin_strategy` controls how Web Terminal lists and toggles native plugins:

- `codex_toml`: plugin keys in Codex `config.toml`.
- `claude_settings`: plugin keys in Claude `settings.json`.
- `directory`: enabled and disabled plugin directories.

`hook_strategy` is currently `json` or `claude_settings`. Add a new strategy only when the native client cannot fit one of the existing models.

## Runtime Contract

Client-agent runtime behavior is metadata-driven where plugin metadata can safely describe it:

- command detection, permission flags, native config roots, and managed home paths come from `app.agent_plugins`;
- server-side agent record adapter loading comes from `tool_adapter_module` and `tool_adapter_class`;
- watcher collector dispatch comes from `watch_collector_name`;
- shell home preparation calls registered `storage.shell_prepare_function` values, while the function bodies remain provider-specific for native layout quirks.

Remote clients answer `agent_clients_list` over the control websocket with the descriptors supported by that client version. The server should not assume its own plugin registry applies to every remote client.

Capability flags are part of this descriptor boundary. A remote-only agent-client can be launchable and configurable without claiming local-only integrations such as agent record ingestion, runtime tags, or work-presence detection. The backend enforces launch/config capabilities on create-window, client config, window config, and profile config endpoints. Older remote clients that omit `capabilities` are treated as launch/config capable for compatibility and as not supporting record/runtime/presence integrations.

## Frontend Contract

The frontend discovers agent-clients with:

```text
GET /api/clients/{client_id}/agent-clients
```

The response drives launch tabs, Settings -> Agents command inputs, and agent profile selectors. For local clients the backend returns local descriptors. For remote clients the backend queries the connected client-agent and returns that client version's descriptors. Keep `frontend/src/agentLaunch.ts` defaults in sync as an offline/fallback list for older or unavailable backends.

## Remote Client Bundle

If plugin code is needed before the remote client connects, include it in `client_app_file_contents()` in `backend/app/services/bootstrap/installer.py`. Keep the isolated bundle import test updated so `app.client_agent.runner` imports from the packaged bundle alone under isolated Python import mode. Files packaged for the remote client must not import `app.models`, `app.agent_tools`, SQLAlchemy, Elastic, or other server-only dependencies.

## Checklist For A New Built-In Plugin

1. Implement or extend an `AgentToolAdapter` when the server must normalize or project events.
2. Register an `AgentPlugin` in `backend/app/agent_plugins/builtins.py`, including adapter coordinates when records are supported.
3. Add any new config strategy support in `agent_config.py` and `agent_profiles.py`.
4. Add a client-agent watcher collector and `watch_collector_name` when runtime event collection is needed.
5. Include bundle files in `client_app_file_contents()` if remote clients need them.
6. Add tests for registry descriptors and conflict validation, command detection, config/profile behavior, bundle imports, remote descriptor discovery, and watcher dispatch.
7. Run backend focused tests plus frontend API/settings/launch tests.
