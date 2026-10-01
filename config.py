"""시스템 전역 설정.

아래 임계값과 대기 시간은 시험용 초기값이다. 실제 값은 테스트 결과를 보고
팀이 합의해 정한다(docs/REQUIREMENTS.md '확정 전 항목' 참고).

기기별 값은 코드를 고치지 말고 `config.local.toml`(또는 환경변수 FALL_CONFIG로
지정한 TOML 파일)에 적는다. 키 이름은 이 파일의 변수 이름과 같다. 예:

    CAMERA_BACKEND = "picamera2"
    TELEGRAM_CHAT_ID = "-1001234567890"
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- 경로 ---
DATA_DIR = BASE_DIR / "data"
ASSETS_DIR = BASE_DIR / "assets"

# --- 이벤트 기록 (SQLite) ---
# 음성 인식 문장 원문 저장 여부. 기본은 분류 결과(SAFE/HELP 등)만 저장한다.
STORE_VOICE_TEXT = False
# 이벤트 보관 기간(일). None이면 자동 삭제하지 않는다. 보관 정책 확정 후 설정.
EVENT_RETENTION_DAYS = None

# --- 로컬 API 서버 (외부 접근 차단을 위해 루프백에만 바인딩) ---
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000
# L3 -> L4 내부 API 공유 키. 비어 있으면 검사하지 않는다(개발용). 설치 스크립트가 생성한다.
INTERNAL_API_TOKEN = ""

# --- 영상 입력 (L3) ---
# 'auto' | 'picamera2' | 'opencv'. auto는 Pi 카메라 모듈을 먼저 시도하고 없으면 USB 카메라를 쓴다.
CAMERA_BACKEND = "auto"
CAMERA_INDEX = 0
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
TARGET_FPS = 10
CAMERA_ROTATION = 0  # 0, 90, 180, 270 (카메라를 돌려 설치한 경우)

# --- 자세 추정 ---
# 'mediapipe' | 'yolo' | 'mock'. 'mock'은 데모·테스트 전용이며 실제 감지를 하지 않는다.
# yolo(Ultralytics)는 AGPL-3.0이므로 상업 배포 시 라이선스 검토가 필요하다.
POSE_MODEL = "mediapipe"
MEDIAPIPE_MODEL_PATH = None  # None이면 assets/models/pose_landmarker_lite.task
YOLO_MODEL_PATH = "yolov8n-pose.pt"
KEYPOINT_MIN_CONFIDENCE = 0.5

# --- 낙상 판정 (L3 FSM) ---
# 하강 속도 단위: 몸통 길이/초 (엉덩이 중심 y 변화량을 몸통 길이로 나눈 값).
DESCENT_VELOCITY_PEAK = 2.0
TORSO_ANGLE_LYING = 60.0      # 수직선 대비 몸통 각도(도) 이상이면 누운 자세
ASPECT_RATIO_LYING = 1.0      # 몸통이 TORSO_ANGLE_STANDING 이상 기울었고 종횡비가 이 값 이상이어도 누운 자세
TORSO_ANGLE_STANDING = 40.0   # 몸통 각도가 이 값 미만이면 상체를 세운 것(일어섬)으로 본다
T1_LYING_SECONDS = 2.0        # 하강 후 누운 자세가 연속으로 유지되어야 하는 시간
FALLING_TIMEOUT_SECONDS = 5.0 # 하강 후 누움·기립 어느 쪽도 확정되지 않으면 정상 복귀

# --- L3 -> L4 보고 ---
HEARTBEAT_INTERVAL_SECONDS = 1.0
HEARTBEAT_STALE_SECONDS = 5.0  # 이 시간 동안 하트비트가 없으면 감지 프로세스 이상으로 표시

# --- 장치 이상 알림 ---
# 감지가 멈춘 상태(프로세스 무응답, 카메라·모델·마이크 오류)가 이 시간 이상 이어지면 보호자에게 알린다.
DEVICE_ALERT_AFTER_SECONDS = 180
# 매일 이 시각(HH:MM)에 '정상 동작 중' 메시지를 보낸다. None이면 보내지 않는다.
DAILY_REPORT_TIME = None

# --- 음성 확인 (L4) ---
# 'vosk' | 'simulated'. 'simulated'는 데모·테스트 전용.
VOICE_BACKEND = "vosk"
VOSK_MODEL_PATH = None  # None이면 data/vosk-model-small-ko
VOICE_SAMPLE_RATE = 16000
MIC_DEVICE = None              # sounddevice 입력 장치 번호/이름. None이면 기본 장치
VOICE_LISTEN_SECONDS = 5.0     # 한 번 질문 후 응답을 듣는 시간
VOICE_MAX_ATTEMPTS = 2         # 무응답·불분명 시 재질문 포함 최대 시도 횟수
PROMPT_TEXT = "괜찮으신가요? 괜찮으시면 '괜찮아요'라고 말씀하시거나 화면 아래 버튼을 눌러 주세요."

# 부정 표현은 '괜찮아'보다 먼저 검사한다 ("안 괜찮아요" -> 도움 요청).
KEYWORDS_NEGATED_SAFE = ["안괜찮", "안괜찬", "괜찮지않", "괜찮지안"]
KEYWORDS_HELP = ["도와", "살려", "아파", "아퍼", "여기야", "구급차", "119"]
KEYWORDS_SAFE = ["괜찮", "괜찬", "일어났", "문제없"]

# --- 현장 경보음 ---
# 경보 상태에서 스피커로 경보음을 반복 재생한다(주변 사람에게 알림).
LOCAL_SIREN = True

# --- 알림 ---
# 여러 보호자에게 보내려면 텔레그램 그룹을 만들고 그룹 chat_id를 쓴다.
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
DEVICE_NAME = "거실"           # 알림 메시지에 표시할 설치 위치
NOTIFY_TIMEOUT_SECONDS = 5.0
NOTIFY_RETRY_DELAYS = [30, 120, 600]  # 전송 실패 시 재시도 간격(초). 마지막 값으로 계속 재시도.
NOTIFY_RETRY_CHECK_SECONDS = 5.0


def _apply_overrides() -> None:
    """config.local.toml과 FALL_* / TELEGRAM_* 환경변수로 위 값을 덮어쓴다."""
    g = globals()
    path = Path(os.getenv("FALL_CONFIG", BASE_DIR / "config.local.toml"))
    if path.exists():
        import tomllib
        with open(path, "rb") as f:
            for key, value in tomllib.load(f).items():
                if key not in g or key.startswith("_"):
                    raise KeyError(f"{path}: 알 수 없는 설정 '{key}'")
                g[key] = value

    env_map = {
        "TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID": "TELEGRAM_CHAT_ID",
        "FALL_INTERNAL_API_TOKEN": "INTERNAL_API_TOKEN",
        "FALL_POSE_MODEL": "POSE_MODEL",
        "FALL_VOICE_BACKEND": "VOICE_BACKEND",
        "FALL_DATA_DIR": "DATA_DIR",
    }
    for env, key in env_map.items():
        if os.getenv(env):
            g[key] = os.environ[env]

    g["DATA_DIR"], g["ASSETS_DIR"] = Path(g["DATA_DIR"]), Path(g["ASSETS_DIR"])
    if g["MEDIAPIPE_MODEL_PATH"] is None:
        g["MEDIAPIPE_MODEL_PATH"] = g["ASSETS_DIR"] / "models" / "pose_landmarker_lite.task"
    if g["VOSK_MODEL_PATH"] is None:
        g["VOSK_MODEL_PATH"] = g["DATA_DIR"] / "vosk-model-small-ko"
    g["MEDIAPIPE_MODEL_PATH"], g["VOSK_MODEL_PATH"] = Path(g["MEDIAPIPE_MODEL_PATH"]), Path(g["VOSK_MODEL_PATH"])


_apply_overrides()

AUDIO_DIR = ASSETS_DIR / "audio"
DB_PATH = DATA_DIR / "fall_events.db"
API_BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
