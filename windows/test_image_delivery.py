import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageColor, ImageChops, ImageDraw, PngImagePlugin
from image_delivery import COVER_RENDER_VERSION, OVERLAY_TEXT_COLORS, _draw_cover, _draw_question_overlay, clean_export


class ImageDeliveryTests(unittest.TestCase):
    def test_short_hook_keeps_word_spacing_in_one_readable_line(self):
        image, style = _draw_question_overlay(Image.new('RGB', (1024, 1024), 'gray'), '의외의 진실?')
        self.assertEqual(style['text_lines'], ['의외의 진실?'])
        self.assertEqual(image.size, (1024, 1024))
        self.assertIn('#8CE88C', style['text_colors'])

    def test_png_metadata_removed_and_original_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"generated.png", Path(directory)/"upload.jpg"
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("Software", "generator")
            metadata.add_text("XML:com.adobe.xmp", "private generation metadata")
            Image.new("RGB", (1024, 768), (23, 68, 95)).save(source, pnginfo=metadata)
            original = source.read_bytes()
            result = clean_export(source, target)
            self.assertEqual(source.read_bytes(), original)
            with Image.open(target) as image:
                self.assertEqual(image.size, (2048, 1536))
                self.assertFalse(image.getexif())
                self.assertNotIn("Software", image.info)
                self.assertNotIn("XML:com.adobe.xmp", image.info)
            self.assertTrue(result["metadata_stripped"])

    def test_exif_orientation_is_applied_before_metadata_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"original.jpg", Path(directory)/"upload.jpg"
            exif = Image.Exif()
            exif[274] = 6
            exif[305] = "image generator"
            Image.new("RGB", (800, 400), "blue").save(source, exif=exif)
            result = clean_export(source, target)
            self.assertEqual((result["width"], result["height"]), (1024, 2048))
            with Image.open(target) as image:
                self.assertEqual(dict(image.getexif()), {})

    def test_cannot_overwrite_generation_original(self):
        with self.assertRaises(ValueError):
            clean_export("same.png", "same.png")

    def test_korean_cover_copy_changes_only_delivery_pixels_and_has_no_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source,plain,cover=(Path(directory)/name for name in ('source.png','plain.jpg','cover.jpg'))
            Image.new('RGB',(1024,1024),(87,102,96)).save(source)
            original=source.read_bytes()
            clean_export(source,plain)
            result=clean_export(source,cover,headline='기부의 기준')
            self.assertTrue(result['cover_text_applied'])
            self.assertEqual(result['cover_text_color'], '#8CE88C')
            self.assertEqual(result['cover_text_colors'], ['#FFFFFF', '#8CE88C', '#FF4040'])
            self.assertEqual(result['cover_aspect_ratio'], '1:1')
            self.assertNotEqual(plain.read_bytes(),cover.read_bytes())
            self.assertEqual(original,source.read_bytes())
            with Image.open(cover) as picture:
                self.assertFalse(picture.getexif())
                self.assertEqual(picture.size,(2048,2048))

    def test_missing_korean_font_is_an_error_instead_of_broken_text(self):
        with tempfile.TemporaryDirectory() as directory:
            source,target=Path(directory)/'source.png',Path(directory)/'cover.jpg'
            Image.new('RGB',(1024,1024),'white').save(source)
            with self.assertRaisesRegex(ValueError,'한글 글꼴'):
                clean_export(source,target,headline='기부',font_path=Path(directory)/'missing.ttf')

    def test_cover_has_visible_displaced_shadow_on_dark_and_light_photos(self):
        # Inspect delivered pixels, not draw calls: a centered outline must
        # not pass as the requested shadow below the title.
        for headline in ('물가지표의 밤', '기부의 기준'):
            for background in ('black', 'white'):
                with self.subTest(headline=headline, background=background):
                    original = Image.new('RGB', (768, 768), background)
                    rendered, color = _draw_cover(original, headline)
                    foreground = ImageColor.getrgb(color)
                    title_y, shadow_y = [], []
                    for y in range(250, 520):
                        for x in range(768):
                            pixel = rendered.getpixel((x, y))
                            if pixel == foreground:
                                title_y.append(y)
                            if pixel == (5, 9, 12):
                                shadow_y.append(y)
                    self.assertTrue(title_y)
                    self.assertTrue(shadow_y)
                    self.assertGreaterEqual(max(shadow_y) - max(title_y), 7)
                    self.assertEqual(rendered.crop((0, 0, 768, 180)).tobytes(),
                                     original.crop((0, 0, 768, 180)).tobytes())
                    self.assertEqual(rendered.crop((0, 590, 768, 768)).tobytes(),
                                     original.crop((0, 590, 768, 768)).tobytes())

    def test_cover_text_is_centered_on_translucent_black_panel(self):
        original = Image.new('RGB', (512, 512), (100, 180, 240))
        rendered, color = _draw_cover(original, '왜 다를까?')
        foreground = ImageColor.getrgb(color)
        points = [(x, y) for y in range(512) for x in range(512)
                  if rendered.getpixel((x, y)) in {ImageColor.getrgb(value) for value in OVERLAY_TEXT_COLORS}]
        self.assertTrue(points)
        left, right = min(x for x, _ in points), max(x for x, _ in points)
        top, bottom = min(y for _, y in points), max(y for _, y in points)
        self.assertLessEqual(abs((left + right) / 2 - 256), 3)
        self.assertLessEqual(abs((top + bottom) / 2 - 256), 3)
        # Padding behind the type must blend black with the actual photo,
        # rather than replace it with an opaque gray or white rectangle.
        panel_y = next(y for y in range(256) if rendered.getpixel((256, y)) != original.getpixel((256, y)))
        self.assertEqual(rendered.getpixel((256, panel_y)), (45, 81, 108))
        self.assertEqual(rendered.getpixel((0, 0)), original.getpixel((0, 0)))

    def test_render_version_identifies_only_cover_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.png'
            Image.new('RGB', (256, 256), 'black').save(source)
            cover = clean_export(source, Path(directory) / 'cover.jpg', headline='물가지표의 밤')
            plain = clean_export(source, Path(directory) / 'plain.jpg')
            self.assertEqual(cover['cover_render_version'], COVER_RENDER_VERSION)
            self.assertEqual(COVER_RENDER_VERSION, 'center-question-overlay-v6')
            self.assertEqual(cover['cover_text_alignment'], 'center')
            self.assertEqual(cover['cover_panel_color'], '#000000')
            self.assertAlmostEqual(cover['cover_panel_opacity'], 140 / 255)
            self.assertEqual(plain['cover_render_version'], '')

    def test_cover_is_center_cropped_to_exact_square(self):
        with tempfile.TemporaryDirectory() as directory:
            source,target=Path(directory)/'wide.png',Path(directory)/'cover.jpg'
            Image.new('RGB',(1600,900),'gray').save(source)
            result=clean_export(source,target,headline='고르는 기준')
            self.assertEqual(result['width'],result['height'])
            self.assertEqual(result['width'],2048)

    def test_failed_export_preserves_existing_delivery_copy_and_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'source.png', Path(directory)/'upload.jpg'
            Image.new('RGB', (640, 480), 'red').save(source)
            Image.new('RGB', (640, 480), 'blue').save(target)
            original, approved_copy = source.read_bytes(), target.read_bytes()
            with patch.object(Image.Image, 'save', side_effect=OSError('disk full')), self.assertRaises(OSError):
                clean_export(source, target, target_long_side=640)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(target.read_bytes(), approved_copy)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_short_windows_replace_lock_is_retried_without_partial_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'source.png', Path(directory)/'upload.jpg'
            Image.new('RGB', (640, 480), 'red').save(source)
            previous = b'previous approved delivery'
            target.write_bytes(previous)
            replace = os.replace
            attempts = []
            def locked_then_replace(temporary, destination):
                self.assertEqual(Path(destination).read_bytes(), previous)
                attempts.append(temporary)
                if len(attempts) < 3:
                    raise PermissionError('scanner lock')
                return replace(temporary, destination)
            with patch('image_delivery.os.replace', side_effect=locked_then_replace), patch('image_delivery.time.sleep'):
                result = clean_export(source, target, target_long_side=640)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(result['width'], 640)
            self.assertNotEqual(target.read_bytes(), previous)

    def test_palette_transparency_renders_white_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'palette.png', Path(directory)/'upload.jpg'
            image = Image.new('P', (64, 64), 0)
            image.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
            image.save(source, transparency=0)
            original = source.read_bytes()
            clean_export(source, target, target_long_side=64)
            with Image.open(target) as rendered:
                self.assertEqual(rendered.getpixel((32, 32)), (255, 255, 255))
            self.assertEqual(source.read_bytes(), original)

    def test_cover_rejects_long_or_multiline_caption(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.png';Image.new('RGB',(1024,1024),'white').save(source)
            for headline in ('가' * 29, '두 줄\n문구'):
                with self.subTest(headline=headline), self.assertRaises(ValueError):
                    clean_export(source,Path(directory)/(str(len(headline))+'.jpg'),headline=headline)

    def test_long_question_wraps_whole_words_into_two_or_three_centered_lines(self):
        question = '필라델피아 반도체 지금 사도 되나?'
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'source.png', Path(directory)/'cover.jpg'
            Image.new('RGB', (1024, 1024), (70, 100, 125)).save(source)
            result = clean_export(source, target, headline=question, target_long_side=1024)
            self.assertIn(len(result['cover_text_lines']), (2, 3))
            self.assertEqual(' '.join(result['cover_text_lines']), question)
            self.assertTrue(result['cover_text_lines'][-1].endswith('?'))
            self.assertIn(len(result['cover_emphasis_words']), (1, 2))
            self.assertTrue(all(word in question for word in result['cover_emphasis_words']))
            self.assertEqual(result['cover_text_alignment'], 'center')
            self.assertGreater(result['cover_blur_radius'], 0)
            self.assertLessEqual(result['cover_blur_radius'], 3)

    def test_renderer_uses_real_white_and_green_pixels_without_red_selection(self):
        original = Image.new('RGB', (640, 640), (65, 85, 105))
        rendered, color = _draw_cover(original, '물가 내려도 주가 오를까?')
        colors = {value for _, value in rendered.getcolors(maxcolors=640 * 640)}
        self.assertEqual(color, '#8CE88C')
        self.assertIn((255, 255, 255), colors)
        self.assertIn((140, 232, 140), colors)
        self.assertNotIn((239, 51, 64), colors)

    def test_mild_blur_changes_background_detail_without_mutating_input(self):
        original = Image.new('RGB', (512, 512), (80, 100, 120))
        draw = ImageDraw.Draw(original)
        for x in range(0, 512, 4):
            draw.line((x, 0, x, 512), fill=(150, 170, 190))
        before = original.tobytes()
        rendered, _ = _draw_cover(original, '예약 언제 해야 할까?')
        self.assertEqual(original.tobytes(), before)
        self.assertIsNotNone(ImageChops.difference(original.crop((0, 0, 512, 50)),
                                                  rendered.crop((0, 0, 512, 50))).getbbox())

    def test_twenty_eight_character_headline_and_legacy_short_text_remain_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.png'
            Image.new('RGB', (512, 512), (65, 85, 105)).save(source)
            for headline in ('가' * 27 + '?', '기부의 기준'):
                result = clean_export(source, Path(directory)/'cover.jpg', headline=headline, target_long_side=512)
                self.assertEqual(result['cover_headline'], headline)
                self.assertLessEqual(len(result['cover_text_lines']), 3)


if __name__ == "__main__":
    unittest.main()
