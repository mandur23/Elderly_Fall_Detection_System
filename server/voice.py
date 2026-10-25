"""음성 확인: 응답 녹음을 문장으로 바꾸고(Whisper), 키워드로 분류한다.

판단이 애매하면 경보 쪽으로 기울도록 부정 표현("안 괜찮아요")과 도움 요청을
'괜찮다'보다 먼저 검사한다. 놓친 낙상보다 잘못 울린 경보가 덜 위험하기 때문이다.

녹음은 서버 메모리와 임시 파일에서만 다루고 인식이 끝나면 바로 지운다
(docs/ARCHITECTURE.md '데이터와 개인정보').
"""

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Optional

from server.config import Settings

log = logging.getLogger("server.voice")

SAFE = "SAFE"                # 괜찮다고 응답
HELP = "HELP"                # 도움 요청(부정 표현 포함)
UNCLEAR = "UNCLEAR"          # 말은 했지만 분류 불가
NO_REPLY = "NO_REPLY"        # 응답 없음(인식된 말이 없음)
DEVICE_FAIL = "DEVICE_FAIL"  # 녹음·인식 실패, 마이크 권한 거부, 기기 연결 끊김

# faster-whisper는 PyAV로 디코딩하므로 컨테이너 형식은 확장자보다 내용으로 판단한다.
# 그래도 임시 파일에 알려진 확장자를 붙여 두면 디코더가 형식을 더 쉽게 고른다.
_KNOWN_SUFFIXES = (".wav", ".webm", ".ogg", ".mp4", ".m4a")


def classify(text: Optional[str], negated_safe: Iterable[str], help_words: Iterable[str],
             safe_words: Iterable[str]) -> str:
    """인식 문장을 SAFE/HELP/UNCLEAR/NO_REPLY 중 하나로 분류한다.

    Whisper가 띄어쓰기를 제멋대로 넣으므로("괜 찮 아 요") 공백을 모두 지우고 비교한다.
    """
    compact = "".join((text or "").split())
    if not compact:
        return NO_REPLY
    if any(k in compact for k in negated_safe) or any(k in compact for k in help_words):
        return HELP
    if any(k in compact for k in safe_words):
        return SAFE
    return UNCLEAR


def classify_with_settings(text: Optional[str], settings: Settings) -> str:
    return classify(text, settings.keywords_negated_safe, settings.keywords_help, settings.keywords_safe)


class TranscriberError(RuntimeError):
    """모델 준비 또는 인식 실패. 호출 쪽은 DEVICE_FAIL(확인 불가 → 경보)로 처리한다."""


class Transcriber:
    """음성 인식 인터페이스. transcribe()는 막히는(blocking) 호출이라 asyncio.to_thread로 부른다."""

    name = "base"

    def load(self) -> None:
        """모델을 미리 올린다. 첫 낙상 때 모델 로딩을 기다리지 않도록 서버 시작 시 부를 수 있다."""

    def transcribe(self, audio_bytes: bytes, filename: str) -> str:
        raise NotImplementedError


class WhisperTranscriber(Transcriber):
    name = "whisper"

    def __init__(self, model_size: str):
        self.model_size = model_size
        self._model = None
        # 모델 하나를 모든 기기가 같이 쓴다. 동시에 인식하면 메모리·CPU가 겹치므로 한 번에 하나씩 처리한다.
        self._lock = threading.Lock()

    def _ensure_model(self):
        """잠금을 잡은 상태에서만 부른다."""
        if self._model is not None:
            return self._model
        try:
            # 무거운 패키지라 실제로 쓸 때만 불러온다(테스트 환경에는 설치하지 않는다).
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise TranscriberError("faster-whisper가 설치되어 있지 않습니다") from e
        try:
            log.info("Whisper 모델 로딩: %s", self.model_size)
            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
        except Exception as e:
            raise TranscriberError(f"Whisper 모델 로딩 실패: {e}") from e
        return self._model

    def load(self) -> None:
        with self._lock:
            self._ensure_model()

    def transcribe(self, audio_bytes: bytes, filename: str) -> str:
        suffix = Path(filename or "").suffix.lower()
        if suffix not in _KNOWN_SUFFIXES:
            suffix = ".wav"
        with self._lock:
            model = self._ensure_model()
            # faster-whisper는 파일 경로를 받는 편이 형식 처리가 가장 안정적이라 임시 파일에 쓴다.
            fd, path = tempfile.mkstemp(prefix="voice_", suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(audio_bytes)
                segments, _info = model.transcribe(path, language="ko")
                # segments는 지연 생성기다. 파일을 지우기 전에 끝까지 읽어야 한다.
                text = "".join(seg.text for seg in segments).strip()
            except TranscriberError:
                raise
            except Exception as e:
                raise TranscriberError(f"음성 인식 실패: {e}") from e
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    log.warning("임시 녹음 파일 삭제 실패: %s", path)
        log.info("음성 인식 완료 (%d자)", len(text))
        return text


class FakeTranscriber(Transcriber):
    """테스트 전용. 녹음 바이트를 인식 문장(UTF-8)으로 그대로 읽는다.

    UTF-8이 아닌 바이트(예: b"\\xff")는 인식 실패로 보고 TranscriberError를 던진다.
    """

    name = "fake"

    def transcribe(self, audio_bytes: bytes, filename: str) -> str:
        try:
            return audio_bytes.decode("utf-8").strip()
        except UnicodeDecodeError as e:
            raise TranscriberError("가짜 인식기: UTF-8이 아닌 녹음") from e


def create_transcriber(settings: Settings) -> Transcriber:
    backend = settings.stt_backend
    if backend == "whisper":
        return WhisperTranscriber(settings.whisper_model)
    if backend == "fake":
        log.warning("가짜 음성 인식기를 사용합니다(테스트 전용)")
        return FakeTranscriber()
    raise ValueError(f"알 수 없는 음성 인식 백엔드: {backend}")
