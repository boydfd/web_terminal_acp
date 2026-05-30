import os
from pathlib import Path

from app.config import Settings


SETTINGS_ENV_VARS = (
    "APP_HOST",
    "APP_PORT",
    "DATABASE_URL",
    "ELASTICSEARCH_URL",
    "TMUX_POOL_SESSION",
    "DEFAULT_SHELL",
    "CLAUDE_PROJECTS_DIR",
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_API_KEY",
    "OPENAI_COMPAT_MODEL",
    "REDIS_URL",
    "SUMMARY_OUTPUT_LANGUAGE",
    "WEB_TERMINAL_AUTH_SECRET",
    "WEB_TERMINAL_AUTH_SESSION_TTL_SECONDS",
)


def clear_settings_env(monkeypatch):
    settings_env_var_names = {env_var.lower() for env_var in SETTINGS_ENV_VARS}
    for env_var in list(os.environ):
        if env_var.lower() in settings_env_var_names:
            monkeypatch.delenv(env_var, raising=False)


def test_clear_settings_env_removes_case_insensitive_variants(monkeypatch):
    monkeypatch.setenv("app_host", "0.0.0.0")
    monkeypatch.setenv("app_port", "1234")
    monkeypatch.setenv("openai_compat_model", "from-env")

    clear_settings_env(monkeypatch)

    settings = Settings(_env_file=None)
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8000
    assert settings.openai_compat_model == "local-summarizer"


def test_clear_settings_env_removes_mixed_case_variants(monkeypatch):
    monkeypatch.setenv("App_Host", "0.0.0.0")
    monkeypatch.setenv("App_Port", "1234")
    monkeypatch.setenv("OpenAI_Compat_Model", "from-env")

    clear_settings_env(monkeypatch)

    settings = Settings(_env_file=None)
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8000
    assert settings.openai_compat_model == "local-summarizer"


def test_settings_defaults_bind_locally(monkeypatch):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")

    settings = Settings(_env_file=None)

    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8000
    assert settings.default_shell == "/usr/bin/zsh"


def test_settings_default_shell_auto_uses_user_shell(monkeypatch):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")

    settings = Settings(_env_file=None, default_shell="auto")

    assert settings.default_shell == "/usr/bin/zsh"


def test_settings_default_shell_can_be_forced(monkeypatch):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")

    settings = Settings(_env_file=None, default_shell="/bin/bash")

    assert settings.default_shell == "/bin/bash"


def test_settings_env_file_points_to_project_root():
    project_root = Path(__file__).resolve().parents[3]

    assert Settings.model_config["env_file"] == project_root / ".env"


def test_settings_accept_openai_compatible_fields(monkeypatch):
    clear_settings_env(monkeypatch)

    settings = Settings(
        _env_file=None,
        openai_compat_base_url="http://127.0.0.1:11434/v1",
        openai_compat_api_key="key",
        openai_compat_model="model-a",
    )
    assert settings.openai_compat_base_url == "http://127.0.0.1:11434/v1"
    assert settings.openai_compat_api_key == "key"
    assert settings.openai_compat_model == "model-a"


def test_settings_default_summary_output_language(monkeypatch):
    clear_settings_env(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.summary_output_language == "中文"


def test_settings_accept_summary_output_language(monkeypatch):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("SUMMARY_OUTPUT_LANGUAGE", "English")

    settings = Settings(_env_file=None)

    assert settings.summary_output_language == "English"


def test_settings_accept_redis_url(monkeypatch):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")

    settings = Settings(_env_file=None)

    assert settings.redis_url == "redis://redis:6379/0"


def test_settings_accept_auth_secret(monkeypatch):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("WEB_TERMINAL_AUTH_SECRET", "login-secret")
    monkeypatch.setenv("WEB_TERMINAL_AUTH_SESSION_TTL_SECONDS", "30")

    settings = Settings(_env_file=None)

    assert settings.web_terminal_auth_secret == "login-secret"
    assert settings.web_terminal_auth_session_ttl_seconds == 30
