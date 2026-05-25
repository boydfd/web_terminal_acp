from __future__ import annotations

import re
from typing import Any


_REDACTED = "[REDACTED]"
_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
)


def redact_secrets(value: Any, *, key_name: str | None = None) -> Any:
    if key_name is not None and _is_secret_key(key_name):
        return _REDACTED
    if isinstance(value, dict):
        return {key: redact_secrets(item, key_name=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_secret_patterns(value)
    return value


def _is_secret_key(key_name: str) -> bool:
    normalized_key = key_name.lower().replace("-", "_")
    return any(secret_part in normalized_key for secret_part in _SECRET_KEY_PARTS)


def _redact_secret_patterns(value: str) -> str:
    redacted_value = value
    for pattern in _SECRET_PATTERNS:
        redacted_value = pattern.sub(_REDACTED, redacted_value)
    return redacted_value
