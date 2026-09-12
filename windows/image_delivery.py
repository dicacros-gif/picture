"""Create metadata-free upload copies while retaining provenance in the run manifest."""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps


def clean_export(source: str | Path, destination: str | Path, *, target_long_side: int = 2048) -> dict:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("생성 원본과 업로드 사본 경로는 달라야 합니다.")
    with Image.open(source) as original:
        oriented = ImageOps.exif_transpose(original)
        if "A" in oriented.getbands():
            canvas = Image.new("RGBA", oriented.size, "white")
            canvas.alpha_composite(oriented.convert("RGBA"))
            pixels = canvas.convert("RGB")
        else:
            pixels = oriented.convert("RGB")
        if max(pixels.size) < target_long_side:
            scale = target_long_side / max(pixels.size)
            pixels = pixels.resize((round(pixels.width * scale), round(pixels.height * scale)), Image.Resampling.LANCZOS)
        # New image from pixels: no EXIF, XMP, ICC, PNG text, software or original provenance chunks.
        clean = Image.frombytes("RGB", pixels.size, pixels.tobytes())
        destination.parent.mkdir(parents=True, exist_ok=True)
        clean.save(destination, format="JPEG", quality=95, subsampling=0, optimize=True)
    with Image.open(destination) as verified:
        if verified.getexif() or any(key.lower() in {"exif", "xmp", "icc_profile", "software", "comment"} for key in verified.info):
            raise RuntimeError("업로드 사본의 메타데이터 정리를 확인하지 못했습니다.")
        width, height = verified.size
    return {"path": str(destination), "original_path": str(source), "width": width, "height": height,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "metadata_stripped": True,
            "delivery_format": "JPEG", "image_style": "photorealistic"}
