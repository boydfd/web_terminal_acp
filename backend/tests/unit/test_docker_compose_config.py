from __future__ import annotations

from pathlib import Path
import re


def test_backend_claude_mount_uses_host_prefixed_env_var() -> None:
    compose = Path(__file__).resolve().parents[2].parent / "docker-compose.yml"
    content = compose.read_text(encoding="utf-8")

    assert re.search(r"\$\{HOST_CLAUDE_CONFIG_DIR:-[^}]+\}:/home/appuser/\.claude\b", content)
    assert re.search(r"\$\{HOST_CLAUDE_JSON:-[^}]+\}:/home/appuser/\.claude\.json\b", content)
    assert not re.search(r"\$\{CLAUDE_CONFIG_DIR:-[^}]+\}:/home/appuser/\.claude\b", content)


def test_compose_configures_redis_cache_and_backend_memory() -> None:
    compose = Path(__file__).resolve().parents[2].parent / "docker-compose.yml"
    content = compose.read_text(encoding="utf-8")

    assert re.search(r"^\s+redis:\n\s+image: redis:7\.2-alpine", content, flags=re.MULTILINE)
    assert "REDIS_URL: redis://redis:6379/0" in content
    assert re.search(r"links:.*?-\s+redis", content, flags=re.DOTALL)
    assert re.search(r"backend:.*?memory: 2g", content, flags=re.DOTALL)


def test_compose_enables_postgres_slow_query_monitoring() -> None:
    compose = Path(__file__).resolve().parents[2].parent / "docker-compose.yml"
    content = compose.read_text(encoding="utf-8")

    assert "shared_preload_libraries=pg_stat_statements" in content
    assert "pg_stat_statements.track=all" in content
    assert "track_io_timing=on" in content
    assert "log_min_duration_statement=${POSTGRES_LOG_MIN_DURATION_STATEMENT_MS:-500}" in content
    assert "log_line_prefix=%m [%p] user=%u,db=%d,app=%a,client=%h " in content
    assert "log_lock_waits=on" in content


def test_frontend_build_passes_onboarding_flag_explicitly() -> None:
    compose = Path(__file__).resolve().parents[2].parent / "docker-compose.yml"
    content = compose.read_text(encoding="utf-8")

    assert "VITE_ENABLE_ONBOARDING: ${VITE_ENABLE_ONBOARDING:-}" in content
