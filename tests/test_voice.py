import unittest

import config
from l4_application.voice_checker import HELP, NO_REPLY, SAFE, UNCLEAR, classify


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


if __name__ == "__main__":
    unittest.main()
