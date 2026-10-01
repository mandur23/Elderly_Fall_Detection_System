"""시스템 전역 설정.

아래 임계값과 대기 시간은 시험용 초기값이다. 실제 값은 테스트 결과를 보고
팀이 합의해 정한다(docs/REQUIREMENTS.md '확정 전 항목' 참고).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("FALL_DATA_DIR", BASE_DIR / "data"))
AUDIO_DIR = BASE_DIR / "assets" / "audio"

# --- 이벤트 기록 (SQLite) ---
DB_PATH = DATA_DIR / "fall_events.db"
# 음성 인식 문장 원문 저장 여부. 기본은 분류 결과(SAFE/HELP 등)만 저장한다.
STORE_VOICE_TEXT = False
# 이벤트 보관 기간(일). None이면 자동 삭제하지 않는다. 보관 정책 확정 후 설정.
EVENT_RETENTION_DAYS = None

# --- 로컬 API 서버 (외부 접근 차단을 위해 루프백에만 바인딩) ---
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000
API_BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

# --- 영상 입력 / 자세 추정 (L3) ---
CAMERA_INDEX = 0
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
TARGET_FPS = 10
# 'mediapipe' | 'yolo' | 'mock'. 'mock'은 데모·테스트 전용이며 실제 감지를 하지 않는다.
POSE_MODEL = os.getenv("FALL_POSE_MODEL", "mediapipe")
YOLO_MODEL_PATH = "yolov8n-pose.pt"
KEYPOINT_MIN_CONFIDENCE = 0.5

# --- 낙상 판정 (L3 FSM) ---
# 하강 속도 단위: 몸통 길이/초 (엉덩이 중심 y 변화량을 몸통 길이로 나눈 값).
# 카메라와의 거리에 덜 민감하도록 픽셀 대신 몸통 길이로 정규화한다.
DESCENT_VELOCITY_PEAK = 2.0
TORSO_ANGLE_LYING = 60.0      # 수직선 대비 몸통 각도(도) 이상이면 누운 자세
ASPECT_RATIO_LYING = 1.0      # 바운딩 박스 가로/세로 이상이면 누운 자세
TORSO_ANGLE_STANDING = 40.0   # 이 각도 미만이고
ASPECT_RATIO_STANDING = 0.8   # 종횡비가 이 값 미만이면 다시 일어선 것으로 본다
T1_LYING_SECONDS = 2.0        # 하강 후 누운 자세가 연속으로 유지되어야 하는 시간
FALLING_TIMEOUT_SECONDS = 5.0 # 하강 후 누움·기립 어느 쪽도 확정되지 않으면 정상 복귀

# --- L3 -> L4 보고 ---
HEARTBEAT_INTERVAL_SECONDS = 1.0
HEARTBEAT_STALE_SECONDS = 5.0  # 이 시간 동안 하트비트가 없으면 감지 프로세스 이상으로 표시

# --- 음성 확인 (L4) ---
# 'vosk' | 'simulated'. 'simulated'는 데모·테스트 전용.
VOICE_BACKEND = os.getenv("FALL_VOICE_BACKEND", "vosk")
VOSK_MODEL_PATH = DATA_DIR / "vosk-model-small-ko"
VOICE_SAMPLE_RATE = 16000
VOICE_LISTEN_SECONDS = 5.0     # 한 번 질문 후 응답을 듣는 시간
VOICE_MAX_ATTEMPTS = 2         # 무응답·불분명 시 재질문 포함 최대 시도 횟수
PROMPT_TEXT = "괜찮으신가요? 괜찮으시면 '괜찮아요'라고 말씀하시거나 화면 아래 버튼을 눌러 주세요."

# 부정 표현은 '괜찮아'보다 먼저 검사한다 ("안 괜찮아요" -> 도움 요청).
KEYWORDS_NEGATED_SAFE = ["안괜찮", "안괜찬", "괜찮지않", "괜찮지안"]
KEYWORDS_HELP = ["도와", "살려", "아파", "아퍼", "여기야", "구급차", "119"]
KEYWORDS_SAFE = ["괜찮", "괜찬", "일어났", "문제없"]

# --- 알림 ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
NOTIFY_TIMEOUT_SECONDS = 5.0
NOTIFY_RETRY_DELAYS = [30, 120, 600]  # 전송 실패 시 재시도 간격(초). 마지막 값으로 계속 재시도.
NOTIFY_RETRY_CHECK_SECONDS = 5.0
