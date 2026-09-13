import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageChops, ImageStat, ImageDraw

from image_delivery import CAPTION_RENDER_VERSION, _draw_caption, _trim_uniform_margins, clean_export


class ImageCaptionTests(unittest.TestCase):
    def test_caption_overlay_preserves_dimensions_and_unmodified_input(self):
        photo = Image.new("RGB", (640, 480), (80, 120, 160))
        for y in range(480):
            photo.putpixel((y, y), (y % 256, 20, 70))
        original = photo.tobytes()
        result, style = _draw_caption(photo, "재고 언제 확인할까?")
        self.assertEqual(style["caption_band_height"], 0)
        self.assertEqual(result.size, photo.size)
        self.assertEqual(photo.tobytes(), original)
        self.assertNotEqual(result.tobytes(), original)
        self.assertEqual(style["caption_text_colors"], ['#FFFFFF', '#8CE88C', '#FF4040'])
        self.assertEqual(style["caption_render_version"], CAPTION_RENDER_VERSION)
        self.assertIn(len(style["caption_text_lines"]), (1, 2, 3))
        self.assertEqual(' '.join(style["caption_text_lines"]), "재고 언제 확인할까?")

    def test_export_metadata_identifies_center_caption_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, plain = [Path(directory) / name for name in ("reference.png", "caption.jpg", "plain.jpg")]
            Image.new("RGB", (640, 480), (80, 120, 160)).save(source)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            plain_result = clean_export(source, plain, target_long_side=640)
            result = clean_export(source, target, caption="재고 확인법", target_long_side=640)
            self.assertEqual(result["caption_text"], "재고 확인법")
            self.assertTrue(result["caption_applied"])
            self.assertEqual(result["caption_placement"], "center")
            self.assertEqual(result["caption_layout"], "center_overlay")
            self.assertEqual(result["caption_text_color"], "#8CE88C")
            self.assertEqual(result["caption_background_color"], "#000000")
            self.assertEqual(result["caption_render_version"], CAPTION_RENDER_VERSION)
            self.assertEqual(result["width"], plain_result["width"])
            self.assertEqual(result["height"] - result["caption_band_height"], plain_result["height"])
            self.assertFalse(result["cover_text_applied"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            self.assertEqual(result["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            with Image.open(plain) as original_photo, Image.open(target) as captioned:
                photo = captioned.crop((0, result["caption_band_height"], 640, result["height"]))
                # The uniform far corner stays unchanged; the title overlays
                # only the center, with mild camera-like blur in the background.
                diff = ImageChops.difference(original_photo.crop((0, 0, 48, 48)), photo.crop((0, 0, 48, 48)))
                self.assertLess(max(ImageStat.Stat(diff).mean), 2)
                central = ImageChops.difference(original_photo.crop((160, 120, 480, 360)), photo.crop((160, 120, 480, 360)))
                self.assertGreater(max(ImageStat.Stat(central).mean), 20)

    def test_rejects_invalid_captions_before_creating_destination(self):
        invalid = ("가" * 29, "가" * 27 + "  ", "한글\n설명", "한글\r설명", "한글\t설명",
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

    def test_accepts_twenty_eight_characters_and_keeps_question_words(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'reference.png', Path(directory)/'caption.jpg'
            Image.new('RGB', (640, 480), (65, 85, 105)).save(source)
            for caption in ('가' * 27 + '?', '기차표 취소표 언제 확인할까?'):
                result = clean_export(source, target, caption=caption, target_long_side=640)
                self.assertEqual(result['caption_text'], caption)
                self.assertTrue(result['caption_text_lines'][-1].endswith('?'))
                if ' ' in caption:
                    self.assertEqual(' '.join(result['caption_text_lines']), caption)
                self.assertIn(len(result['caption_text_lines']), (2, 3))

    def test_uniform_thin_white_and_black_exterior_margins_are_trimmed(self):
        for background in ('white', 'black'):
            with self.subTest(background=background):
                original = Image.new('RGB', (400, 300), background)
                ImageDraw.Draw(original).rectangle((12, 10, 387, 289), fill=(70, 100, 140))
                before = original.tobytes()
                trimmed, style = _trim_uniform_margins(original)
                self.assertEqual(style['caption_trim_box'], [12, 10, 388, 290])
                self.assertEqual(trimmed.size, (376, 280))
                self.assertTrue(style['caption_margin_trimmed'])
                self.assertEqual(original.tobytes(), before)

    def test_letterbox_uniform_margins_do_not_crop_photo_side_edges(self):
        original = Image.new('RGB', (400, 300), 'black')
        ImageDraw.Draw(original).rectangle((0, 12, 399, 287), fill=(70, 100, 140))
        _, style = _trim_uniform_margins(original)
        self.assertEqual(style['caption_trim_box'], [0, 12, 400, 288])

    def test_colored_wide_asymmetric_and_nonuniform_borders_are_preserved(self):
        fixtures = []
        for background, bounds in (('blue', (12, 10, 387, 289)), ('white', (60, 60, 339, 239)),
                                   ('black', (3, 3, 365, 265))):
            picture = Image.new('RGB', (400, 300), background)
            ImageDraw.Draw(picture).rectangle(bounds, fill=(70, 100, 140))
            fixtures.append(picture)
        nonuniform = Image.new('RGB', (400, 300), 'white')
        draw = ImageDraw.Draw(nonuniform)
        draw.rectangle((12, 10, 387, 289), fill=(70, 100, 140))
        draw.rectangle((0, 0, 2, 299), fill='red')
        draw.rectangle((0, 0, 399, 2), fill='red')
        fixtures.append(nonuniform)
        for original in fixtures:
            trimmed, style = _trim_uniform_margins(original)
            self.assertEqual(trimmed.tobytes(), original.tobytes())
            self.assertFalse(style['caption_margin_trimmed'])

    def test_trim_never_removes_nonborder_text_or_logo_pixels(self):
        original = Image.new('RGB', (400, 300), 'white')
        draw = ImageDraw.Draw(original)
        draw.rectangle((12, 10, 387, 289), fill=(70, 100, 140))
        draw.text((5, 3), 'TEXT', fill='black')
        trimmed, style = _trim_uniform_margins(original)
        left, top, right, bottom = style['caption_trim_box']
        for y in range(300):
            for x in range(400):
                if original.getpixel((x, y)) != (255, 255, 255):
                    self.assertTrue(left <= x < right and top <= y < bottom)
                    self.assertEqual(trimmed.getpixel((x - left, y - top)), original.getpixel((x, y)))

    def test_caption_export_records_oriented_crop_and_keeps_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'reference.png', Path(directory)/'caption.jpg'
            picture = Image.new('RGB', (400, 300), 'white')
            ImageDraw.Draw(picture).rectangle((12, 10, 387, 289), fill=(70, 100, 140))
            picture.save(source)
            before = source.read_bytes()
            result = clean_export(source, target, caption='예약 언제 해야 할까?', target_long_side=376)
            self.assertEqual(result['caption_original_size'], [400, 300])
            self.assertEqual(result['caption_trim_box'], [12, 10, 388, 290])
            self.assertEqual((result['width'], result['height']), (376, 280))
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(result['caption_panel_color'], '#000000')
            self.assertAlmostEqual(result['caption_panel_opacity'], 140 / 255)

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
