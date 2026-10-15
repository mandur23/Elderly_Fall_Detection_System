"""라즈베리파이(edge)와 PC 서버(server) 사이 WebSocket 통신 규약.

양쪽은 이 파일과 docs/PROTOCOL.md만 공유한다. 바꿀 때는 PROTOCOL_VERSION을 올리고 양쪽을 함께 고친다.
표준 라이브러리만 쓴다(라즈베리파이에 그대로 복사해 쓸 수 있게).

연결
  edge가 서버에 접속한다: ws://<서버 주소>:8000/ws/device
  핸드셰이크 헤더 Authorization: Bearer <DEVICE_TOKEN> (틀리면 서버가 HTTP 401로 거절)

바이너리 메시지 = [종류 1바이트][시각 8바이트 float64 big-endian][내용]
  VIDEO     edge -> server  JPEG 한 장. 시각은 edge의 촬영 시각(time.monotonic(), 초)
  AUDIO_IN  edge -> server  마이크 PCM(s16le, mono, 16kHz), 약 0.1초 단위. 시각은 첫 샘플의 녹음 시각
  AUDIO_OUT server -> edge  스피커로 재생할 PCM(같은 형식). 시각은 쓰지 않는다(0)

텍스트 메시지 = JSON {"type": ..., ...}. 종류별 필수 항목은 REQUIRED 참고.
"""

import json
import struct
from typing import Any, Dict, Tuple

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8000
WS_PATH = "/ws/device"
KIOSK_PATH = "/kiosk"          # 서버가 제공하는 현장 화면. edge의 Chromium이 연다
HEALTH_PATH = "/api/health"    # 서버가 떠 있으면 200. edge가 현장 화면을 열기 전에 기다릴 때 쓴다
AUTH_HEADER = "Authorization"  # 값: "Bearer <DEVICE_TOKEN>"

# --- 바이너리 메시지 ---
VIDEO = 0x01
AUDIO_IN = 0x02
AUDIO_OUT = 0x03
BINARY_KINDS = (VIDEO, AUDIO_IN, AUDIO_OUT)

AUDIO_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_BYTES = 2
AUDIO_CHUNK_SECONDS = 0.1
AUDIO_CHUNK_BYTES = int(AUDIO_RATE * AUDIO_CHUNK_SECONDS) * AUDIO_SAMPLE_BYTES * AUDIO_CHANNELS

_HEADER = struct.Struct("!Bd")
HEADER_SIZE = _HEADER.size

# 영상 기본값. 서버가 WELCOME·CONFIG로 바꿀 수 있다.
DEFAULT_VIDEO = {"width": 640, "height": 480, "fps": 10, "jpeg_quality": 80}

STATUS_INTERVAL_SECONDS = 2.0  # edge가 STATUS를 보내는 간격
HEARTBEAT_SECONDS = 10.0       # WebSocket ping 간격 (양쪽)

# --- JSON 제어 메시지 ---
# edge -> server
HELLO = "hello"                  # 접속 직후 한 번
STATUS = "status"                # STATUS_INTERVAL_SECONDS마다 장치 상태
PLAYBACK_DONE = "playback_done"  # play_begin으로 시작한 재생이 끝남(또는 중단됨)
# server -> edge
WELCOME = "welcome"              # hello 응답. edge는 받은 영상 설정을 적용한 뒤 영상 전송을 시작한다
CONFIG = "config"                # 접속 중 영상 설정 변경
MIC = "mic"                      # 마이크 전송 켜기/끄기 (기본 꺼짐)
PLAY_BEGIN = "play_begin"        # 이어서 오는 AUDIO_OUT을 이 id로 재생. loop=true면 play_stop까지 반복
PLAY_END = "play_end"            # 이 id의 AUDIO_OUT을 다 보냄
PLAY_STOP = "play_stop"          # 재생 즉시 중단, 받은 소리 버림
# 양쪽
ERROR = "error"

REQUIRED: Dict[str, Tuple[str, ...]] = {
    HELLO: ("protocol", "device_id", "video", "audio"),
    STATUS: ("camera_ok", "mic_ok", "speaker_ok"),
    PLAYBACK_DONE: ("id",),
    WELCOME: ("protocol", "video"),
    CONFIG: ("video",),
    MIC: ("enabled",),
    PLAY_BEGIN: ("id",),
    PLAY_END: ("id",),
    PLAY_STOP: (),
    ERROR: ("message",),
}
EDGE_TO_SERVER = frozenset({HELLO, STATUS, PLAYBACK_DONE, ERROR})
SERVER_TO_EDGE = frozenset({WELCOME, CONFIG, MIC, PLAY_BEGIN, PLAY_END, PLAY_STOP, ERROR})

# WebSocket 닫기 코드
CLOSE_PROTOCOL_ERROR = 4400   # 버전 불일치, 잘못된 메시지
CLOSE_REPLACED = 4409         # 같은 device_id가 다시 접속해 이전 연결을 닫음


class ProtocolError(ValueError):
    pass


def pack(kind: int, timestamp: float, payload: bytes) -> bytes:
    if kind not in BINARY_KINDS:
        raise ProtocolError(f"알 수 없는 바이너리 종류: {kind}")
    return _HEADER.pack(kind, timestamp) + payload


def unpack(data: bytes) -> Tuple[int, float, bytes]:
    """(종류, 시각, 내용). 형식이 틀리면 ProtocolError."""
    if len(data) < HEADER_SIZE:
        raise ProtocolError(f"바이너리 메시지가 너무 짧습니다 ({len(data)}바이트)")
    kind, timestamp = _HEADER.unpack_from(data)
    if kind not in BINARY_KINDS:
        raise ProtocolError(f"알 수 없는 바이너리 종류: {kind}")
    return kind, timestamp, bytes(data[HEADER_SIZE:])


def encode(type_: str, **fields: Any) -> str:
    """JSON 제어 메시지를 만든다. 필수 항목이 빠지면 ProtocolError."""
    _check(type_, fields)
    return json.dumps({"type": type_, **fields}, ensure_ascii=False)


def decode(text: str) -> Dict[str, Any]:
    """JSON 제어 메시지를 읽는다. 형식이 틀리거나 필수 항목이 없으면 ProtocolError."""
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"JSON이 아닙니다: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError("JSON 객체가 아닙니다")
    _check(msg.get("type"), msg)
    return msg


def _check(type_: Any, fields: Dict[str, Any]) -> None:
    if type_ not in REQUIRED:
        raise ProtocolError(f"알 수 없는 메시지 종류: {type_!r}")
    missing = [k for k in REQUIRED[type_] if k not in fields]
    if missing:
        raise ProtocolError(f"{type_} 메시지에 필수 항목이 없습니다: {', '.join(missing)}")


def bearer(token: str) -> Dict[str, str]:
    """핸드셰이크 헤더."""
    return {AUTH_HEADER: f"Bearer {token}"}


def token_from_header(value: str) -> str:
    """'Bearer <토큰>'에서 토큰을 꺼낸다. 형식이 틀리면 빈 문자열."""
    scheme, _, token = (value or "").partition(" ")
    return token.strip() if scheme == "Bearer" else ""
