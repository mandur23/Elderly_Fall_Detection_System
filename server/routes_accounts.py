"""계정 API: 회원가입·로그인, 내 정보, 보호자 연결, 기기, 웹 푸시 구독.

규칙은 server/auth.py의 Accounts가 갖고, 여기서는 요청 검증과
오류를 HTTP 상태로 바꾸는 일만 한다.
"""

import logging
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from server.auth import Accounts, current_user
from server.config import Settings
from server.db import GUARDIAN_ACCEPTED, GUARDIAN_PENDING

log = logging.getLogger("server.routes_accounts")

router = APIRouter()


# --- 요청 본문 --------------------------------------------------------------
# 길이 상한은 비정상적으로 큰 입력을 막기 위한 것이고, 실제 규칙(비밀번호 8자 이상 등)은 Accounts가 검사한다.

class SignupBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    name: str = Field(max_length=100)


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class GuardianRequestBody(BaseModel):
    email: str = Field(max_length=254)


class DeviceBody(BaseModel):
    name: str = Field(max_length=100)
    kind: Literal["pi", "phone"]


class PushKeys(BaseModel):
    p256dh: str = Field(max_length=500)
    auth: str = Field(max_length=500)


class PushSubscribeBody(BaseModel):
    endpoint: str = Field(max_length=2000)
    keys: PushKeys


class PushUnsubscribeBody(BaseModel):
    endpoint: str = Field(max_length=2000)


# --- 공통 도우미 --------------------------------------------------------------

def _accounts(request: Request) -> Accounts:
    return request.app.state.accounts


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _user_view(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": user["id"], "email": user["email"], "name": user["name"]}


def _live(request: Request, name: str) -> Any:
    """실시간 구성 요소(guardian_hub, devices, events). 계정 라우터만 붙인 앱(계정 테스트)에는 없다."""
    return getattr(request.app.state, name, None)


def _http_error(exc: Exception) -> HTTPException:
    """Accounts의 예외를 HTTP 오류로 바꾼다 (auth.py 머리말의 오류 규칙)."""
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _start_session(request: Request, response: Response, user_id: int) -> None:
    settings = _settings(request)
    token = _accounts(request).create_session(user_id)
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.session_days * 24 * 3600,
        httponly=True,       # 페이지 스크립트가 토큰을 읽지 못하게
        samesite="lax",      # 다른 사이트에서 보낸 POST에는 쿠키가 붙지 않게
        secure=settings.cookie_secure,
        path="/",
    )


# --- 계정 ------------------------------------------------------------------

@router.post("/api/signup", status_code=201)
def signup(body: SignupBody, request: Request, response: Response):
    accounts = _accounts(request)
    try:
        user_id = accounts.create_user(body.email, body.password, body.name)
    except ValueError as exc:
        raise _http_error(exc)
    _start_session(request, response, user_id)
    return {"user": _user_view(accounts.get_user(user_id))}


@router.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    user = _accounts(request).authenticate(body.email, body.password)
    if user is None:
        # 없는 이메일과 틀린 비밀번호를 구분하지 않는다(가입 여부를 알려 주지 않으려고).
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다.")
    _start_session(request, response, user["id"])
    return {"user": _user_view(user)}


@router.post("/api/logout")
async def logout(request: Request, response: Response):
    # async def: 열린 보호자 화면(메모리 상태)을 이벤트 루프에서 닫기 위함이다.
    settings = _settings(request)
    token = request.cookies.get(settings.cookie_name)
    _accounts(request).delete_session(token)
    # 이 세션으로 열어 둔 보호자 화면이 로그아웃 뒤에도 보호 대상의 상태를 계속 받지 않게 닫는다.
    hub = _live(request, "guardian_hub")
    if hub is not None:
        await hub.close_session(token)
    response.delete_cookie(
        key=settings.cookie_name, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure
    )
    return {"ok": True}


