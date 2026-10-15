"""서버 설정.

값은 아래 순서로 정해진다(뒤가 앞을 덮어쓴다).
  1. 이 파일의 기본값
  2. server/config.toml (또는 환경변수 FALL_SERVER_CONFIG, 실행 인자 --config로 지정한 TOML 파일)
  3. 환경변수 (ENV_OVERRIDES)

config.toml은 저장소에 올리지 않는다(토큰이 들어 있다). 예시는 server/config.example.toml.
키 이름은 Settings의 항목 이름과 같다. 상대 경로는 저장소 최상위 기준이다.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

SERVER_DIR = Path(__file__).resolve().parent
ROOT_DIR = SERVER_DIR.parent
DEFAULT_CONFIG_PATH = SERVER_DIR / "config.toml"

PROMPT_TEXT = "괜찮으신가요? 괜찮으시면 '괜찮아요'라고 말씀하시거나 화면 아래 버튼을 눌러 주세요."

# 자유 인식에 더해 응답 문구로 제한한 인식(문법)을 한다. 단어는 Vosk 한국어 소형 모델(0.22)의 어휘
# 단위로 띄어 쓴다('괜찮아요'는 어휘에 없고 '괜찮 아요'로 있다). 모델을 바꾸면 어휘를 다시 확인한다.
VOICE_GRAMMAR = ["괜찮아", "괜찮 아요", "네 괜찮 아요", "괜찮 아", "일어났 어요", "문제 없어요",
                 "안 괜찮 아요", "안 괜찮아", "도 와 주세요", "도와줘", "살려 주세요", "살려줘",
                 "아파", "아파 요", "구급차", "구급차 불러 주세요", "여기", "네", "예", "응", "아니", "아니요"]
# 부정 표현은 '괜찮아'보다 먼저 검사한다 ("안 괜찮아요" -> 도움 요청).
KEYWORDS_NEGATED_SAFE = ["안괜찮", "안괜찬", "괜찮지않", "괜찮지안"]
KEYWORDS_HELP = ["도와", "살려", "아파", "아퍼", "여기야", "구급차", "119"]
KEYWORDS_SAFE = ["괜찮", "괜찬", "일어났", "문제없"]

# 환경변수 -> 설정 항목
ENV_OVERRIDES = {
    "DEVICE_TOKEN": "device_token",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
    "FALL_SERVER_HOST": "host",
    "FALL_SERVER_PORT": "port",
    "FALL_POSE_MODEL": "pose_model_path",
    "FALL_VOSK_MODEL": "vosk_model_path",
    "FALL_DB_PATH": "db_path",
}


class ConfigError(ValueError):
    pass


@dataclass
class Settings:
    # --- 서버 ---
    host: str = "0.0.0.0"
    port: int = 8000
    device_token: str = ""          # edge 접속 토큰. 비어 있으면 서버가 시작하지 않는다
    device_name: str = "거실"        # 설치 위치. 알림 메시지와 welcome에 쓴다
    log_level: str = "INFO"

    # --- 영상 (welcome으로 edge에 알려 준다) ---
    video_width: int = 640
    video_height: int = 480
    video_fps: int = 10
    jpeg_quality: int = 80

    # --- 자세 추정 ---
    pose_model_path: str = "assets/models/pose_landmarker_lite.task"
    keypoint_min_confidence: float = 0.5

    # --- 낙상 판정: UR Fall 공개 영상 평가로 정한 값 (docs/EVALUATION.md) ---
    descent_velocity_peak: float = 1.5      # 하강 속도(몸통 길이/초) 이 값 이상이면 하강 감지
    descent_window_seconds: float = 0.3     # 하강 속도를 재는 시간 구간
    torso_angle_lying: float = 60.0         # 수직선 대비 몸통 각도(도) 이상이면 누운 자세
    aspect_ratio_lying: float = 1.0         # 몸통이 일어섬 기준 이상 기울었고 종횡비가 이 값 이상이어도 누운 자세
    torso_angle_standing: float = 40.0      # 몸통 각도가 이 값 미만이면 일어선 자세
    lying_seconds: float = 2.0              # 하강 후 누운 자세가 이어져야 하는 시간
    falling_timeout_seconds: float = 5.0    # 하강 후 누움·일어섬 어느 쪽도 확정되지 않으면 정상 복귀
    standing_confirm_seconds: float = 0.5   # 하강 후 일어선 자세가 이만큼 이어져야 정상 복귀
    missing_counts_as_lying: bool = True    # 하강 후 사람이 안 보이면 누운 것과 같이 본다

    # --- 음성 확인 ---
    vosk_model_path: str = "assets/models/vosk-model-small-ko-0.22"
    listen_seconds: float = 5.0             # 한 번 물은 뒤 듣는 시간
    listen_grace_seconds: float = 3.0       # 마이크 소리가 늦게 와도 기다리는 여유 시간
    voice_max_attempts: int = 2             # 무응답·불분명이면 다시 묻는 것을 포함한 최대 횟수
    prompt_text: str = PROMPT_TEXT
    prompt_wav: str = ""                    # 녹음한 안내 음성 파일. 비어 있거나 없으면 합성 음성 -> 알림음
    tts_voice: str = ""                     # Windows 합성 음성 이름. 비어 있으면 한국어 음성을 찾는다
    playback_timeout_margin: float = 5.0    # 안내 음성 길이 + 이 시간 안에 playback_done이 없으면 장치 오류
    voice_grammar: List[str] = field(default_factory=lambda: list(VOICE_GRAMMAR))
    # 문법 인식은 엉뚱한 말도 가장 가까운 문구로 바꾸므로 모든 단어 신뢰도가 이 값 이상일 때만 쓴다.
    voice_grammar_min_conf: float = 0.9
    keywords_negated_safe: List[str] = field(default_factory=lambda: list(KEYWORDS_NEGATED_SAFE))
    keywords_help: List[str] = field(default_factory=lambda: list(KEYWORDS_HELP))
    keywords_safe: List[str] = field(default_factory=lambda: list(KEYWORDS_SAFE))
    store_voice_text: bool = False          # 인식 문장 원문 저장. 기본은 분류 결과만 저장

    # --- 보호자 알림 (텔레그램) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_timeout_seconds: float = 5.0
    notify_retry_delays: List[float] = field(default_factory=lambda: [30.0, 120.0, 600.0])
    notify_retry_check_seconds: float = 5.0

    # --- 장치 감시 ---
    device_alert_after_seconds: float = 180.0  # 장치 문제가 이만큼 이어지면 보호자에게 알림
    status_stale_seconds: float = 6.0          # 이 시간 동안 status가 없으면 상태 보고 끊김
    video_stale_seconds: float = 5.0           # 이 시간 동안 영상이 없으면 영상 끊김
    monitor_interval_seconds: float = 1.0

    # --- 기록 ---
    db_path: str = "data/server_events.db"
    event_retention_days: int = 0              # 0이면 자동 삭제하지 않는다

    def path(self, value: str) -> Path:
        """설정의 경로 값. 상대 경로는 저장소 최상위 기준."""
        p = Path(value).expanduser()
        return p if p.is_absolute() else ROOT_DIR / p

    @property
    def video(self) -> Dict[str, int]:
        return {"width": self.video_width, "height": self.video_height, "fps": self.video_fps,
                "jpeg_quality": self.jpeg_quality}

    @property
    def notifier_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def _coerce(name: str, value: Any, default: Any, source: str) -> Any:
    """설정 값을 기본값과 같은 형식으로 맞춘다. 맞출 수 없으면 ConfigError."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("1", "true", "yes", "0", "false", "no"):
            return value.lower() in ("1", "true", "yes")
    elif isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
    elif isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
    elif isinstance(default, str):
        if isinstance(value, str):
            return value
    elif isinstance(default, list):
        if isinstance(value, list):
            if default and isinstance(default[0], float):
                return [float(v) for v in value]
            return [str(v) for v in value]
    raise ConfigError(f"{source}: '{name}' 값의 형식이 맞지 않습니다: {value!r}")


