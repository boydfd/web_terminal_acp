from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    default_shell: str = "/bin/bash"
    claude_projects_dir: str = "~/.claude/projects"
    openai_compat_base_url: str = "http://127.0.0.1:11434/v1"
    openai_compat_api_key: str = "dev-local-key"
    openai_compat_model: str = "local-summarizer"
    openai_compat_timeout_seconds: float = 60.0
    terminal_summary_idle_seconds: int = 120
    terminal_summary_initial_max_wait_seconds: int = 120
    terminal_summary_repeat_seconds: int = 600
    terminal_summary_input_context_max_bytes: int = 32768
    summary_output_language: str = "中文"


@lru_cache
def get_settings() -> Settings:
    return Settings()
