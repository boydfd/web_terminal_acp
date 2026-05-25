import os
import pwd
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field


def default_user_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
    except KeyError:
        return "/bin/bash"


class ClientAgentConfig(BaseModel):
    client_id: UUID
    token: str
    server_url: str
    name: str
    install_path: Path
    tmux_pool_session: str = "web_terminal_acp_pool"
    client_daemon_session: str = "web_terminal_acp_client"
    reconnect_initial_delay_seconds: float = 1
    reconnect_max_delay_seconds: float = 30
    default_shell: str = Field(default_factory=default_user_shell)

    @classmethod
    def load(cls, path: Path) -> "ClientAgentConfig":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def _websocket_base_url(self) -> str:
        base_url = self.server_url.rstrip("/")
        if base_url.startswith("https://"):
            return f"wss://{base_url.removeprefix('https://')}"
        if base_url.startswith("http://"):
            return f"ws://{base_url.removeprefix('http://')}"
        return base_url

    @property
    def websocket_url(self) -> str:
        base_url = self._websocket_base_url()
        if base_url.endswith("/api/client-agent/ws"):
            return base_url
        if base_url.endswith("/api/client-agent/bulk-ws"):
            return f"{base_url.removesuffix('/api/client-agent/bulk-ws')}/api/client-agent/ws"
        return f"{base_url}/api/client-agent/ws"

    @property
    def bulk_websocket_url(self) -> str:
        base_url = self._websocket_base_url()
        if base_url.endswith("/api/client-agent/bulk-ws"):
            return base_url
        if base_url.endswith("/api/client-agent/ws"):
            return f"{base_url.removesuffix('/api/client-agent/ws')}/api/client-agent/bulk-ws"
        return f"{base_url}/api/client-agent/bulk-ws"
