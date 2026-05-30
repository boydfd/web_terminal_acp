#!/usr/bin/env bash
set -euo pipefail

WEB_TERMINAL_SERVER_URL="${WEB_TERMINAL_SERVER_URL:-}"
WEB_TERMINAL_REGISTRATION_KEY="${WEB_TERMINAL_REGISTRATION_KEY:-}"
WEB_TERMINAL_CLIENT_NAME="${WEB_TERMINAL_CLIENT_NAME:-$(hostname 2>/dev/null || echo remote-client)}"
WEB_TERMINAL_INSTALL_PATH="${WEB_TERMINAL_INSTALL_PATH:-$HOME/.web-terminal-acp}"

usage() {
  cat <<'USAGE'
Usage:
  WEB_TERMINAL_SERVER_URL=http://host:8001 \
  WEB_TERMINAL_REGISTRATION_KEY=wtr_xxx \
  ./register-client-direct.sh

Options:
  --server-url URL       Web Terminal backend URL
  --registration-key KEY One-time registration key generated in Settings
  --name NAME            Client display name
  --install-path PATH    Install path, default ~/.web-terminal-acp
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server-url)
      WEB_TERMINAL_SERVER_URL="${2:-}"
      shift 2
      ;;
    --registration-key)
      WEB_TERMINAL_REGISTRATION_KEY="${2:-}"
      shift 2
      ;;
    --name)
      WEB_TERMINAL_CLIENT_NAME="${2:-}"
      shift 2
      ;;
    --install-path)
      WEB_TERMINAL_INSTALL_PATH="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$WEB_TERMINAL_SERVER_URL" ] || [ -z "$WEB_TERMINAL_REGISTRATION_KEY" ]; then
  usage >&2
  exit 2
fi

missing=""
command -v python3 >/dev/null 2>&1 || missing="$missing python3"
command -v tmux >/dev/null 2>&1 || missing="$missing tmux"
command -v bash >/dev/null 2>&1 || missing="$missing bash"
venv_test="$(mktemp -d 2>/dev/null || true)"
if [ -z "$venv_test" ] || ! python3 -m venv "$venv_test/venv" >/dev/null 2>&1 || ! "$venv_test/venv/bin/python" -m pip --version >/dev/null 2>&1; then
  missing="$missing python3-venv"
fi
rm -rf "$venv_test"
if [ -n "$missing" ]; then
  echo "missing dependencies:$missing" >&2
  exit 42
fi

export WEB_TERMINAL_SERVER_URL
export WEB_TERMINAL_REGISTRATION_KEY
export WEB_TERMINAL_CLIENT_NAME
export WEB_TERMINAL_INSTALL_PATH

python3 - <<'PY'
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
import urllib.error
import urllib.request

server_url = os.environ["WEB_TERMINAL_SERVER_URL"].rstrip("/")
registration_key = os.environ["WEB_TERMINAL_REGISTRATION_KEY"]
client_name = os.environ["WEB_TERMINAL_CLIENT_NAME"]
install_path = pathlib.Path(os.environ["WEB_TERMINAL_INSTALL_PATH"]).expanduser().resolve()
hostname = os.uname().nodename

payload = json.dumps({
    "registration_key": registration_key,
    "name": client_name,
    "hostname": hostname,
    "install_path": str(install_path),
    "server_url": server_url,
}).encode("utf-8")
request = urllib.request.Request(
    server_url + "/api/clients/register",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    response = urllib.request.urlopen(request, timeout=60)
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", errors="replace")
    raise SystemExit(f"registration failed: HTTP {exc.code} {detail}") from exc

body = json.loads(response.read().decode("utf-8"))
package = body["package"]
checksum_body = json.dumps(
    {"files": package["files"], "requirements": package["requirements"]},
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
if hashlib.sha256(checksum_body).hexdigest() != package.get("checksum"):
    raise SystemExit("registration package checksum mismatch")

app_root = install_path / "app" / "app"
logs_path = install_path / "logs"
app_root.mkdir(parents=True, exist_ok=True)
logs_path.mkdir(parents=True, exist_ok=True)
(install_path / "npm-global" / "bin").mkdir(parents=True, exist_ok=True)
(install_path / "config.json").write_text(json.dumps(body["config"], indent=2, sort_keys=True), encoding="utf-8")
(install_path / "config.json").chmod(0o600)
(install_path / "requirements.txt").write_text(package["requirements"], encoding="utf-8")
for relative_path, text in package["files"].items():
    path = pathlib.Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"unsafe package path: {relative_path}")
    target = app_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

venv = install_path / "venv"
python_bin = venv / "bin" / "python"
subprocess.check_call(["python3", "-m", "venv", str(venv)])
subprocess.check_call([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"])
subprocess.check_call([str(python_bin), "-m", "pip", "install", "-r", str(install_path / "requirements.txt")])

daemon = str(body["config"].get("client_daemon_session") or "web_terminal_acp_client")
config_path = install_path / "config.json"
client_log = logs_path / "client.log"
pattern = "python.*-m app[.]client_agent.*--config " + str(config_path)
subprocess.run(["tmux", "kill-session", "-t", daemon], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(
    "for pid in $(pgrep -f " + shlex.quote(pattern) + " || true); do "
    "if [ \"$pid\" != \"$$\" ]; then kill \"$pid\" >/dev/null 2>&1 || true; fi; done",
    shell=True,
    check=False,
)
command = (
    "cd " + shlex.quote(str(install_path / "app")) + " && "
    "PATH=" + shlex.quote(str(install_path / "npm-global" / "bin")) + ":$PATH "
    "PYTHONPATH=" + shlex.quote(str(install_path / "app")) + " "
    + shlex.quote(str(python_bin)) + " -m app.client_agent "
    "--config " + shlex.quote(str(config_path)) + " >> " + shlex.quote(str(client_log)) + " 2>&1"
)
subprocess.check_call(["tmux", "new-session", "-d", "-s", daemon, command])
print("registered client " + str(body["client_id"]) + " and started tmux session " + daemon)
PY
