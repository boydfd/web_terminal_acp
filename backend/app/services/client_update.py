from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.client_agent.updater import package_checksum
from app.services.bootstrap.installer import AGENT_REQUIREMENTS, client_app_file_contents
from app.services.runtime.client_connections import (
    ClientConnectionClosed,
    ClientConnectionRegistry,
)
from app.services.runtime.protocol import AgentMessage, TerminalPayload


class ClientUpdateUnavailable(RuntimeError):
    """Raised when the client cannot be asked to start an update."""


@dataclass(frozen=True)
class ClientUpdateStartResult:
    client_id: UUID
    job_id: str
    method: str
    status: str = "STARTED"


def build_client_update_package(job_id: str | None = None) -> dict[str, object]:
    files = client_app_file_contents()
    requirements = AGENT_REQUIREMENTS
    return {
        "job_id": job_id or str(uuid4()),
        "files": files,
        "requirements": requirements,
        "checksum": package_checksum(files, requirements),
    }


async def start_client_update(
    client_id: UUID,
    *,
    registry: ClientConnectionRegistry,
    request_timeout: float = 8.0,
) -> ClientUpdateStartResult:
    connection = registry.get(client_id)
    if connection is None or connection.closed:
        raise ClientUpdateUnavailable("client is not connected")

    package = build_client_update_package()
    job_id = str(package["job_id"])
    try:
        response = await connection.request(
            AgentMessage(
                type="self_update_prepare",
                client_id=client_id,
                request_id=str(uuid4()),
                payload=package,
            ),
            timeout=2.0,
        )
        if response.type == "self_update_started":
            return ClientUpdateStartResult(client_id=client_id, job_id=job_id, method="agent_message")
        if response.type == "terminal_error":
            raise ClientUpdateUnavailable(_error_message(response))
    except TimeoutError:
        pass
    except ClientConnectionClosed as exc:
        raise ClientUpdateUnavailable("client connection closed") from exc

    await _start_legacy_terminal_update(
        connection,
        client_id,
        job_id=job_id,
        timeout=request_timeout,
    )
    return ClientUpdateStartResult(client_id=client_id, job_id=job_id, method="terminal_bootstrap")


async def _start_legacy_terminal_update(connection, client_id: UUID, *, job_id: str, timeout: float) -> None:
    window_id = uuid4()
    created = await connection.request(
        AgentMessage(
            type="create_window",
            client_id=client_id,
            window_id=window_id,
            request_id=str(uuid4()),
            payload={"cwd": None, "shell_command": "/bin/bash"},
        ),
        timeout=timeout,
    )
    if created.type == "terminal_error":
        raise ClientUpdateUnavailable(_error_message(created))
    remote_session_id = created.payload.get("remote_session_id")
    remote_window_id = created.payload.get("remote_window_id")
    if not isinstance(remote_session_id, str) or not isinstance(remote_window_id, str):
        raise ClientUpdateUnavailable("client did not return tmux target")

    attached = await connection.request(
        AgentMessage(
            type="terminal_attach",
            client_id=client_id,
            window_id=window_id,
            request_id=str(uuid4()),
            payload={"remote_session_id": remote_session_id, "remote_window_id": remote_window_id},
        ),
        timeout=timeout,
    )
    if attached.type == "terminal_error":
        raise ClientUpdateUnavailable(_error_message(attached))

    script = _legacy_update_input_script(job_id)
    await connection.send(
        AgentMessage(
            type="terminal_input",
            client_id=client_id,
            window_id=window_id,
            payload=TerminalPayload.from_bytes(window_id, script.encode("utf-8")).model_dump(mode="json"),
        )
    )


def _error_message(message: AgentMessage) -> str:
    value = message.payload.get("message")
    return value if isinstance(value, str) and value else f"client update failed: {message.type}"


def _legacy_update_input_script(job_id: str) -> str:
    return "python3 - <<'WEB_TERMINAL_UPDATE_PY'\n" + _legacy_update_python(job_id) + "\nWEB_TERMINAL_UPDATE_PY\nexit\n"


