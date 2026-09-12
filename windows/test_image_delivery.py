import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin
from image_delivery import clean_export


class ImageDeliveryTests(unittest.TestCase):
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
            self.assertIn(result['cover_text_color'], {'#8CE88C', '#EF3340'})
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
            for headline in ('열세글자이상으로너무긴후킹문구입니다','두 줄\n문구'):
                with self.subTest(headline=headline), self.assertRaises(ValueError):
                    clean_export(source,Path(directory)/(str(len(headline))+'.jpg'),headline=headline)


if __name__ == "__main__":
    unittest.main()
