"""음성 응답 인식과 판단.

edge가 보내는 마이크 소리를 Vosk로 실시간 인식한다. 자유 인식과 응답 문구로 제한한 인식(문법)을 함께 한다.
Vosk 한국어 소형 모델은 자유 인식에서 '괜찮아요'를 거의 알아듣지 못했다("된 채 네요" 등, docs/EVALUATION.md).
문법 인식은 그 말을 잘 알아듣지만 엉뚱한 말도 가장 가까운 문구로 바꿔 버리므로, 모든 단어 신뢰도가 기준(0.9)
이상일 때만 쓴다. 잘못된 '괜찮다'가 가장 위험한 오류이기 때문이다(combine_results).
판단이 애매하면 경보 쪽으로 기울도록 도움 요청·부정 표현을 '괜찮다'보다 먼저 검사한다.

테스트에서는 Recognizer를 바꿔 끼워 모델 없이도 돌 수 있다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

log = logging.getLogger("server.voice")

SAFE = "SAFE"                # 괜찮다고 응답
HELP = "HELP"                # 도움 요청 (부정 표현 포함)
UNCLEAR = "UNCLEAR"          # 말은 했지만 분류 불가
NO_REPLY = "NO_REPLY"        # 응답 없음
DEVICE_FAIL = "DEVICE_FAIL"  # 마이크·스피커·인식 모델 오류로 확인 불가


@dataclass
class Keywords:
    negated_safe: Sequence[str]
    help: Sequence[str]
    safe: Sequence[str]

    @classmethod
    def from_settings(cls, s) -> "Keywords":
        return cls(s.keywords_negated_safe, s.keywords_help, s.keywords_safe)


def classify(text: Optional[str], kw: Keywords) -> str:
    compact = "".join((text or "").split())
    if not compact:
        return NO_REPLY
    if any(k in compact for k in kw.negated_safe) or any(k in compact for k in kw.help):
        return HELP
    if any(k in compact for k in kw.safe):
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


@dataclass
class Recognition:
    text: str                           # 자유 인식 문장
    grammar_text: Optional[str] = None  # 문법 인식 문장 (문법을 쓰지 않으면 None)
    grammar_conf: float = 0.0           # 문법 인식 단어 신뢰도의 최솟값


def judge(rec: Recognition, kw: Keywords, min_conf: float) -> Tuple[str, Optional[str]]:
    """(판단 결과, 판단에 쓴 문장)."""
    free = classify(rec.text, kw)
    # 자유 인식이 아무것도 못 들었으면(무응답) 문법 인식도 쓰지 않는다.
    if rec.grammar_text is None or free == HELP or not rec.text.strip():
        return free, rec.text or None
    result = combine_results(free, classify(rec.grammar_text, kw), rec.grammar_conf, min_conf)
    log.info("음성 인식: 자유 '%s'(%s), 문법 '%s'(신뢰도 %.2f) -> %s",
             rec.text, free, rec.grammar_text, rec.grammar_conf, result)
    return result, (rec.grammar_text if result != free else rec.text) or None


class RecognitionSession:
    """한 번 듣는 동안의 인식. accept()를 소리 조각마다 차례로 부르고 finish()로 끝낸다."""

    def accept(self, pcm: bytes) -> None:
        raise NotImplementedError

    def finish(self) -> Recognition:
        raise NotImplementedError


class Recognizer:
    name = "base"
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def load(self) -> None:
        """시작할 때 한 번 부른다(모델 불러오기). 실패하면 error에 이유를 남긴다."""

    def session(self) -> RecognitionSession:
        raise NotImplementedError


@dataclass
class _Stream:
    """KaldiRecognizer 하나의 결과 모음. 실시간으로 넣으면 말이 끊긴 곳마다 결과가 나뉘어 나온다."""
    rec: object
    texts: List[str] = field(default_factory=list)
    confs: List[float] = field(default_factory=list)

    def take(self, raw: str) -> None:
        r = json.loads(raw)
        if r.get("text", "").strip():
            self.texts.append(r["text"].strip())
            self.confs.extend(w["conf"] for w in r.get("result", []))

    def accept(self, pcm: bytes) -> None:
        if self.rec.AcceptWaveform(pcm):
            self.take(self.rec.Result())

    def finish(self) -> Tuple[str, float]:
        self.take(self.rec.FinalResult())
        return " ".join(self.texts), min(self.confs, default=0.0)


class VoskSession(RecognitionSession):
    def __init__(self, model, rate: int, grammar: Optional[str]):
        from vosk import KaldiRecognizer
        free = KaldiRecognizer(model, rate)
        free.SetWords(True)
        self._free = _Stream(free)
        self._grammar = None
        if grammar:
            g = KaldiRecognizer(model, rate, grammar)
            g.SetWords(True)
            self._grammar = _Stream(g)

    def accept(self, pcm: bytes) -> None:
        self._free.accept(pcm)
        if self._grammar is not None:
            self._grammar.accept(pcm)

    def finish(self) -> Recognition:
        text, _ = self._free.finish()
        if self._grammar is None:
            return Recognition(text)
        g_text, g_conf = self._grammar.finish()
        return Recognition(text, g_text, g_conf)


class VoskRecognizer(Recognizer):
    name = "vosk"

    def __init__(self, model_path: Path, rate: int, grammar: Optional[Sequence[str]] = None):
        self.model_path = Path(model_path)
        self.rate = rate
        self.grammar = json.dumps(list(grammar), ensure_ascii=False) if grammar else None
        self.model = None
        self.error = "음성 인식 모델을 아직 불러오지 않았습니다"

    def load(self) -> None:
        try:
            from vosk import Model, SetLogLevel
        except ImportError:
            self.error = "vosk 패키지가 설치되지 않았습니다"
            log.error(self.error)
            return
        SetLogLevel(-1)
        if not self.model_path.exists():
            self.error = f"음성 인식 모델이 없습니다: {self.model_path}"
            log.error(self.error)
            return
        try:
            self.model = Model(str(self.model_path))
        except Exception as e:
            self.error = f"음성 인식 모델을 불러오지 못했습니다: {e}"
            log.error(self.error)
            return
        self.error = None

    def session(self) -> RecognitionSession:
        return VoskSession(self.model, self.rate, self.grammar)
