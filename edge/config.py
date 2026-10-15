"""edge 설정: edge/config.toml(저장소에 올리지 않음) + 명령행 인자.

명령행 인자가 설정 파일보다 우선한다. 설정 파일 형식은 edge/config.example.toml 참고.
"""

import argparse
import socket
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from shared import protocol

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")
CAMERA_BACKENDS = ("auto", "picamera2", "opencv", "none")
SPEAKER_BACKENDS = ("sounddevice", "null")
ROTATIONS = (0, 90, 180, 270)

Source = Union[int, str, None]   # 웹캠 번호, 영상 파일 경로, None(기본 카메라)
Device = Union[int, str, None]   # 오디오 장치 번호 또는 이름 일부, None(기본 장치)


class ConfigError(ValueError):
    pass


@dataclass
class EdgeConfig:
    server: str = ""
    token: str = ""
    device_id: str = field(default_factory=socket.gethostname)
    name: str = ""
    camera: str = "auto"
    source: Source = None
    rotation: int = 0
    loop_file: bool = True
    mic: bool = True
    mic_device: Device = None
    speaker: str = "sounddevice"
    speaker_device: Device = None
    volume: float = 1.0
    log_level: str = "INFO"

    @property
    def ws_url(self) -> str:
        return server_urls(self.server)[0]

    @property
    def http_url(self) -> str:
        return server_urls(self.server)[1]

    def check(self) -> None:
        """실행에 필요한 값이 맞는지 본다. 틀리면 ConfigError."""
        server_urls(self.server)
        if not self.token:
            raise ConfigError("장치 토큰이 없습니다. config.toml의 [server] token 또는 --token으로 지정하세요")
        if not self.device_id:
            raise ConfigError("device_id가 비어 있습니다")
        if self.camera not in CAMERA_BACKENDS:
            raise ConfigError(f"camera는 {', '.join(CAMERA_BACKENDS)} 중 하나여야 합니다: {self.camera!r}")
        if self.speaker not in SPEAKER_BACKENDS:
            raise ConfigError(f"speaker는 {', '.join(SPEAKER_BACKENDS)} 중 하나여야 합니다: {self.speaker!r}")
        if self.rotation not in ROTATIONS:
            raise ConfigError(f"rotation은 0, 90, 180, 270 중 하나여야 합니다: {self.rotation!r}")
        if not 0.0 <= self.volume <= 1.0:
            raise ConfigError(f"volume은 0.0~1.0이어야 합니다: {self.volume!r}")


def server_urls(server: str) -> Tuple[str, str]:
    """서버 주소를 (WebSocket URL, HTTP 기본 URL)로 바꾼다.

    "192.168.0.10", "192.168.0.10:8000", "http://192.168.0.10:8000", "ws://192.168.0.10:8000/ws/device"
    모두 받는다. 포트가 없으면 8000, ws 경로가 없으면 /ws/device.
    """
    text = (server or "").strip()
    if not text:
        raise ConfigError("서버 주소가 없습니다. config.toml의 [server] address 또는 --server로 지정하세요")
    if "://" not in text:
        text = "ws://" + text
    parts = urllib.parse.urlsplit(text)
    secure = {"ws": False, "http": False, "wss": True, "https": True}.get(parts.scheme)
    if secure is None or not parts.hostname:
        raise ConfigError(f"서버 주소 형식이 틀립니다: {server!r}")
    try:
        port = parts.port or protocol.DEFAULT_PORT
    except ValueError as e:
        raise ConfigError(f"서버 포트가 틀립니다: {server!r}") from e
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{host}:{port}"
    path = parts.path if parts.scheme in ("ws", "wss") and parts.path not in ("", "/") else protocol.WS_PATH
    ws = urllib.parse.urlunsplit(("wss" if secure else "ws", netloc, path, "", ""))
    http = urllib.parse.urlunsplit(("https" if secure else "http", netloc, "", "", ""))
    return ws, http


