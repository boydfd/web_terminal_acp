from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import stat
from pathlib import Path
from typing import Any

from app.client_agent.config import ClientAgentConfig

_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def package_checksum(files: dict[str, str], requirements: str) -> str:
    body = json.dumps(
        {"files": files, "requirements": requirements},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def validate_update_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str], str]:
    job_id = payload.get("job_id")
    files = payload.get("files")
    requirements = payload.get("requirements")
    checksum = payload.get("checksum")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("update payload missing job_id")
    if _SAFE_JOB_ID.fullmatch(job_id) is None:
        raise ValueError("update payload has unsafe job_id")
    if not isinstance(files, dict) or not files:
        raise ValueError("update payload missing files")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in files.items()):
        raise ValueError("update payload files must be string map")
    if not isinstance(requirements, str):
        raise ValueError("update payload missing requirements")
    if checksum != package_checksum(files, requirements):
        raise ValueError("update payload checksum mismatch")
    return job_id, files, requirements


def _safe_join(root: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe update path: {relative_path}")
    return root / path


async def start_self_update(
    config: ClientAgentConfig,
    payload: dict[str, Any],
    *,
    runner=None,
) -> dict[str, str]:
    job_id, files, requirements = validate_update_payload(payload)
    install_path = config.install_path.expanduser().resolve()
    staging_root = install_path / "updates" / job_id
    staging_app = staging_root / "app"
    logs_path = install_path / "logs"
    logs_path.mkdir(parents=True, exist_ok=True)
    staging_app.mkdir(parents=True, exist_ok=True)

    for relative_path, text in files.items():
        target = _safe_join(staging_app / "app", relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    (staging_root / "requirements.txt").write_text(requirements, encoding="utf-8")
    script_path = staging_root / "run-update.sh"
    script_path.write_text(_updater_script(config, job_id, install_path, staging_root), encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)

    session_name = f"web_terminal_acp_update_{job_id.replace('-', '_')}"
    command = ["tmux", "new-session", "-d", "-s", session_name, str(script_path)]
    if runner is None:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "failed to start updater")
    else:
        await runner(command)

    return {"job_id": job_id, "tmux_session": session_name}


def _updater_script(
    config: ClientAgentConfig,
    job_id: str,
    install_path: Path,
    staging_root: Path,
) -> str:
    live_app = install_path / "app"
    next_app = install_path / f"app.next-{job_id}"
    backup_app = install_path / "backups" / f"app-{job_id}"
    venv_path = install_path / "venv"
    python_path = venv_path / "bin" / "python"
    config_path = install_path / "config.json"
    update_log = install_path / "logs" / f"update-{job_id}.log"
    client_log = install_path / "logs" / "client.log"
    stop_existing_processes = _kill_existing_client_processes_command(config_path)
    daemon_command = (
        f"cd {shlex.quote(str(live_app))} && "
        f"PYTHONPATH={shlex.quote(str(live_app))} "
        f"{shlex.quote(str(python_path))} -m app.client_agent "
        f"--config {shlex.quote(str(config_path))} >> {shlex.quote(str(client_log))} 2>&1"
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail
exec >> {shlex.quote(str(update_log))} 2>&1
echo "starting client update {job_id} $(date -Is)"
mkdir -p {shlex.quote(str(install_path / "backups"))} {shlex.quote(str(install_path / "logs"))}
python3 -m venv {shlex.quote(str(venv_path))}
{shlex.quote(str(python_path))} -m pip install --upgrade pip
{shlex.quote(str(python_path))} -m pip install -r {shlex.quote(str(staging_root / "requirements.txt"))}
rm -rf {shlex.quote(str(next_app))}
cp -a {shlex.quote(str(staging_root / "app"))} {shlex.quote(str(next_app))}
if [ -d {shlex.quote(str(live_app))} ]; then
  rm -rf {shlex.quote(str(backup_app))}
  mv {shlex.quote(str(live_app))} {shlex.quote(str(backup_app))}
fi
mv {shlex.quote(str(next_app))} {shlex.quote(str(live_app))}
tmux kill-session -t {shlex.quote(config.client_daemon_session)} >/dev/null 2>&1 || true
{stop_existing_processes}
tmux new-session -d -s {shlex.quote(config.client_daemon_session)} {shlex.quote(daemon_command)}
{_completion_notification_command(config, job_id, python_path)}
echo "client update {job_id} finished $(date -Is)"
"""


def _completion_notification_command(
    config: ClientAgentConfig,
    job_id: str,
    python_path: Path,
) -> str:
    url = f"{config.server_url.rstrip('/')}/api/clients/{config.client_id}/update/complete"
    headers = {
        "Authorization": f"Bearer {config.token}",
        "Content-Type": "application/json",
    }
    payload = json.dumps({"job_id": job_id})
    script = f"""import urllib.request
payload = {json.dumps(payload)}.encode("utf-8")
request = urllib.request.Request(
    {json.dumps(url)},
    data=payload,
    headers={json.dumps(headers)},
    method="POST",
)
try:
    urllib.request.urlopen(request, timeout=10).read()
except Exception as exc:
    print("client update completion notification failed: " + str(exc))
"""
    return (
        f"{shlex.quote(str(python_path))} - <<'WEB_TERMINAL_UPDATE_COMPLETE_PY'\n"
        f"{script}"
        "WEB_TERMINAL_UPDATE_COMPLETE_PY\n"
    )


def _kill_existing_client_processes_command(config_path: Path) -> str:
    pattern = f"python.*-m app[.]client_agent.*--config {re.escape(str(config_path))}"
    quoted_pattern = shlex.quote(pattern)
    return f"""for pid in $(pgrep -f {quoted_pattern} || true); do
  if [ "$pid" != "$$" ]; then kill "$pid" >/dev/null 2>&1 || true; fi
done
sleep 1
for pid in $(pgrep -f {quoted_pattern} || true); do
  if [ "$pid" != "$$" ]; then kill -9 "$pid" >/dev/null 2>&1 || true; fi
done"""
