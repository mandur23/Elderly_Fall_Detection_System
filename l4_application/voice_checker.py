"""음성 안내 재생과 응답 확인.

안내는 미리 만든 wav 파일을 재생하고(재생이 끝난 뒤 녹음 시작), 응답은 Vosk로
오프라인 인식한 뒤 키워드로 분류한다. 판단이 애매하면 경보 쪽으로 기울도록
도움 요청·부정 표현을 '괜찮다'보다 먼저 검사한다.
"""

import json
import logging
import os
import queue
import subprocess
from pathlib import Path
from typing import Iterable, Optional, Tuple

log = logging.getLogger("l4.voice")

SAFE = "SAFE"          # 괜찮다고 응답
HELP = "HELP"          # 도움 요청
UNCLEAR = "UNCLEAR"    # 말은 했지만 분류 불가
NO_REPLY = "NO_REPLY"  # 응답 없음
DEVICE_FAIL = "DEVICE_FAIL"  # 마이크·인식 모델 오류


def classify(text: Optional[str], negated_safe: Iterable[str], help_words: Iterable[str],
             safe_words: Iterable[str]) -> str:
    compact = "".join((text or "").split())
    if not compact:
        return NO_REPLY
    if any(k in compact for k in negated_safe) or any(k in compact for k in help_words):
        return HELP
    if any(k in compact for k in safe_words):
        return SAFE
    return UNCLEAR


class VoiceChecker:
    """음성 확인 인터페이스."""
    name = "base"

    def __init__(self, keywords: Tuple[Iterable[str], Iterable[str], Iterable[str]]):
        self.keywords = keywords
        self.error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def play_prompt(self) -> None:
        raise NotImplementedError

    def listen(self, seconds: float) -> Tuple[str, Optional[str]]:
        """(분류 결과, 인식 문장)을 반환한다."""
        raise NotImplementedError

    def _classify(self, text: Optional[str]) -> str:
        return classify(text, *self.keywords)


def play_wav_blocking(path: Path) -> None:
    if os.name == "nt":
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
    else:
        subprocess.run(["aplay", "-q", str(path)], check=True, timeout=30)


class VoskVoiceChecker(VoiceChecker):
    name = "vosk"

    def __init__(self, keywords, model_path: Path, prompt_wav: Path, sample_rate: int):
        super().__init__(keywords)
        self.prompt_wav = prompt_wav
        self.sample_rate = sample_rate
        self.model = None
        try:
            from vosk import Model, SetLogLevel
            SetLogLevel(-1)
            if not Path(model_path).exists():
                raise FileNotFoundError(f"Vosk 모델이 없습니다: {model_path}")
            self.model = Model(str(model_path))
            import sounddevice  # noqa: F401  마이크 라이브러리 존재 확인
        except Exception as e:
            self.error = f"음성 인식 준비 실패: {e}"
            log.error(self.error)
        if not prompt_wav.exists():
            log.warning("안내 음성 파일이 없습니다: %s (화면 안내만 표시됩니다)", prompt_wav)

    def play_prompt(self) -> None:
        if not self.prompt_wav.exists():
            return
        try:
            play_wav_blocking(self.prompt_wav)
        except Exception as e:
            log.error("안내 음성 재생 실패: %s", e)

    def listen(self, seconds: float) -> Tuple[str, Optional[str]]:
        if self.model is None:
            return DEVICE_FAIL, None
        try:
            import sounddevice as sd
            from vosk import KaldiRecognizer
            audio = sd.rec(int(seconds * self.sample_rate), samplerate=self.sample_rate,
                           channels=1, dtype="int16")
            sd.wait()
            rec = KaldiRecognizer(self.model, self.sample_rate)
            rec.AcceptWaveform(audio.tobytes())
            text = json.loads(rec.FinalResult()).get("text", "").strip()
        except Exception as e:
            self.error = f"마이크 오류: {e}"
            log.error(self.error)
            return DEVICE_FAIL, None
        self.error = None
        log.info("음성 인식 결과: '%s'", text)
        return self._classify(text), text or None


class SimulatedVoiceChecker(VoiceChecker):
    """데모·테스트용. inject()로 넣은 문장을 다음 듣기 결과로 돌려준다."""
    name = "simulated"

    def __init__(self, keywords):
        super().__init__(keywords)
        self._replies: "queue.Queue[Optional[str]]" = queue.Queue()

    def inject(self, text: Optional[str]) -> None:
        self._replies.put(text)

    def play_prompt(self) -> None:
        log.info("[안내 음성] 괜찮으신가요?")

    def listen(self, seconds: float) -> Tuple[str, Optional[str]]:
        try:
            text = self._replies.get(timeout=seconds)
        except queue.Empty:
            return NO_REPLY, None
        if text == DEVICE_FAIL:
            return DEVICE_FAIL, None
        return self._classify(text), text
