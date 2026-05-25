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
    "SUMMARY_OUTPUT_LANGUAGE",
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

    settings = Settings(_env_file=None)

    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8000


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