def load_settings(config_path: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
                  **overrides: Any) -> Settings:
    """설정 파일과 환경변수를 읽어 Settings를 만든다. overrides는 실행 인자 등 마지막으로 덮어쓸 값."""
    env = os.environ if env is None else env
    settings = Settings()
    defaults = {f.name: getattr(settings, f.name) for f in fields(Settings)}

    path = Path(config_path) if config_path else Path(env.get("FALL_SERVER_CONFIG", DEFAULT_CONFIG_PATH))
    if config_path and not path.exists():
        raise ConfigError(f"설정 파일이 없습니다: {path}")
    if path.exists():
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: TOML 형식 오류: {e}") from e
        for key, value in data.items():
            if key not in defaults:
                raise ConfigError(f"{path}: 알 수 없는 설정 '{key}'")
            setattr(settings, key, _coerce(key, value, defaults[key], str(path)))

    for env_name, key in ENV_OVERRIDES.items():
        if env.get(env_name):
            setattr(settings, key, _coerce(key, env[env_name], defaults[key], f"환경변수 {env_name}"))

    for key, value in overrides.items():
        if value is None:
            continue
        if key not in defaults:
            raise ConfigError(f"알 수 없는 설정 '{key}'")
        setattr(settings, key, _coerce(key, value, defaults[key], "실행 인자"))

    if settings.voice_max_attempts < 1:
        raise ConfigError("voice_max_attempts는 1 이상이어야 합니다")
    if not settings.notify_retry_delays:
        raise ConfigError("notify_retry_delays가 비어 있습니다")
    return settings
