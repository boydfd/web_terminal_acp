from __future__ import annotations

from importlib import resources


def read_direct_registration_script() -> str:
    return resources.files("app.resources").joinpath("register-client-direct.sh").read_text(encoding="utf-8")
