import unittest

import config
from l4_application.voice_checker import HELP, NO_REPLY, SAFE, UNCLEAR, classify, combine_results


def c(text):
    return classify(text, config.KEYWORDS_NEGATED_SAFE, config.KEYWORDS_HELP, config.KEYWORDS_SAFE)


class ClassifyTest(unittest.TestCase):
    def test_safe(self):
        for text in ["괜찮아요", "네 괜찮아", "괜 찮 아 요", "일어났어요"]:
            self.assertEqual(c(text), SAFE, text)

    def test_help(self):
        for text in ["도와주세요", "살려줘", "아파요", "구급차 불러줘"]:
            self.assertEqual(c(text), HELP, text)

    def test_negation_and_mixed_lean_to_help(self):
        for text in ["안 괜찮아요", "괜찮지 않아", "괜찮은데 아파", "괜찮아 아니 도와줘"]:
            self.assertEqual(c(text), HELP, text)

    def test_no_reply_and_unclear(self):
        self.assertEqual(c(""), NO_REPLY)
        self.assertEqual(c(None), NO_REPLY)
        self.assertEqual(c("음 뭐라고"), UNCLEAR)

    def test_model_word_spacing_is_classified(self):
        # Vosk 소형 모델은 '괜찮 아요', '도 와 주세요'처럼 나눠서 낸다
        self.assertEqual(c("네 괜찮 아요"), SAFE)
        self.assertEqual(c("안 괜찮 아요"), HELP)
        self.assertEqual(c("도 와 주세요"), HELP)


class CombineTest(unittest.TestCase):
    """자유 인식과 문법 인식 합치기. 잘못된 SAFE가 가장 위험하다."""

    def test_confident_grammar_result_is_used(self):
        self.assertEqual(combine_results(UNCLEAR, SAFE, 1.0, 0.9), SAFE)
        self.assertEqual(combine_results(UNCLEAR, HELP, 0.95, 0.9), HELP)

    def test_low_confidence_grammar_result_is_ignored(self):
        # 시험에서 '음, 뭐라고요?'가 '응 문제 없어요'(신뢰도 0.73)로 바뀐 경우
        self.assertEqual(combine_results(UNCLEAR, SAFE, 0.73, 0.9), UNCLEAR)

    def test_free_help_wins(self):
        self.assertEqual(combine_results(HELP, SAFE, 1.0, 0.9), HELP)

    def test_no_reply_stays(self):
        self.assertEqual(combine_results(NO_REPLY, None, 0.0, 0.9), NO_REPLY)


if __name__ == "__main__":
    unittest.main()
