import os
import unittest
import wave
from pathlib import Path

from server.config import Settings
from server.voice import (DEVICE_FAIL, HELP, NO_REPLY, SAFE, UNCLEAR, Keywords, Recognition, VoskRecognizer,
                          classify, combine_results, judge)

KW = Keywords.from_settings(Settings())


class ClassifyTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("괜찮아요", KW), SAFE)
        self.assertEqual(classify("괜찮 아요", KW), SAFE)  # Vosk 어휘처럼 띄어 써도
        self.assertEqual(classify("도와 주세요", KW), HELP)
        self.assertEqual(classify("살려줘", KW), HELP)
        self.assertEqual(classify("뭐라고요", KW), UNCLEAR)
        self.assertEqual(classify("", KW), NO_REPLY)
        self.assertEqual(classify(None, KW), NO_REPLY)

    def test_negation_and_help_are_checked_first(self):
        self.assertEqual(classify("안 괜찮아요", KW), HELP)
        self.assertEqual(classify("괜찮지 않아", KW), HELP)
        self.assertEqual(classify("괜찮은데 아파요", KW), HELP)


class CombineTest(unittest.TestCase):
    def test_free_help_wins(self):
        self.assertEqual(combine_results(HELP, SAFE, 1.0, 0.9), HELP)

    def test_grammar_used_only_with_high_confidence(self):
        self.assertEqual(combine_results(UNCLEAR, SAFE, 0.95, 0.9), SAFE)
        # "음, 뭐라고요?"가 문법 인식에서 "응 문제 없어요"(0.76)로 바뀐 경우 -> 받아들이지 않음
        self.assertEqual(combine_results(UNCLEAR, SAFE, 0.76, 0.9), UNCLEAR)
        self.assertEqual(combine_results(UNCLEAR, HELP, 0.95, 0.9), HELP)
        self.assertEqual(combine_results(UNCLEAR, UNCLEAR, 1.0, 0.9), UNCLEAR)

    def test_judge(self):
        # 소형 모델은 '괜찮아요'를 자유 인식에서 '된 채 네요'로 들었다(합성 음성 시험)
        self.assertEqual(judge(Recognition("된 채 네요", "괜찮 아요", 1.0), KW, 0.9), (SAFE, "괜찮 아요"))
        self.assertEqual(judge(Recognition("음 뭐 라고요", "응 문제 없어요", 0.76), KW, 0.9),
                         (UNCLEAR, "음 뭐 라고요"))
        self.assertEqual(judge(Recognition("도 와 주세요", "괜찮 아요", 1.0), KW, 0.9)[0], HELP)
        # 자유 인식이 아무것도 못 들었으면 문법 인식을 믿지 않는다
        self.assertEqual(judge(Recognition("", "괜찮 아요", 1.0), KW, 0.9), (NO_REPLY, None))
        self.assertEqual(judge(Recognition("괜찮아요"), KW, 0.9), (SAFE, "괜찮아요"))


class VoskModelMissingTest(unittest.TestCase):
    def test_missing_model_is_reported(self):
        rec = VoskRecognizer(Path("없는-모델-폴더"), 16000)
        rec.load()
        self.assertFalse(rec.ok)
        self.assertIsNotNone(rec.error)


VOSK_MODEL = os.environ.get("SERVER_TEST_VOSK_MODEL")
TTS_DIR = os.environ.get("SERVER_TEST_TTS_DIR")


@unittest.skipUnless(VOSK_MODEL and TTS_DIR, "SERVER_TEST_VOSK_MODEL, SERVER_TEST_TTS_DIR를 설정하면 실제 모델로 시험")
class VoskSamplesTest(unittest.TestCase):
    """합성 음성(docs/EVALUATION.md 2절)을 0.1초씩 실시간처럼 넣어 기존 평가와 같은 결과가 나오는지 확인."""

    @classmethod
    def setUpClass(cls):
        s = Settings()
        cls.rec = VoskRecognizer(Path(VOSK_MODEL), 16000, s.voice_grammar)
        cls.rec.load()
        cls.min_conf = s.voice_grammar_min_conf

    def result(self, name: str) -> str:
        with wave.open(str(Path(TTS_DIR) / name)) as w:
            pcm = w.readframes(w.getnframes())
        session = self.rec.session()
        for i in range(0, len(pcm), 3200):
            session.accept(pcm[i:i + 3200])
        return judge(session.finish(), KW, self.min_conf)[0]

    def test_samples(self):
        self.assertTrue(self.rec.ok, self.rec.error)
        safe = [self.result(f"safe{i}.wav") for i in range(1, 5)]
        self.assertGreaterEqual(safe.count(SAFE), 3)
        for i in range(1, 4):
            self.assertEqual(self.result(f"help{i}.wav"), HELP)
        self.assertNotEqual(self.result("negated.wav"), SAFE)
        others = ["unclear.wav"] + [f"other{i}.wav" for i in range(1, 7)]
        self.assertNotIn(SAFE, [self.result(n) for n in others])  # 잘못된 '괜찮다' 0건
        self.assertEqual(self.result("quiet.wav"), NO_REPLY)
        self.assertNotEqual(DEVICE_FAIL, SAFE)


if __name__ == "__main__":
    unittest.main()
