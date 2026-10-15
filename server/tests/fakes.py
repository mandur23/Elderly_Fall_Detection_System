"""테스트용 대역: 모델 없이 도는 자세 추정·음성 인식, 기록만 하는 알림, 시험 설정."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

from server.config import Settings
from server.features import Keypoints
from server.notifier import Notifier, NotifierError
from server.pose import PoseEstimator, PoseFactory
from server.speech import Sound
from server.voice import Recognition, RecognitionSession, Recognizer

TOKEN = "test-token"


def make_settings(tmp: Path, **overrides) -> Settings:
    values = dict(
        device_token=TOKEN, db_path=str(tmp / "events.db"), listen_seconds=0.5, listen_grace_seconds=1.0,
        playback_timeout_margin=1.0, notify_retry_check_seconds=0.05, monitor_interval_seconds=0.05,
    )
    values.update(overrides)
    return Settings(**values)


def prompt_sound() -> Sound:
    return Sound(b"\x10\x00" * 1600, "test")  # 0.1초


# --- 자세 ---
def standing(x: float = 320.0, hip_y: float = 250.0) -> Keypoints:
    """몸통 길이 150px로 선 자세."""
    return Keypoints((x - 20, hip_y - 150), (x + 20, hip_y - 150), (x - 15, hip_y), (x + 15, hip_y),
                     (x - 40, hip_y - 200, x + 40, hip_y + 200))


def lying(y: float = 400.0) -> Keypoints:
    """바닥에 가로로 누운 자세 (몸통 각도 90도, 영역이 가로로 넓음)."""
    return Keypoints((200, y - 10), (200, y + 10), (350, y - 10), (350, y + 10), (120, y - 40, 560, y + 40))


def fall_script(fall_at: float, fall_seconds: float = 0.3) -> Callable[[float], Optional[Keypoints]]:
    """fall_at초까지 서 있다가 fall_seconds 동안 엉덩이가 150px 내려간 뒤 바닥에 눕는다(시각은 상대값)."""
    def script(t: float) -> Optional[Keypoints]:
        if t < fall_at:
            return standing()
        if t < fall_at + fall_seconds:
            return standing(hip_y=250 + 150 * (t - fall_at) / fall_seconds)
        return lying()
    return script


class ScriptedEstimator(PoseEstimator):
    """script(접속 후 첫 장면부터 지난 시간)로 자세를 돌려준다. 영상 내용은 보지 않는다."""

    def __init__(self, factory: "FakePoseFactory"):
        self.factory = factory
        self.t0: Optional[float] = None
        self.resets = 0

    def detect(self, rgb: np.ndarray, t: float) -> Optional[Keypoints]:
        if self.factory.delay:
            time.sleep(self.factory.delay)
        if self.t0 is None:
            self.t0 = t
        self.factory.seen.append(t)
        return self.factory.script(t - self.t0)

    def reset(self) -> None:
        self.t0 = None
        self.resets += 1


class FakePoseFactory(PoseFactory):
    name = "fake"

    def __init__(self, script: Callable[[float], Optional[Keypoints]] = lambda t: standing(), error: str = None,
                 delay: float = 0.0):
        self.script = script
        self.error = error
        self.delay = delay
        self.seen: List[float] = []

    def create(self) -> PoseEstimator:
        return ScriptedEstimator(self)


# --- 음성 인식 ---
class FakeSession(RecognitionSession):
    def __init__(self, recognizer: "FakeRecognizer"):
        self.recognizer = recognizer
        self.peak = 0
        self.bytes = 0

    def accept(self, pcm: bytes) -> None:
        samples = np.frombuffer(pcm, dtype="<i2")
        self.peak = max(self.peak, int(np.abs(samples.astype(np.int32)).max()) if len(samples) else 0)
        self.bytes += len(pcm)

    def finish(self) -> Recognition:
        self.recognizer.heard.append(self.bytes)
        # 큰 소리가 들어왔을 때만 '말했다'고 본다. 작은 잡음은 무응답.
        text = self.recognizer.text if self.peak > 1000 else ""
        return Recognition(text, self.recognizer.grammar_text if text else None, self.recognizer.grammar_conf)


class FakeRecognizer(Recognizer):
    name = "fake"

    def __init__(self, text: str = "괜찮아요", error: Optional[str] = None):
        self.text = text
        self.grammar_text: Optional[str] = None
        self.grammar_conf = 0.0
        self.error = error
        self.heard: List[int] = []

    def session(self) -> RecognitionSession:
        return FakeSession(self)


def loud_pcm(seconds: float) -> bytes:
    n = int(16000 * seconds)
    return (np.sin(np.arange(n) * 2 * np.pi * 300 / 16000) * 8000).astype("<i2").tobytes()


def quiet_pcm(seconds: float) -> bytes:
    return np.random.randint(-30, 31, int(16000 * seconds), dtype="<i2").tobytes()


# --- 알림 ---
class FakeNotifier(Notifier):
    def __init__(self, enabled: bool = True, fail: int = 0):
        self.enabled = enabled
        self.fail = fail
        self.sent: List[str] = []
        self.attempts = 0

    async def send(self, text: str) -> None:
        self.attempts += 1
        if self.fail > 0:
            self.fail -= 1
            raise NotifierError("가짜 전송 실패")
        self.sent.append(text)


def tiny_jpeg() -> bytes:
    import cv2
    ok, buf = cv2.imencode(".jpg", np.zeros((48, 64, 3), np.uint8))
    assert ok
    return buf.tobytes()
