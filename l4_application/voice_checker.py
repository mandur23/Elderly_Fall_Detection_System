"""음성 안내 재생과 응답 확인.

안내는 미리 만든 wav 파일을 재생하고(재생이 끝난 뒤 녹음 시작), 응답은 Vosk로
오프라인 인식한 뒤 키워드로 분류한다. 판단이 애매하면 경보 쪽으로 기울도록
도움 요청·부정 표현을 '괜찮다'보다 먼저 검사한다.

Vosk 한국어 소형 모델은 자유 인식에서 '괜찮아요'를 거의 알아듣지 못했다(합성 음성 시험).
그래서 응답 문구로 제한한 인식(문법)을 한 번 더 하되, 이 방식은 엉뚱한 말도 가장 가까운 문구로
바꿔 버리므로 모든 단어의 신뢰도가 기준 이상일 때만 결과를 쓴다(combine_results 참고).
"""

import json
import logging
import queue
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from l4_application.audio import PromptPlayer

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


def combine_results(free: str, grammar: Optional[str], grammar_conf: float, min_conf: float) -> str:
    """자유 인식 분류와 문법 인식 분류를 합친다. 잘못된 '괜찮다'가 가장 위험하므로 보수적으로 고른다.

    1. 자유 인식이 도움 요청이면 그대로 도움 요청
    2. 문법 인식은 모든 단어 신뢰도가 min_conf 이상일 때만 SAFE·HELP로 인정
    3. 나머지는 자유 인식 결과 (UNCLEAR면 다시 묻고, 끝까지 불분명하면 경보)
    """
    if free == HELP:
        return HELP
    if grammar in (SAFE, HELP) and grammar_conf >= min_conf:
        return grammar
    return free


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

    def check(self) -> None:
        """평소 장치 점검. 낙상이 없을 때도 마이크 고장을 알 수 있게 주기적으로 호출된다."""

    def _classify(self, text: Optional[str]) -> str:
        return classify(text, *self.keywords)


class VoskVoiceChecker(VoiceChecker):
    name = "vosk"
    CHECK_SECONDS = 0.3

    def __init__(self, keywords, model_path: Path, prompt: PromptPlayer, sample_rate: int,
                 mic_device=None, grammar: Optional[List[str]] = None, grammar_min_conf: float = 0.9):
        super().__init__(keywords)
        self.prompt = prompt
        self.sample_rate = sample_rate
        self.mic_device = mic_device
        self.grammar = json.dumps(grammar, ensure_ascii=False) if grammar else None
        self.grammar_min_conf = grammar_min_conf
        self.model = None
        self._busy = threading.Lock()  # 음성 확인과 장치 점검이 마이크를 동시에 쓰지 않게 한다
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

    def play_prompt(self) -> None:
        self.prompt.play()

    def _record(self, seconds: float):
        import sounddevice as sd
        audio = sd.rec(int(seconds * self.sample_rate), samplerate=self.sample_rate,
                       channels=1, dtype="int16", device=self.mic_device)
        sd.wait()
        return audio

    def _record_checked(self, seconds: float):
        """녹음하고 마이크 상태(self.error)를 갱신한다. 장치 오류·완전 무음이면 None."""
        try:
            audio = self._record(seconds)
        except Exception as e:
            self.error = f"마이크 오류: {e}"
            log.error(self.error)
            return None
        if int(abs(audio).max()) == 0:
            # 완전한 무음은 조용한 방이 아니라 마이크 고장·음소거일 가능성이 높다.
            self.error = "마이크 입력이 없습니다 (연결·음소거 확인)"
            log.error(self.error)
            return None
        if self.error is not None:
            log.info("마이크 정상 복구")
        self.error = None
        return audio

    def check(self) -> None:
        """짧게 녹음해 소리 크기만 확인한다. 녹음 내용은 인식하거나 저장하지 않는다."""
        if self.model is None or not self._busy.acquire(blocking=False):
            return  # 모델이 없으면 점검할 수 없고, 음성 확인 중이면 건너뛴다
        try:
            self._record_checked(self.CHECK_SECONDS)
        finally:
            self._busy.release()

    def listen(self, seconds: float) -> Tuple[str, Optional[str]]:
        if self.model is None:
            return DEVICE_FAIL, None
        with self._busy:
            audio = self._record_checked(seconds)
        if audio is None:
            return DEVICE_FAIL, None
        return self.recognize(audio.tobytes())

    def recognize(self, pcm: bytes) -> Tuple[str, Optional[str]]:
        """16비트 mono PCM을 인식해 (분류 결과, 인식 문장)을 반환한다. 설치 점검 도구도 이 경로를 쓴다."""
        text, _ = self._recognize(pcm, None)
        free = self._classify(text)
        result = free
        if self.grammar and free != HELP and text:
            g_text, g_conf = self._recognize(pcm, self.grammar)
            result = combine_results(free, self._classify(g_text), g_conf, self.grammar_min_conf)
            log.info("음성 인식: 자유 '%s'(%s), 문법 '%s'(신뢰도 %.2f) -> %s", text, free, g_text, g_conf, result)
            if result != free:
                text = g_text
        else:
            log.info("음성 인식 결과: '%s' -> %s", text, result)
        return result, text or None

    def _recognize(self, pcm: bytes, grammar: Optional[str]) -> Tuple[str, float]:
        """(인식 문장, 단어 신뢰도 최솟값)."""
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(self.model, self.sample_rate, grammar) if grammar \
            else KaldiRecognizer(self.model, self.sample_rate)
        rec.SetWords(True)
        rec.AcceptWaveform(pcm)
        r = json.loads(rec.FinalResult())
        words = r.get("result", [])
        return r.get("text", "").strip(), min((w["conf"] for w in words), default=0.0)


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