def parse_source(value: Any) -> Source:
    """'0' -> 0(웹캠 번호), 그 밖의 문자열 -> 영상 파일 경로, ''/None -> None."""
    if value is None or isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def parse_device(value: Any) -> Device:
    """오디오 장치: 번호, 이름 일부, 또는 None(기본 장치)."""
    return parse_source(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m edge",
        description="낙상 감지 edge: 카메라·마이크를 PC 서버로 보내고, 서버가 보낸 소리를 재생합니다.",
    )
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                   help="설정 파일 (기본: edge/config.toml, 없으면 기본값)")
    p.add_argument("--server", help="서버 주소 (예: 192.168.0.10, 192.168.0.10:8000, ws://.../ws/device)")
    p.add_argument("--token", help="장치 토큰 (설정 파일에 두는 것을 권장)")
    p.add_argument("--device-id", help="장치 구분자 (기본: 컴퓨터 이름)")
    p.add_argument("--name", help="설치 위치 이름 (예: 거실)")
    p.add_argument("--camera", choices=CAMERA_BACKENDS, help="카메라 방식 (기본 auto)")
    p.add_argument("--source", help="웹캠 번호 또는 영상 파일 경로")
    p.add_argument("--rotation", type=int, choices=ROTATIONS, help="영상 회전(시계 방향, 도)")
    p.add_argument("--no-loop", action="store_true", help="영상 파일이 끝나면 반복하지 않음")
    p.add_argument("--no-mic", action="store_true", help="마이크를 쓰지 않음")
    p.add_argument("--mic-device", help="마이크 장치 번호 또는 이름 일부 (--list-devices 참고)")
    p.add_argument("--speaker", choices=SPEAKER_BACKENDS,
                   help="스피커 방식. null이면 소리를 내지 않고 재생한 것처럼 처리 (시험용)")
    p.add_argument("--speaker-device", help="스피커 장치 번호 또는 이름 일부")
    p.add_argument("--volume", type=float, help="재생 음량 0.0~1.0 (기본 1.0)")
    p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), help="로그 수준")
    p.add_argument("--list-devices", action="store_true", help="오디오 장치 목록을 보여 주고 끝냄")
    p.add_argument("--print-url", choices=("ws", "http", "kiosk", "health"),
                   help="설정된 서버 URL을 출력하고 끝냄 (키오스크 스크립트용)")
    return p


def load_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다 ({path}): {e}") from e


def from_file(data: Dict[str, Any]) -> EdgeConfig:
    cfg = EdgeConfig()
    server, device = data.get("server", {}), data.get("device", {})
    camera, mic, speaker = data.get("camera", {}), data.get("microphone", {}), data.get("speaker", {})
    cfg.server = str(server.get("address", cfg.server))
    cfg.token = str(server.get("token", cfg.token))
    cfg.device_id = str(device.get("id") or cfg.device_id)
    cfg.name = str(device.get("name", cfg.name))
    cfg.camera = str(camera.get("backend", cfg.camera))
    cfg.source = parse_source(camera.get("source", cfg.source))
    cfg.rotation = int(camera.get("rotation", cfg.rotation))
    cfg.loop_file = bool(camera.get("loop_file", cfg.loop_file))
    cfg.mic = bool(mic.get("enabled", cfg.mic))
    cfg.mic_device = parse_device(mic.get("device", cfg.mic_device))
    cfg.speaker = str(speaker.get("backend", cfg.speaker))
    cfg.speaker_device = parse_device(speaker.get("device", cfg.speaker_device))
    cfg.volume = float(speaker.get("volume", cfg.volume))
    cfg.log_level = str(data.get("log", {}).get("level", cfg.log_level)).upper()
    return cfg


def load(args: argparse.Namespace) -> EdgeConfig:
    """설정 파일을 읽고 명령행 인자로 덮어쓴다. 값 검사는 EdgeConfig.check()."""
    cfg = from_file(load_file(args.config))
    overrides: Dict[str, Optional[Any]] = {
        "server": args.server,
        "token": args.token,
        "device_id": args.device_id,
        "name": args.name,
        "camera": args.camera,
        "rotation": args.rotation,
        "speaker": args.speaker,
        "volume": args.volume,
        "log_level": args.log_level,
    }
    for key, value in overrides.items():
        if value is not None:
            setattr(cfg, key, value)
    if args.source is not None:
        cfg.source = parse_source(args.source)
    if args.mic_device is not None:
        cfg.mic_device = parse_device(args.mic_device)
    if args.speaker_device is not None:
        cfg.speaker_device = parse_device(args.speaker_device)
    if args.no_loop:
        cfg.loop_file = False
    if args.no_mic:
        cfg.mic = False
    return cfg
