"""Accounts: authentication, admin user management, and per-user holdings."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from backend import dependencies as deps
from backend.dependencies import (
    SESSION_COOKIE_NAME,
    AuthRequest,
    HoldingsReplaceRequest,
    HoldingUpsertRequest,
    _auth_source,
    _clear_session_cookie,
    _current_user,
    _holdings_payload,
    _require_user,
    _set_session_cookie,
)
from backend.services.auth import AuthRateLimitError, super_user_username

router = APIRouter()


@router.post("/api/auth/register")
def register(payload: AuthRequest, request: Request, response: Response) -> dict:
    source = _auth_source(request)
    try:
        deps.auth_service.assert_auth_allowed(payload.username, source)
    except AuthRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after_seconds)}) from exc
    try:
        user = deps.auth_service.create_user(payload.username, payload.password, payload.displayName)
    except ValueError as exc:
        deps.auth_service.record_auth_failure(payload.username, source)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps.auth_service.clear_auth_failures(payload.username, source)
    token = deps.auth_service.create_session(user.id)
    _set_session_cookie(response, token)
    holdings = deps.auth_service.list_holdings(user.id)
    return {"authenticated": True, "user": user.public_dict(), "holdings": [h.model_dump() for h in holdings]}


@router.post("/api/auth/login")
def login(payload: AuthRequest, request: Request, response: Response) -> dict:
    source = _auth_source(request)
    try:
        deps.auth_service.assert_auth_allowed(payload.username, source)
    except AuthRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after_seconds)}) from exc
    user = deps.auth_service.authenticate(payload.username, payload.password)
    if user is None:
        deps.auth_service.record_auth_failure(payload.username, source)
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    deps.auth_service.clear_auth_failures(payload.username, source)
    token = deps.auth_service.create_session(user.id)
    _set_session_cookie(response, token)
    holdings = deps.auth_service.list_holdings(user.id)
    return {"authenticated": True, "user": user.public_dict(), "holdings": [h.model_dump() for h in holdings]}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    deps.auth_service.delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    _clear_session_cookie(response)
    return {"authenticated": False}


@router.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    user = _current_user(request)
    if user is None:
        return {"authenticated": False, "user": None, "holdings": []}
    holdings = deps.auth_service.list_holdings(user.id)
    return {"authenticated": True, "user": user.public_dict(), "holdings": [h.model_dump() for h in holdings]}


@router.get("/api/admin/users")
def list_admin_users(request: Request) -> dict:
    deps._require_super_user(request)
    return {"superUser": super_user_username(), "users": deps.auth_service.list_users()}


@router.delete("/api/admin/users/{user_id}")
def delete_admin_user(user_id: int, request: Request) -> dict:
    deps._require_super_user(request)
    try:
        deleted = deps.auth_service.delete_user(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return {"superUser": super_user_username(), "users": deps.auth_service.list_users()}


@router.get("/api/me/holdings")
def get_my_holdings(request: Request) -> dict:
    user = _require_user(request)
    return _holdings_payload(deps.auth_service.list_holdings(user.id))


@router.put("/api/me/holdings")
def replace_my_holdings(payload: HoldingsReplaceRequest, request: Request) -> dict:
    user = _require_user(request)
    return _holdings_payload(deps.auth_service.replace_holdings(user.id, payload.holdings))


@router.post("/api/me/holdings")
def upsert_my_holding(payload: HoldingUpsertRequest, request: Request) -> dict:
    user = _require_user(request)
    return _holdings_payload(deps.auth_service.upsert_holding(user.id, payload.holding))


@router.delete("/api/me/holdings/{stock_code}")
def delete_my_holding(stock_code: str, request: Request) -> dict:
    user = _require_user(request)
    return _holdings_payload(deps.auth_service.delete_holding(user.id, stock_code))
