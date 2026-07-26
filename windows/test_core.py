import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from picture_cleaner_pc import build_prompt, detect_content_bounds, is_ephemeral_keyword, process_image
from naver_automation import NaverAutomation


class CoreTests(unittest.TestCase):
    def test_crop_detects_colored_center(self):
        image = Image.new("RGB", (500, 400), "white")
        ImageDraw.Draw(image).rectangle((80, 50, 420, 350), fill=(30, 120, 210))
        left, top, right, bottom = detect_content_bounds(image)
        self.assertLess(left, 90)
        self.assertGreater(left, 60)
        self.assertLess(top, 60)
        self.assertGreater(right, 410)
        self.assertGreater(bottom, 340)

    def test_process_creates_jpeg(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "Screenshot_test.png"
            Image.new("RGB", (800, 600), (50, 120, 200)).save(source)
            output = process_image(source, root / "out")
            self.assertTrue(output.exists())
            with Image.open(output) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(max(image.size), 2048)

    def test_prompt_contains_topic(self):
        text = build_prompt("테스트 주제", ["연관어"], "참고", True)
        self.assertIn("테스트 주제", text)
        self.assertIn("[사진 삽입 위치]", text)

    def test_generated_blog_is_split_without_publishing_marker(self):
        title, body = NaverAutomation._split_title_body("첫 줄 제목\n\n본문 내용")
        self.assertEqual(title, "첫 줄 제목")
        self.assertEqual(body, "본문 내용")

    def test_one_time_sports_keyword_is_filtered(self):
        self.assertTrue(is_ephemeral_keyword("한국 일본 축구 경기 결과"))
        self.assertTrue(is_ephemeral_keyword("프로야구 생중계"))
        self.assertFalse(is_ephemeral_keyword("여름철 전기요금 절약 방법"))


if __name__ == "__main__":
    unittest.main()
