import copy
import unittest
from unittest.mock import MagicMock

from naver_automation import NaverAutomation


class PublishedHashtagColorTests(unittest.TestCase):
    def setUp(self):
        self.driver = MagicMock()
        self.paragraphs = ["일반 본문."] * 7 + ["마지막 본문.\n#CPI #한국시간\n정리 뜻과 의미"]
        self.visual_style = {"quote_layouts": [""] * 8}
        self.rendered = [[[self.node("일반 본문.")]] for _ in range(7)] + [[
            [self.node("마지막 본문.")],
            [self.node("#CPI", color="rgb(56, 124, 187)", native=True), self.node(" "),
             self.node("#", color="rgb(56, 124, 187)", native=True),
             self.node("한국시간", color="rgb(187, 0, 92)", native=True)],
            [self.node("정리 뜻과 의미")],
        ]]
        self.driver.execute_script.return_value = self.rendered

    @staticmethod
    def node(text, *, color="rgb(0, 0, 0)", native=False):
        return {"value": text, "color": color, "background": "", "underline": False,
                "native_hashtag": native}

    def check(self, **kwargs):
        return NaverAutomation._article_native_colors_rendered(
            self.driver, self.paragraphs, ["한국시간"], self.visual_style, **kwargs)

    def test_native_tint_is_accepted_only_for_published_default_footer_hashtags(self):
        self.assertFalse(self.check())
        self.assertTrue(self.check(published=True))
        self.assertIn("closest('span.__se-hash-tag')", self.driver.execute_script.call_args.args[0])

    def test_blue_hashtag_without_native_marker_is_rejected(self):
        self.rendered[7][1][0]["native_hashtag"] = False
        self.assertFalse(self.check(published=True))

    def test_native_tint_cannot_replace_an_intentional_keyword_color(self):
        self.rendered[7][1][3]["color"] = "rgb(56, 124, 187)"
        self.assertFalse(self.check(published=True))

    def test_unobserved_native_tint_is_rejected(self):
        self.rendered[7][1][0]["color"] = "rgb(255, 0, 255)"
        self.assertFalse(self.check(published=True))

    def test_hashtag_background_and_underline_still_require_exact_match(self):
        original = copy.deepcopy(self.rendered)
        for update in ({"background": "rgb(255, 248, 178)"}, {"underline": True}):
            with self.subTest(update=update):
                self.driver.execute_script.return_value = copy.deepcopy(original)
                self.driver.execute_script.return_value[7][1][0].update(update)
                self.assertFalse(self.check(published=True))

    def test_extra_non_hashtag_content_disables_footer_exception(self):
        self.paragraphs[7] = self.paragraphs[7].replace("#CPI #한국시간", "#CPI #한국시간 설명")
        self.rendered[7][1].append(self.node(" 설명"))
        self.assertFalse(self.check(published=True))

    def test_native_marker_does_not_permit_body_or_earlier_section_color_change(self):
        self.rendered[0][0][0].update(color="rgb(56, 124, 187)", native_hashtag=True)
        self.assertFalse(self.check(published=True))
        self.paragraphs[0] = "#CPI"
        self.rendered[0][0][0]["value"] = "#CPI"
        self.assertFalse(self.check(published=True))

    def test_only_last_hashtag_only_row_in_final_section_gets_exception(self):
        self.paragraphs[7] = "#첫태그\n" + self.paragraphs[7]
        self.rendered[7].insert(0, [self.node("#첫태그", color="rgb(56, 124, 187)", native=True)])
        self.assertFalse(self.check(published=True))

    def test_native_footer_marker_never_allows_changed_text(self):
        self.rendered[7][1][0]["value"] = "#ABC"
        self.assertFalse(self.check(published=True))

    def test_plain_gray_text_is_rejected_after_publication(self):
        self.rendered[0][0][0]['color'] = 'rgb(51, 51, 51)'
        self.assertFalse(self.check(published=True))


if __name__ == "__main__":
    unittest.main()
