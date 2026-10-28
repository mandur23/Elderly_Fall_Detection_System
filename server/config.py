"""중앙 서버 설정.

값은 환경변수로 바꿀 수 있다. 판정 임계값과 대기 시간은 시험용 초기값이며,
테스트 결과를 보고 팀이 정한다(docs/REQUIREMENTS.md '확정 전 항목').
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").lower() in ("1", "true", "yes")


@dataclass
class Settings:
    # --- 저장 위치 ---
    data_dir: Path = field(default_factory=lambda: Path(_env("FALL_DATA_DIR", str(BASE_DIR / "data"))))
    db_filename: str = "server.db"

    # --- 서버 실행 ---
    host: str = field(default_factory=lambda: _env("FALL_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("FALL_PORT", "8443")))
    ssl_certfile: Optional[str] = field(default_factory=lambda: os.getenv("FALL_SSL_CERT"))
    ssl_keyfile: Optional[str] = field(default_factory=lambda: os.getenv("FALL_SSL_KEY"))
    web_dir: Path = field(default_factory=lambda: Path(_env("FALL_WEB_DIR", str(BASE_DIR / "web"))))
    # 관리자 페이지(/admin). 서버 PC 자신에서만 열린다(server/admin.py). 0으로 두면 아예 끈다.
    admin_enabled: bool = field(default_factory=lambda: _env_bool("FALL_ADMIN", True))

    # --- 계정 ---
    session_days: int = 14
    cookie_name: str = "session"
    # 휴대폰 브라우저는 HTTPS에서만 카메라를 쓰므로 기본은 Secure 쿠키다.
    cookie_secure: bool = field(default_factory=lambda: _env_bool("FALL_COOKIE_SECURE", True))

    # --- 자세 추정 ---
    # 'yolo' | 'fake'. 'fake'는 테스트 전용이며 프레임 바이트를 관절 좌표 JSON으로 읽는다.
    pose_backend: str = field(default_factory=lambda: _env("FALL_POSE_BACKEND", "yolo"))
    yolo_model_path: str = field(default_factory=lambda: _env("FALL_YOLO_MODEL", str(BASE_DIR / "models" / "yolov8n-pose.pt")))
    keypoint_min_confidence: float = 0.5

    # --- 낙상 판정 ---
    detection_backend: str = field(default_factory=lambda: _env("FALL_DETECTION_BACKEND", "random_forest"))
    rf_model_path: str = field(default_factory=lambda: _env("FALL_RF_MODEL", str(BASE_DIR / "models" / "fall_detector_window_yolo.pkl")))
    rf_threshold: float = field(default_factory=lambda: _env_float("FALL_RF_THRESHOLD", 0.40))
    # 하강 속도 단위: 몸통 길이/초. 카메라와의 거리에 덜 민감하도록 몸통 길이로 정규화한다.
    descent_velocity_peak: float = field(default_factory=lambda: _env_float("FALL_DESCENT_PEAK", 2.0))
    torso_angle_lying: float = 60.0      # 수직선 대비 몸통 각도(도) 이상이면 누운 자세
    aspect_ratio_lying: float = 1.0      # 바운딩 박스 가로/세로 이상이면 누운 자세
    torso_angle_standing: float = 40.0   # 이 각도 미만이고
    aspect_ratio_standing: float = 0.8   # 종횡비가 이 값 미만이면 다시 일어선 것으로 본다
    t1_lying_seconds: float = field(default_factory=lambda: _env_float("FALL_T1_SECONDS", 2.0))
    # T1을 재는 동안 누운 장면 사이에 '사람 없음'·'중간 자세'가 이 시간까지 끼어도 이어서 잰다(뒤척임, 화면 밖)
    lying_gap_seconds: float = 1.0
    falling_timeout_seconds: float = 5.0

    # --- 기기 상태 ---
    camera_stale_seconds: float = 5.0    # 이 시간 동안 프레임이 없으면 '카메라 응답 없음'

    # --- 음성 확인 ---
    # 'whisper' | 'fake'. 'fake'는 테스트 전용이며 녹음 바이트를 인식 문장(UTF-8)으로 읽는다.
    stt_backend: str = field(default_factory=lambda: _env("FALL_STT_BACKEND", "whisper"))
    whisper_model: str = field(default_factory=lambda: _env("FALL_WHISPER_MODEL", "small"))
    voice_max_attempts: int = 2
    # 음성 확인 요청 후 녹음이 올라오기까지 기다리는 시간(안내 재생 + 녹음 + 전송 여유)
    voice_reply_timeout_seconds: float = field(default_factory=lambda: _env_float("FALL_VOICE_TIMEOUT", 20.0))
    # 음성 확인 중 기기 연결이 끊겼을 때 다시 접속하기를 기다리는 시간. 그래도 없으면 '확인 불가' 경보.
    device_reconnect_grace_seconds: float = 3.0
    store_voice_text: bool = False
    # 부정 표현은 '괜찮아'보다 먼저 검사한다 ("안 괜찮아요" -> 도움 요청).
    keywords_negated_safe: List[str] = field(default_factory=lambda: ["안괜찮", "안괜찬", "괜찮지않", "괜찮지안"])
    keywords_help: List[str] = field(default_factory=lambda: ["도와", "살려", "아파", "아퍼", "여기야", "구급차", "119"])
    keywords_safe: List[str] = field(default_factory=lambda: ["괜찮", "괜찬", "일어났", "문제없"])

    # --- 웹 푸시 ---
    vapid_private_key: Optional[str] = field(default_factory=lambda: os.getenv("FALL_VAPID_PRIVATE_KEY"))
    vapid_public_key: Optional[str] = field(default_factory=lambda: os.getenv("FALL_VAPID_PUBLIC_KEY"))
    vapid_subject: str = field(default_factory=lambda: _env("FALL_VAPID_SUBJECT", "mailto:admin@example.com"))
    push_retry_delays: List[float] = field(default_factory=lambda: [10.0, 60.0])

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def https_enabled(self) -> bool:
        return bool(self.ssl_certfile and self.ssl_keyfile)

    @property
    def push_enabled(self) -> bool:
        return bool(self.vapid_private_key and self.vapid_public_key)
