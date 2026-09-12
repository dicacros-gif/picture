import tempfile
import unittest
from pathlib import Path

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
            result=clean_export(source,cover,headline='기부\n무엇부터 확인할까요?')
            self.assertTrue(result['cover_text_applied'])
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


if __name__ == "__main__":
    unittest.main()
