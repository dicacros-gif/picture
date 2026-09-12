"""Create metadata-free upload copies while retaining provenance in the run manifest."""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps, ImageDraw, ImageFont


def _draw_cover(pixels: Image.Image, headline: str, font_path: str | Path | None = None) -> tuple[Image.Image, str]:
    font_path = Path(font_path or "C:/Windows/Fonts/malgunbd.ttf")
    if not font_path.is_file():
        raise ValueError("첫 사진의 한글 문구에 필요한 한글 글꼴을 찾지 못했습니다.")
    if not headline.strip() or len(headline) > 12 or "\n" in headline:
        raise ValueError("첫 사진의 한글 문구가 비어 있거나 너무 깁니다.")
    width, height = pixels.size
    margin, maximum_width = round(width * .065), round(width * .86)
    size = max(24, round(min(width, height) * .095))
    drawing = ImageDraw.Draw(pixels)
    while True:
        font = ImageFont.truetype(str(font_path), size)
        lines = []
        for original in headline.splitlines():
            current = ""
            for char in original:
                if current and drawing.textlength(current + char, font=font) > maximum_width:
                    lines.append(current.strip()); current = char
                else:
                    current += char
            lines.append(current.strip())
        spacing = round(size * .3)
        text = "\n".join(lines)
        bounds = drawing.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
        text_height = bounds[3] - bounds[1]
        if text_height < height * .34:
            break
        size -= 2
        if size < 20:
            raise ValueError("첫 사진 문구를 읽기 쉬운 크기로 배치할 수 없습니다.")
    overlay = Image.new("RGBA", pixels.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    panel_height = text_height + margin * 2
    draw.rectangle((0, 0, width, panel_height), fill=(12, 22, 29, 150))
    text_color = "#8CE88C" if int(hashlib.sha256(headline.encode("utf-8")).hexdigest(), 16) % 2 == 0 else "#EF3340"
    draw.multiline_text((margin, margin - bounds[1]), text, font=font, spacing=spacing,
                        fill=text_color, stroke_width=max(2, round(size * .035)), stroke_fill=(20, 25, 30, 210))
    return Image.alpha_composite(pixels.convert("RGBA"), overlay).convert("RGB"), text_color


def clean_export(source: str | Path, destination: str | Path, *, target_long_side: int = 2048,
                 headline: str = "", font_path: str | Path | None = None) -> dict:
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
        cover_color = ""
        if headline:
            side = min(pixels.size)
            left, top = (pixels.width - side) // 2, (pixels.height - side) // 2
            pixels = pixels.crop((left, top, left + side, top + side))
            pixels, cover_color = _draw_cover(pixels, headline, font_path)
        # Retain the source file and generation history separately from the upload copy.
        clean = Image.frombytes("RGB", pixels.size, pixels.tobytes())
        destination.parent.mkdir(parents=True, exist_ok=True)
        clean.save(destination, format="JPEG", quality=95, subsampling=0, optimize=True)
    with Image.open(destination) as verified:
        if verified.getexif() or any(key.lower() in {"exif", "xmp", "icc_profile", "software", "comment"} for key in verified.info):
            raise RuntimeError("업로드 사본의 메타데이터 정리를 확인하지 못했습니다.")
        width, height = verified.size
    return {"path": str(destination), "original_path": str(source), "width": width, "height": height,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "metadata_stripped": True,
            "delivery_format": "JPEG", "image_style": "photorealistic",
            "cover_headline": headline, "cover_text_applied": bool(headline),
            "cover_text_color": cover_color, "cover_aspect_ratio": "1:1" if headline else ""}
