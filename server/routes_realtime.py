"""실시간 API: 기기 카메라 WebSocket, 보호자 화면 WebSocket, 이벤트(음성 업로드·해제·기록), 기기 상태.

메모리 상태(기기 세션, 진행 중 이벤트)는 이벤트 루프 한 곳에서만 바꾸므로 여기 핸들러는 모두
async def로 둔다(def로 두면 FastAPI가 스레드에서 실행한다).

WebSocket 거부 코드: 4400 잘못된 요청, 4401 인증 실패, 4403 권한 없음·다른 출처, 4409 새 연결로 교체됨.
브라우저는 HTTP 오류 코드를 볼 수 없으므로 일단 접속을 받은 뒤 이 코드로 닫는다.
클라이언트는 4401/4403이면 재연결하지 말고 로그인·기기 키를 확인하게 안내한다.
"""

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from starlette.datastructures import FormData, UploadFile

from server.auth import check_origin, current_user, device_from_request, optional_user, user_from_websocket

log = logging.getLogger("server.routes_realtime")

router = APIRouter()

CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
# 640x480 JPEG(품질 70)는 보통 50KB 안팎이다. 이보다 훨씬 큰 메시지는 잘못 보낸 것으로 보고 버린다.
MAX_FRAME_BYTES = 2 * 1024 * 1024
# 몇 초짜리 응답 녹음(wav 16kHz 모노 약 32KB/초)에 넉넉한 상한
MAX_AUDIO_BYTES = 10 * 1024 * 1024
# multipart 머리글·다른 필드(error, attempt)에 주는 여유
MAX_VOICE_BODY_BYTES = MAX_AUDIO_BYTES + 64 * 1024
# 열린 보호자 화면의 로그인 세션을 이 간격으로 다시 확인한다(세션 만료·삭제 반영).
SESSION_RECHECK_SECONDS = 60.0


def _state(conn: Any):
    return conn.app.state


async def _reject(ws: WebSocket, code: int, reason: str) -> None:
    await ws.accept()
    await ws.close(code=code, reason=reason)


# ---------------------------------------------------------------------------
# 기기 카메라
# ---------------------------------------------------------------------------

def _authenticate_camera(ws: WebSocket, device_id: int) -> Tuple[Optional[Dict[str, Any]], int, str]:
    """(기기, 거부 코드, 사유). Pi는 기기 키, 휴대폰은 주인의 로그인 세션으로 인증한다."""
    device = _state(ws).accounts.get_device(device_id)
    if device is None:
        return None, CLOSE_UNAUTHORIZED, "unknown device"
    if device["kind"] == "pi":
        if device_from_request(ws, device_id) is None:
            return None, CLOSE_UNAUTHORIZED, "bad device key"
        return device, 0, ""
    # 휴대폰: 쿠키로 인증하므로 다른 사이트에서 연 접속을 막는다.
    if not check_origin(ws):
        return None, CLOSE_FORBIDDEN, "origin"
    user = user_from_websocket(ws)
    if user is None:
        return None, CLOSE_UNAUTHORIZED, "login required"
    if user["id"] != device["owner_id"]:
        return None, CLOSE_FORBIDDEN, "not owner"
    return device, 0, ""


@router.websocket("/ws/camera")
async def camera_socket(ws: WebSocket):
    try:
        device_id = int(ws.query_params.get("device_id", ""))
    except ValueError:
        await _reject(ws, CLOSE_BAD_REQUEST, "device_id required")
        return
    device, code, reason = _authenticate_camera(ws, device_id)
    if device is None:
        log.info("카메라 접속 거부: device=%s (%s)", device_id, reason)
        await _reject(ws, code, reason)
        return

    await ws.accept()
    devices, events = _state(ws).devices, _state(ws).events
    session = await devices.connect(device, ws)
    try:
        await ws.send_json({"type": "hello", "device_id": device_id, "name": device["name"], "kind": device["kind"]})
        await ws.send_json({"type": "state", "state": devices.status(device_id)["state"]})
        # 음성 확인 도중 다시 접속했으면(화면 새로 고침 등) 요청을 다시 보낸다.
        pending = events.pending_voice_check(device_id)
        if pending is not None:
            await ws.send_json(pending)

        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                if len(data) > MAX_FRAME_BYTES:
                    log.warning("기기 %s: 너무 큰 프레임(%d바이트) 버림", device_id, len(data))
                    continue
                devices.submit_frame(session, data)
                continue
            text = message.get("text")
            if text:
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                # 지금은 연결 유지용 ping만 있다. 응답하지 않아도 된다.
                if isinstance(payload, dict) and payload.get("type") != "ping":
                    log.debug("기기 %s: 알 수 없는 메시지 %s", device_id, payload.get("type"))
    except Exception as e:  # 연결이 갑자기 끊긴 경우 등
        log.info("기기 %s 연결 오류: %s", device_id, type(e).__name__)
    finally:
        await devices.disconnect(session)