def _legacy_update_python(job_id: str) -> str:
    return f"""
import hashlib, json, pathlib, shlex, stat, subprocess, urllib.request

job_id = {json.dumps(job_id)}
config_path = pathlib.Path("~/.web-terminal-acp/config.json").expanduser()
config = json.loads(config_path.read_text(encoding="utf-8"))
install_path = pathlib.Path(config.get("install_path") or "~/.web-terminal-acp").expanduser().resolve()
base_url = str(config["server_url"]).rstrip("/")
client_id = str(config["client_id"])
url = base_url + "/api/clients/" + client_id + "/update/package?job_id=" + job_id
request = urllib.request.Request(url, headers={{"Authorization": "Bearer " + str(config["token"])}})
package = json.loads(urllib.request.urlopen(request, timeout=60).read().decode("utf-8"))
checksum_body = json.dumps({{"files": package["files"], "requirements": package["requirements"]}}, sort_keys=True, separators=(",", ":")).encode("utf-8")
if hashlib.sha256(checksum_body).hexdigest() != package.get("checksum"):
    raise RuntimeError("update package checksum mismatch")
staging = install_path / "updates" / job_id
app_root = staging / "app" / "app"
app_root.mkdir(parents=True, exist_ok=True)
for relative_path, text in package["files"].items():
    path = pathlib.Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError("unsafe update path: " + relative_path)
    target = app_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
(staging / "requirements.txt").write_text(package["requirements"], encoding="utf-8")
script = staging / "run-update.sh"
live_app = install_path / "app"
next_app = install_path / ("app.next-" + job_id)
backup_app = install_path / "backups" / ("app-" + job_id)
venv = install_path / "venv"
python_bin = venv / "bin" / "python"
update_log = install_path / "logs" / ("update-" + job_id + ".log")
client_log = install_path / "logs" / "client.log"
daemon = str(config.get("client_daemon_session") or "web_terminal_acp_client")
daemon_command = "cd " + {json.dumps("{live}")}.format(live=shlex.quote(str(live_app))) + " && PYTHONPATH=" + {json.dumps("{live}")}.format(live=shlex.quote(str(live_app))) + " " + {json.dumps("{python}")}.format(python=shlex.quote(str(python_bin))) + " -m app.client_agent --config " + {json.dumps("{config}")}.format(config=shlex.quote(str(config_path))) + " >> " + {json.dumps("{log}")}.format(log=shlex.quote(str(client_log))) + " 2>&1"
complete_url = base_url + "/api/clients/" + client_id + "/update/complete"
complete_payload = json.dumps({{"job_id": job_id}})
complete_headers = {{"Authorization": "Bearer " + str(config["token"]), "Content-Type": "application/json"}}
complete_script = (
    "import urllib.request\\n"
    "payload = " + json.dumps(complete_payload) + ".encode('utf-8')\\n"
    "request = urllib.request.Request("
    + json.dumps(complete_url)
    + ", data=payload, headers="
    + json.dumps(complete_headers)
    + ", method='POST')\\n"
    "try:\\n"
    "    urllib.request.urlopen(request, timeout=10).read()\\n"
    "except Exception as exc:\\n"
    "    print('client update completion notification failed: ' + str(exc))\\n"
)
body = "#!/usr/bin/env bash\\nset -euo pipefail\\nexec >> " + {json.dumps("{log}")}.format(log=shlex.quote(str(update_log))) + " 2>&1\\n"
body += "mkdir -p " + {json.dumps("{backups}")}.format(backups=shlex.quote(str(install_path / "backups"))) + " " + {json.dumps("{logs}")}.format(logs=shlex.quote(str(install_path / "logs"))) + "\\n"
body += "python3 -m venv " + {json.dumps("{venv}")}.format(venv=shlex.quote(str(venv))) + "\\n"
body += {json.dumps("{python}")}.format(python=shlex.quote(str(python_bin))) + " -m pip install --upgrade pip\\n"
body += {json.dumps("{python}")}.format(python=shlex.quote(str(python_bin))) + " -m pip install -r " + {json.dumps("{req}")}.format(req=shlex.quote(str(staging / "requirements.txt"))) + "\\n"
body += "rm -rf " + {json.dumps("{next}")}.format(next=shlex.quote(str(next_app))) + "\\n"
body += "cp -a " + {json.dumps("{stage}")}.format(stage=shlex.quote(str(staging / "app"))) + " " + {json.dumps("{next}")}.format(next=shlex.quote(str(next_app))) + "\\n"
body += "if [ -d " + {json.dumps("{live}")}.format(live=shlex.quote(str(live_app))) + " ]; then rm -rf " + {json.dumps("{backup}")}.format(backup=shlex.quote(str(backup_app))) + "; mv " + {json.dumps("{live}")}.format(live=shlex.quote(str(live_app))) + " " + {json.dumps("{backup}")}.format(backup=shlex.quote(str(backup_app))) + "; fi\\n"
body += "mv " + {json.dumps("{next}")}.format(next=shlex.quote(str(next_app))) + " " + {json.dumps("{live}")}.format(live=shlex.quote(str(live_app))) + "\\n"
pattern = "python.*-m app[.]client_agent.*--config " + __import__("re").escape(str(config_path))
body += "tmux kill-session -t " + shlex.quote(daemon) + " >/dev/null 2>&1 || true\\n"
body += "for pid in $(pgrep -f " + shlex.quote(pattern) + " || true); do if [ \\"$pid\\" != \\"$$\\" ]; then kill \\"$pid\\" >/dev/null 2>&1 || true; fi; done\\n"
body += "sleep 1\\n"
body += "for pid in $(pgrep -f " + shlex.quote(pattern) + " || true); do if [ \\"$pid\\" != \\"$$\\" ]; then kill -9 \\"$pid\\" >/dev/null 2>&1 || true; fi; done\\n"
body += "tmux new-session -d -s " + shlex.quote(daemon) + " " + shlex.quote(daemon_command) + "\\n"
body += shlex.quote(str(python_bin)) + " - <<'WEB_TERMINAL_UPDATE_COMPLETE_PY'\\n" + complete_script + "WEB_TERMINAL_UPDATE_COMPLETE_PY\\n"
script.write_text(body, encoding="utf-8")
script.chmod(script.stat().st_mode | stat.S_IXUSR)
session = "web_terminal_acp_update_" + job_id.replace("-", "_")
subprocess.check_call(["tmux", "new-session", "-d", "-s", session, str(script)])
"""
