from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.auth import auth_enabled, create_session_token, verify_login_secret
from app.schemas import LoginIn, LoginOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
async def read_auth_status() -> dict[str, bool]:
    return {"enabled": auth_enabled()}


@router.post("/login", response_model=LoginOut)
async def login(payload: LoginIn) -> LoginOut:
    if not auth_enabled():
        return LoginOut(token="", enabled=False)
    if not verify_login_secret(payload.secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid login secret")
    return LoginOut(token=create_session_token(), enabled=True)