# ---------------------------------------------------------------------------
# 보호자 화면
# ---------------------------------------------------------------------------

@router.websocket("/ws/guardian")
async def guardian_socket(ws: WebSocket):
    if not check_origin(ws):
        await _reject(ws, CLOSE_FORBIDDEN, "origin")
        return
    user = user_from_websocket(ws)
    if user is None:
        await _reject(ws, CLOSE_UNAUTHORIZED, "login required")
        return

    await ws.accept()
    state = _state(ws)
    owner_ids = state.accounts.visible_owner_ids(user["id"])
    snapshot = {
        "type": "snapshot",
        "devices": state.devices.statuses_for(owner_ids),
        "active_events": state.events.active_events_visible_to(user),
    }
    # snapshot을 만든 뒤 기다림 없이 등록하고 바로 보낸다. 그래야 그 사이 생긴 변경을 놓치지 않고,
    # 변경 메시지가 snapshot보다 먼저 도착하지도 않는다.
    # 세션 토큰도 함께 등록해 로그아웃하면 이 화면을 바로 닫게 한다(GuardianHub.close_session).
    state.guardian_hub.register(user["id"], ws, session_token=ws.cookies.get(state.settings.cookie_name))
    try:
        await ws.send_json(snapshot)
        while True:
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=SESSION_RECHECK_SECONDS)
            except asyncio.TimeoutError:
                # 세션이 만료되었거나 지워졌으면 더는 보호 대상의 상태를 보내지 않는다.
                if user_from_websocket(ws) is None:
                    log.info("보호자 화면 세션 만료로 연결을 닫음 (user=%s)", user["id"])
                    await ws.close(code=CLOSE_UNAUTHORIZED, reason="session expired")
                    break
                continue
            if message["type"] == "websocket.disconnect":
                break
            # 화면이 보내는 ping은 무시한다(연결 유지용).
    except Exception as e:
        log.info("보호자 화면 연결 오류 (user=%s): %s", user["id"], type(e).__name__)
    finally:
        state.guardian_hub.unregister(user["id"], ws)


# ---------------------------------------------------------------------------
# 이벤트
# ---------------------------------------------------------------------------

def _event_or_404(request: Request, event_id: int) -> Dict[str, Any]:
    row = _state(request).events.get_row(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="이벤트를 찾을 수 없습니다.")
    return row


def _actor(request: Request, device_id: int) -> Tuple[str, Dict[str, Any]]:
    """기기 키 또는 로그인한 사용자로 요청자를 정한다. 둘 다 없으면 401.

    맞는 기기 키가 있으면 기기로 본다. 같은 브라우저에 로그인 쿠키가 남아 있어도(예: 주인 계정으로
    로그인한 채 연 현장 화면) 기기 자격으로 처리되게 하기 위함이다.
    """
    device = device_from_request(request, device_id)
    if device is not None:
        return "device", device
    user = optional_user(request)
    if user is not None:
        return "user", user
    raise HTTPException(status_code=401, detail="로그인 또는 기기 키가 필요합니다.")


def _too_large() -> HTTPException:
    return HTTPException(status_code=413, detail="녹음 파일이 너무 큽니다.")


async def _read_voice_form(request: Request) -> FormData:
    """multipart 본문을 크기 상한을 지키며 읽는다.

    FastAPI의 File()/Form() 매개변수를 쓰면 인증 확인 전에 본문 전체를 임시 파일로 받아 버린다.
    그러면 로그인도 기기 키도 없는 요청이 큰 파일로 디스크를 채울 수 있으므로, 인증한 뒤에
    Content-Length와 실제로 받은 바이트 수를 함께 확인하며 직접 읽는다.
    """
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_VOICE_BODY_BYTES:
        raise _too_large()
    received = 0
    original_receive = request.receive

    async def capped_receive():
        nonlocal received
        message = await original_receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > MAX_VOICE_BODY_BYTES:
                raise _too_large()  # Content-Length 없이(chunked) 보내도 상한을 넘으면 멈춘다
        return message

    try:
        return await Request(request.scope, capped_receive).form(max_files=1, max_fields=5)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="multipart 형식의 녹음 업로드가 아닙니다.")


