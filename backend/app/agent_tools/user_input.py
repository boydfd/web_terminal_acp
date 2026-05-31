from __future__ import annotations

import re

_TAG_PATTERNS = (
    re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<objective>\s*(.*?)\s*</objective>", re.DOTALL | re.IGNORECASE),
)

_SYNTHETIC_PREFIXES = (
    "# AGENTS.md instructions for ",
    "<environment_context>",
    "<goal_context>",
    "<turn_aborted>",
    "<user_info>",
    "<system-reminder>",
    "<system_reminder>",
    "<conversation_summary>",
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)

_SENSITIVE_PREFIXES = (
    "<encrypted",
    "encrypted:",
    "encrypted ",
    "ciphertext:",
    "-----begin",
)


def extract_real_user_input(text: str | None, *, provider: str | None = None) -> str | None:
    if text is None:
        return None

    stripped = text.strip()
    if not stripped:
        return None

    tagged_parts: list[str] = []
    for pattern in _TAG_PATTERNS:
        tagged_parts.extend(part.strip() for part in pattern.findall(stripped) if part.strip())
    if tagged_parts:
        tagged_input = "\n\n".join(tagged_parts)
        return None if _is_sensitive_message(tagged_input) else tagged_input

    if _is_synthetic_user_message(stripped, provider=provider):
        return None

    if _is_sensitive_message(stripped):
        return None

    return stripped


def _is_synthetic_user_message(text: str, *, provider: str | None) -> bool:
    lowered = text.lower()
    if any(lowered.startswith(prefix.lower()) for prefix in _SYNTHETIC_PREFIXES):
        return True

    if provider == "cursor_cli" and lowered.startswith("<user_info>"):
        return True

    if text.startswith("# AGENTS.md instructions for ") and "<INSTRUCTIONS>" in text:
        return True

    return False


def _is_sensitive_message(text: str) -> bool:
    lowered = text.lower()
    return any(lowered.startswith(prefix) for prefix in _SENSITIVE_PREFIXES)
