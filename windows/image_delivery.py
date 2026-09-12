"""Create metadata-free upload copies while retaining provenance in the run manifest."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path

from PIL import Image, ImageOps, ImageDraw, ImageFont


def _validated_caption(caption: str) -> str:
    if not isinstance(caption, str):
        raise ValueError("이미지 설명은 한글을 포함한 10자 이내의 한 줄이어야 합니다.")
    if not caption:
        return ""
    caption = unicodedata.normalize("NFC", caption)
    if (len(caption) > 10 or not re.search(r"[가-힣]", caption)
            or any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in caption)
            or re.search(r"[a-z][a-z0-9+.-]*://|www\.|[a-z0-9가-힣-]+\.(?:[a-z]{2,}|한국)", caption, re.IGNORECASE)):
        raise ValueError("이미지 설명은 공백을 포함해 10자 이내이며 한글을 포함해야 합니다. 줄바꿈과 URL은 사용할 수 없습니다.")
    return caption.strip()


def _draw_caption(pixels: Image.Image, caption: str, font_path: str | Path | None = None) -> tuple[Image.Image, dict]:
    """Add a separate top band; never cover or crop pixels from the reference photo."""
    font_path = Path(font_path or "C:/Windows/Fonts/malgunbd.ttf")
    if not font_path.is_file():
        raise ValueError("이미지 한글 설명에 필요한 한글 글꼴을 찾지 못했습니다.")
    width = pixels.width
    padding = max(12, round(width * .016))
    maximum_width = width - padding * 2
    size = max(24, round(width * .045))
    drawing = ImageDraw.Draw(pixels)
    while size >= 16:
        font = ImageFont.truetype(str(font_path), size)
        bounds = drawing.textbbox((0, 0), caption, font=font)
        text_width, text_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        if text_width <= maximum_width:
            break
        size -= 1
    else:
        raise ValueError("이미지 폭이 좁아 한글 설명을 읽기 쉬운 크기로 배치할 수 없습니다.")
    band_height = text_height + padding * 2
    background, color = "#0C161D", "#F8FAFC"
    result = Image.new("RGB", (width, pixels.height + band_height), background)
    result.paste(pixels, (0, band_height))
    draw = ImageDraw.Draw(result)
    draw.text(((width - text_width) // 2 - bounds[0], padding - bounds[1]), caption, font=font, fill=color)
    return result, {"caption_band_height": band_height, "caption_text_color": color,
                    "caption_background_color": background, "caption_font": font_path.name}


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
                 headline: str = "", font_path: str | Path | None = None, caption: str = "") -> dict:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("생성 원본과 업로드 사본 경로는 달라야 합니다.")
    caption = _validated_caption(caption)
    if headline and caption:
        raise ValueError("첫 사진 후킹 문구와 참고 이미지 설명은 동시에 적용할 수 없습니다.")
    caption_style = {"caption_band_height": 0, "caption_text_color": "", "caption_background_color": "", "caption_font": ""}
    with Image.open(source) as original:
        oriented = ImageOps.exif_transpose(original)
        if "A" in oriented.getbands() or "transparency" in oriented.info:
            canvas = Image.new("RGBA", oriented.size, "white")
            canvas.alpha_composite(oriented.convert("RGBA"))
            pixels = canvas.convert("RGB")
        else:
            pixels = oriented.convert("RGB")
        # Crop the cover first, then size the delivered square. Resizing the
        # landscape source first left a requested 2048px cover only 1152px wide.
        if headline:
            side = min(pixels.size)
            left, top = (pixels.width - side) // 2, (pixels.height - side) // 2
            pixels = pixels.crop((left, top, left + side, top + side))
        if max(pixels.size) < target_long_side:
            scale = target_long_side / max(pixels.size)
            pixels = pixels.resize((round(pixels.width * scale), round(pixels.height * scale)), Image.Resampling.LANCZOS)
        cover_color = ""
        if headline:
            pixels, cover_color = _draw_cover(pixels, headline, font_path)
        if caption:
            pixels, caption_style = _draw_caption(pixels, caption, font_path)
        # Retain the source file and generation history separately from the upload copy.
        clean = Image.frombytes("RGB", pixels.size, pixels.tobytes())
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            clean.save(temporary, format="JPEG", quality=95, subsampling=0, optimize=True)
            with Image.open(temporary) as verified:
                verified.load()
                if verified.getexif() or any(key.lower() in {"exif", "xmp", "icc_profile", "software", "comment"} for key in verified.info):
                    raise RuntimeError("업로드 사본의 메타데이터 정리를 확인하지 못했습니다.")
                width, height = verified.size
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            for attempt in range(5):
                try:
                    os.replace(temporary, destination)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(.05 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)
    return {"path": str(destination), "original_path": str(source), "width": width, "height": height,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "metadata_stripped": True,
            "delivery_format": "JPEG", "image_style": "photorealistic",
            "cover_headline": headline, "cover_text_applied": bool(headline),
            "cover_text_color": cover_color, "cover_aspect_ratio": "1:1" if headline else "",
            "caption_text": caption, "caption_applied": bool(caption),
            "caption_placement": "top" if caption else "", "caption_layout": "separate_band" if caption else "",
            **caption_style}