@router.get("/api/me")
def me(request: Request, user: Dict[str, Any] = Depends(current_user)):
    accounts = _accounts(request)
    return {
        "user": _user_view(user),
        # 내가 등록한 보호자 (요청 중인 것도 상태와 함께 보여 준다)
        "guardians": accounts.guardian_links_of(user["id"]),
        # 내가 보호하는 사용자 (수락한 연결만)
        "wards": accounts.ward_links_of(user["id"], GUARDIAN_ACCEPTED),
        # 나에게 온 보호자 요청 (수락 대기)
        "requests": accounts.ward_links_of(user["id"], GUARDIAN_PENDING),
    }


# --- 보호자 연결 --------------------------------------------------------------

@router.post("/api/guardians", status_code=201)
def request_guardian(body: GuardianRequestBody, request: Request, user: Dict[str, Any] = Depends(current_user)):
    try:
        return _accounts(request).request_guardian(user["id"], body.email)
    except ValueError as exc:
        raise _http_error(exc)


@router.post("/api/guardians/{link_id}/accept")
def accept_guardian(link_id: int, request: Request, user: Dict[str, Any] = Depends(current_user)):
    try:
        return _accounts(request).accept_guardian(link_id, user["id"])
    except (LookupError, PermissionError) as exc:
        raise _http_error(exc)


@router.delete("/api/guardians/{link_id}")
def remove_guardian(link_id: int, request: Request, user: Dict[str, Any] = Depends(current_user)):
    try:
        _accounts(request).remove_guardian(link_id, user["id"])
    except (LookupError, PermissionError) as exc:
        raise _http_error(exc)
    return {"ok": True}


# --- 기기 ------------------------------------------------------------------

@router.get("/api/devices")
def list_devices(request: Request, user: Dict[str, Any] = Depends(current_user)):
    return {"devices": _accounts(request).devices_of([user["id"]])}


@router.post("/api/devices", status_code=201)
def create_device(body: DeviceBody, request: Request, user: Dict[str, Any] = Depends(current_user)):
    try:
        device, key = _accounts(request).create_device(user["id"], body.name, body.kind)
    except ValueError as exc:
        raise _http_error(exc)
    result: Dict[str, Any] = {"device": device}
    if key is not None:
        # Pi 기기 키는 지금 한 번만 보여 준다. 서버에는 해시만 남아 다시 알려 줄 수 없다.
        result["key"] = key
    return result


@router.delete("/api/devices/{device_id}")
async def delete_device(device_id: int, request: Request, user: Dict[str, Any] = Depends(current_user)):
    """기기 삭제. 기기 키가 유출되었을 때 키를 무효로 만드는 방법이기도 하다.

    async def: DB에서 지운 뒤 메모리 상태(접속 중인 연결, 진행 중 이벤트)도 이벤트 루프에서 정리한다.
    연결을 남겨 두면 지운 기기가 계속 영상을 보내고, 지울 수 없는 경보가 화면에 남는다.
    """
    try:
        _accounts(request).delete_device(device_id, user["id"])
    except (LookupError, PermissionError) as exc:
        raise _http_error(exc)
    events, devices = _live(request, "events"), _live(request, "devices")
    if events is not None:
        events.forget_device(device_id)
    if devices is not None:
        await devices.remove_device(device_id)
    return {"ok": True}


# --- 웹 푸시 ----------------------------------------------------------------

@router.get("/api/push/vapid-public-key")
def vapid_public_key(request: Request):
    settings = _settings(request)
    key: Optional[str] = settings.vapid_public_key if settings.push_enabled else None
    return {"key": key, "enabled": settings.push_enabled}


@router.post("/api/push/subscribe")
def push_subscribe(body: PushSubscribeBody, request: Request, user: Dict[str, Any] = Depends(current_user)):
    try:
        _accounts(request).save_push_subscription(user["id"], body.endpoint, body.keys.p256dh, body.keys.auth)
    except ValueError as exc:
        raise _http_error(exc)
    return {"ok": True}


@router.post("/api/push/unsubscribe")
def push_unsubscribe(body: PushUnsubscribeBody, request: Request, user: Dict[str, Any] = Depends(current_user)):
    # 다른 계정의 구독은 지우지 않는다.
    _accounts(request).delete_push_subscription(body.endpoint, user_id=user["id"])
    return {"ok": True}