@router.post("/api/events/{event_id}/voice")
async def upload_voice(event_id: int, request: Request):
    """음성 확인 응답 녹음 (multipart: 파일 audio, 선택 필드 error·attempt).

    녹음·마이크에 실패한 기기는 audio 없이 error만 보낸다(확인 불가 -> 경보).
    이벤트와 요청자를 먼저 확인하고 본문은 그 뒤에 읽는다(_read_voice_form).
    """
    row = _event_or_404(request, event_id)
    kind, who = _actor(request, row["device_id"])
    device = _state(request).accounts.get_device(row["device_id"])
    if kind == "user":
        # 휴대폰 카메라 화면은 로그인 세션으로 올린다. 주인 본인의 휴대폰 기기만 허용한다.
        if device is None or device["kind"] != "phone" or device["owner_id"] != who["id"]:
            raise HTTPException(status_code=403, detail="이 기기의 주인만 녹음을 올릴 수 있습니다.")
    else:
        device = who

    form = await _read_voice_form(request)
    try:
        audio = form.get("audio")
        error = form.get("error") or None
        attempt_text = form.get("attempt")
        if not isinstance(audio, UploadFile):
            audio = None
        if error is not None and not isinstance(error, str):
            raise HTTPException(status_code=400, detail="error는 문자열이어야 합니다.")
        attempt: Optional[int] = None
        if attempt_text not in (None, ""):
            if not isinstance(attempt_text, str) or not attempt_text.isdigit():
                raise HTTPException(status_code=400, detail="attempt는 숫자여야 합니다.")
            attempt = int(attempt_text)
        if audio is None and not error:
            raise HTTPException(status_code=400, detail="녹음 파일(audio) 또는 오류(error)가 필요합니다.")

        data, filename = b"", ""
        if audio is not None:
            data = await audio.read(MAX_AUDIO_BYTES + 1)
            filename = audio.filename or ""
            if len(data) > MAX_AUDIO_BYTES:
                raise _too_large()
    finally:
        await form.close()
    accepted = _state(request).events.submit_voice(event_id, device, data, filename, error=error, attempt=attempt)
    if not accepted:
        # 이미 해제되었거나 시간이 지난 녹음. 기기 쪽에서 할 일은 없으므로 오류로 보내지 않는다.
        log.info("이벤트 %s: 기다리는 음성 확인이 없어 녹음을 버림", event_id)
    return {"accepted": accepted}


@router.get("/api/events")
async def list_events(request: Request, limit: int = Query(50, ge=1, le=200)):
    user = current_user(request)
    return {"events": _state(request).events.recent_events_visible_to(user, limit)}


@router.post("/api/events/{event_id}/clear")
async def clear_event(event_id: int, request: Request):
    """'괜찮아요' 버튼(현장 화면·카메라 화면) 또는 보호자의 경보 종료."""
    row = _event_or_404(request, event_id)
    actor = _actor(request, row["device_id"])
    events = _state(request).events
    try:
        cleared = await events.clear(event_id, actor)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not cleared:
        raise HTTPException(status_code=409, detail="이미 끝난 이벤트입니다.")
    return {"ok": True, "event": events.public(events.get_row(event_id))}


# ---------------------------------------------------------------------------
# 기기 상태 (현장 화면)
# ---------------------------------------------------------------------------

@router.get("/api/devices/{device_id}/state")
async def device_state(device_id: int, request: Request):
    """현장 화면(키오스크)은 기기 키로, 웹은 주인·보호자 로그인으로 읽는다."""
    state = _state(request)
    kind, who = _actor(request, device_id)
    device = state.accounts.get_device(device_id)
    if kind == "user":
        if device is None or not state.events.can_view(who["id"], device["owner_id"]):
            raise HTTPException(status_code=403, detail="볼 권한이 없는 기기입니다.")
    elif device is None:
        raise HTTPException(status_code=404, detail="기기를 찾을 수 없습니다.")
    active = state.events.active_for_device(device_id)
    active_row = state.events.get_row(active.id) if active is not None else None
    return {
        "device": state.devices.status(device_id, device),
        "active_event": state.events.public(active_row) if active_row is not None else None,
    }
