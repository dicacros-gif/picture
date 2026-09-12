import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageChops, ImageStat

from image_delivery import _draw_caption, clean_export


class ImageCaptionTests(unittest.TestCase):
    def test_caption_band_preserves_every_original_pixel(self):
        photo = Image.new("RGB", (640, 480), (80, 120, 160))
        for y in range(480):
            photo.putpixel((y, y), (y % 256, 20, 70))
        result, style = _draw_caption(photo, "재고 확인법")
        band = style["caption_band_height"]
        self.assertGreater(band, 0)
        self.assertEqual(result.size, (640, 480 + band))
        self.assertEqual(result.crop((0, band, 640, band + 480)).tobytes(), photo.tobytes())
        self.assertEqual(result.getpixel((0, 0)), (12, 22, 29))
        self.assertGreater(len(result.crop((0, 0, 640, band)).getcolors(maxcolors=100000)), 2)

    def test_export_metadata_identifies_top_caption_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, plain = [Path(directory) / name for name in ("reference.png", "caption.jpg", "plain.jpg")]
            Image.new("RGB", (640, 480), (80, 120, 160)).save(source)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            plain_result = clean_export(source, plain, target_long_side=640)
            result = clean_export(source, target, caption="재고 확인법", target_long_side=640)
            self.assertEqual(result["caption_text"], "재고 확인법")
            self.assertTrue(result["caption_applied"])
            self.assertEqual(result["caption_placement"], "top")
            self.assertEqual(result["caption_layout"], "separate_band")
            self.assertEqual(result["caption_text_color"], "#F8FAFC")
            self.assertEqual(result["caption_background_color"], "#0C161D")
            self.assertEqual(result["width"], plain_result["width"])
            self.assertEqual(result["height"] - result["caption_band_height"], plain_result["height"])
            self.assertFalse(result["cover_text_applied"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            self.assertEqual(result["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            with Image.open(plain) as original_photo, Image.open(target) as captioned:
                photo = captioned.crop((0, result["caption_band_height"], 640, result["height"]))
                # JPEG changes at the band boundary must not affect the photo's central area.
                diff = ImageChops.difference(original_photo.crop((16, 16, 624, 464)), photo.crop((16, 16, 624, 464)))
                self.assertLess(max(ImageStat.Stat(diff).mean), 2)

    def test_rejects_invalid_captions_before_creating_destination(self):
        invalid = ("가" * 11, "가" * 9 + "  ", "한글\n설명", "한글\r설명", "한글\t설명",
                   "한글\u2028설명", "한글\u202e설명", "English", "한글t.co", "한글http://", "www.한글",
                   "ftp://한글", "한글.kr", "주소.한국", None)
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "reference.png", Path(directory) / "caption.jpg"
            Image.new("RGB", (640, 480), "white").save(source)
            for caption in invalid:
                with self.subTest(caption=caption), self.assertRaises(ValueError):
                    clean_export(source, target, caption=caption, target_long_side=640)
                self.assertFalse(target.exists())

    def test_accepts_ten_characters_including_spaces(self):
        caption = "가나다라마 바사아자"
        self.assertEqual(len(caption), 10)
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "reference.png", Path(directory) / "caption.jpg"
            Image.new("RGB", (640, 480), "white").save(source)
            result = clean_export(source, target, caption=caption, target_long_side=640)
            self.assertEqual(result["caption_text"], caption)

    def test_caption_preserves_exif_orientation_and_existing_metadata_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "reference.jpg", Path(directory) / "caption.jpg"
            exif = Image.Exif()
            exif[274] = 6
            exif[305] = "Fixture software"
            Image.new("RGB", (640, 480), (10, 90, 180)).save(source, exif=exif)
            result = clean_export(source, target, caption="설정 화면", target_long_side=640)
            self.assertEqual(result["width"], 480)
            self.assertEqual(result["height"] - result["caption_band_height"], 640)
            with Image.open(target) as captioned, Image.open(source) as original:
                self.assertFalse(captioned.getexif())
                self.assertFalse({"exif", "xmp", "icc_profile", "software", "comment"} & captioned.info.keys())
                self.assertEqual(original.getexif()[274], 6)
                self.assertEqual(original.getexif()[305], "Fixture software")

    def test_caption_and_cover_headline_cannot_be_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "reference.png", Path(directory) / "caption.jpg"
            Image.new("RGB", (640, 480), "white").save(source)
            with self.assertRaisesRegex(ValueError, "동시에"):
                clean_export(source, target, caption="확인 방법", headline="핵심 비밀")
            self.assertFalse(target.exists())

    def test_caption_requires_available_korean_font_and_separate_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "reference.png", Path(directory) / "caption.jpg"
            Image.new("RGB", (640, 480), "white").save(source)
            with self.assertRaisesRegex(ValueError, "글꼴"):
                clean_export(source, target, caption="확인 방법", font_path=Path(directory) / "missing.ttf")
            self.assertFalse(target.exists())
            with self.assertRaisesRegex(ValueError, "경로는 달라야"):
                clean_export(source, source, caption="확인 방법")

    def test_plain_export_has_no_caption_band(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "reference.png", Path(directory) / "plain.jpg"
            Image.new("RGB", (640, 480), "white").save(source)
            result = clean_export(source, target, target_long_side=640)
            self.assertFalse(result["caption_applied"])
            self.assertEqual(result["caption_text"], "")
            self.assertEqual(result["caption_band_height"], 0)
            self.assertEqual((result["width"], result["height"]), (640, 480))


if __name__ == "__main__":
    unittest.main()
