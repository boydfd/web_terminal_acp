from functools import lru_cache
import os
from pathlib import Path
import pwd

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_user_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
    except KeyError:
        return "/bin/bash"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_host: str = "127.0.0.1"
    app_port: int = 8000
    database_url: str = "postgresql+asyncpg://web_terminal:dev_password@127.0.0.1:15436/web_terminal_acp"
    elasticsearch_url: str = "http://127.0.0.1:19201"
    tmux_pool_session: str = "web_terminal_acp_pool"
    default_shell: str = Field(default_factory=default_user_shell)
    claude_projects_dir: str = "~/.claude/projects"
    openai_compat_base_url: str = "http://127.0.0.1:11434/v1"
    openai_compat_api_key: str = "dev-local-key"
    openai_compat_model: str = "local-summarizer"
    openai_compat_timeout_seconds: float = 60.0
    redis_url: str | None = None
    web_terminal_auth_secret: str | None = None
    web_terminal_auth_session_ttl_seconds: int = 604800
    web_terminal_disable_auth_for_tests: bool = False
    terminal_summary_idle_seconds: int = 20
    terminal_summary_initial_max_wait_seconds: int = 120
    terminal_summary_repeat_seconds: int = 600
    terminal_summary_input_context_max_bytes: int = 32768
    summary_output_language: str = "中文"

    @field_validator("default_shell")
    @classmethod
    def resolve_default_shell(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in {"", "auto", "login"}:
            return default_user_shell()
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
