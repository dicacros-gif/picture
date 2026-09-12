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


if __name__ == "__main__":
    unittest.main()
