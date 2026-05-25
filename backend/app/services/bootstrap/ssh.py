from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
import posixpath
import shlex
from types import TracebackType

import paramiko


@dataclass(frozen=True)
class SshConnectionInfo:
    host: str
    port: int
    username: str
    private_key: str
    passphrase: str | None = None


class SshCommandError(RuntimeError):
    def __init__(self, command: str, exit_status: int, stderr: str) -> None:
        self.command = command
        self.exit_status = exit_status
        self.stderr = stderr
        super().__init__(f"remote command failed ({exit_status}): {stderr.strip()}")


class SshClient:
    def __init__(self, info: SshConnectionInfo) -> None:
        self.info = info
        self._client: paramiko.SSHClient | None = None

    def __enter__(self) -> SshClient:
        key = load_private_key(self.info.private_key, self.info.passphrase)
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.info.host,
            port=self.info.port,
            username=self.info.username,
            pkey=key,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
        )
        self._client = client
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def run(self, command: str) -> str:
        client = self._require_client()
        _stdin, stdout, stderr = client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        stdout_text = stdout.read().decode("utf-8", errors="replace")
        stderr_text = stderr.read().decode("utf-8", errors="replace")
        if exit_status != 0:
            raise SshCommandError(command, exit_status, stderr_text or stdout_text)
        return stdout_text

    def upload_text(self, remote_path: str, text: str, mode: int = 0o600) -> None:
        parent = posixpath.dirname(remote_path)
        if parent:
            self.run(f"mkdir -p -- {_quote_remote_path(parent)}")

        sftp_path = _sftp_path(remote_path)
        client = self._require_client()
        with client.open_sftp() as sftp:
            with sftp.file(sftp_path, "w") as remote_file:
                remote_file.write(text)
            sftp.chmod(sftp_path, mode)

    def _require_client(self) -> paramiko.SSHClient:
        if self._client is None:
            raise RuntimeError("SSH client is not connected")
        return self._client


def load_private_key(private_key: str, passphrase: str | None = None) -> paramiko.PKey:
    errors: list[Exception] = []
    for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_class.from_private_key(StringIO(private_key), password=passphrase)
        except Exception as exc:  # pragma: no cover - Paramiko uses several parse exception types.
            errors.append(exc)
    raise ValueError("unsupported or invalid SSH private key") from errors[-1]


def _quote_remote_path(path: str) -> str:
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        suffix_parts = [part for part in path[2:].split("/") if part]
        suffix = "/".join(shlex.quote(part) for part in suffix_parts)
        return f'"$HOME"/{suffix}' if suffix else '"$HOME"'
    return shlex.quote(path)


def _sftp_path(path: str) -> str:
    if path == "~":
        return "."
    if path.startswith("~/"):
        return path[2:]
    return path
