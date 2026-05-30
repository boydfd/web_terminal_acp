from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import Request, WebSocket, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config import get_settings

AUTH_LOGIN_PATH = "/api/auth/login"
AUTH_STATUS_PATH = "/api/auth/status"
CLIENT_REGISTRATION_PATH = "/api/clients/register"
CLIENT_REGISTRATION_SCRIPT_PATH = "/api/clients/register-script"
HEALTH_PATH = "/healthz"
_TOKEN_PREFIX = "wtauth"


def auth_enabled() -> bool:
    if get_settings().web_terminal_disable_auth_for_tests:
        return False
    return bool((get_settings().web_terminal_auth_secret or "").strip())


def _auth_secret() -> str:
    secret = (get_settings().web_terminal_auth_secret or "").strip()
    if not secret:
        raise RuntimeError("WEB_TERMINAL_AUTH_SECRET is not configured")
    return secret


def _session_ttl_seconds() -> int:
    ttl = get_settings().web_terminal_auth_session_ttl_seconds
    return max(ttl, 1)


def _sign(message: str) -> str:
    return hmac.new(_auth_secret().encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else now
    nonce = secrets.token_urlsafe(24)
    body = f"{issued_at}.{nonce}"
    return f"{_TOKEN_PREFIX}.{body}.{_sign(body)}"


def verify_login_secret(secret: str) -> bool:
    configured = _auth_secret()
    return hmac.compare_digest(secret, configured)


def verify_session_token(token: str, now: int | None = None) -> bool:
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _TOKEN_PREFIX:
        return False
    issued_at_text, nonce, signature = parts[1], parts[2], parts[3]
    if not issued_at_text or not nonce or not signature:
        return False
    body = f"{issued_at_text}.{nonce}"
    if not hmac.compare_digest(signature, _sign(body)):
        return False
    try:
        issued_at = int(issued_at_text)
    except ValueError:
        return False
    current_time = int(time.time()) if now is None else now
    if issued_at > current_time + 60:
        return False
    return current_time - issued_at <= _session_ttl_seconds()


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return None
    return token


def _request_token(request: Request) -> str | None:
    return _bearer_token(request.headers.get("authorization"))


def websocket_session_token(websocket: WebSocket) -> str | None:
    token = websocket.query_params.get("auth_token")
    if token:
        return token
    return _bearer_token(websocket.headers.get("authorization"))


def is_websocket_authenticated(websocket: WebSocket) -> bool:
    if not auth_enabled():
        return True
    token = websocket_session_token(websocket)
    return token is not None and verify_session_token(token)


async def require_websocket_auth(websocket: WebSocket) -> bool:
    if is_websocket_authenticated(websocket):
        return True
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    return False


def _is_registration_callback(path: str) -> bool:
    return path in {CLIENT_REGISTRATION_PATH, CLIENT_REGISTRATION_SCRIPT_PATH}


def _is_client_update_callback(path: str) -> bool:
    if not path.startswith("/api/clients/"):
        return False
    return path.endswith("/update/package") or path.endswith("/update/complete")


def _requires_http_auth(path: str) -> bool:
    if path in {HEALTH_PATH, AUTH_LOGIN_PATH, AUTH_STATUS_PATH}:
        return False
    if _is_registration_callback(path) or _is_client_update_callback(path):
        return False
    return path.startswith("/api/")


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        if auth_enabled() and _requires_http_auth(request.url.path):
            token = _request_token(request)
            if token is None or not verify_session_token(token):
                return JSONResponse(
                    {"detail": "login required"},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
        return await call_next(request)
