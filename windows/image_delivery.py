"""Create metadata-free upload copies while retaining provenance in the run manifest."""
from __future__ import annotations

import hashlib
from itertools import combinations
import os
import re
import tempfile
import time
import unicodedata
import sys
from pathlib import Path

from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter, ImageChops

COVER_RENDER_VERSION = "center-question-overlay-v6"
CAPTION_RENDER_VERSION = "center-question-overlay-v3"
# White keeps complete sentences readable. Fluorescent green and red mark
# different hook words so both generated covers and Google captures have a
# clear hierarchy on the translucent black panel.
OVERLAY_TEXT_COLORS = ("#FFFFFF", "#8CE88C", "#FF4040")


def _default_korean_font() -> Path:
    if sys.platform == "darwin":
        for value in ("/System/Library/Fonts/AppleSDGothicNeo.ttc", "/Library/Fonts/AppleGothic.ttf"):
            if Path(value).is_file():
                return Path(value)
    return Path("C:/Windows/Fonts/malgunbd.ttf")


def _validated_caption(caption: str) -> str:
    if not isinstance(caption, str):
        raise ValueError("이미지 설명은 한글을 포함한 28자 이내의 한 줄이어야 합니다.")
    if not caption:
        return ""
    caption = unicodedata.normalize("NFC", caption)
    if (len(caption) > 28 or not re.search(r"[가-힣]", caption)
            or any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in caption)
            or re.search(r"[a-z][a-z0-9+.-]*://|www\.|[a-z0-9가-힣-]+\.(?:[a-z]{2,}|한국)", caption, re.IGNORECASE)):
        raise ValueError("이미지 설명은 공백을 포함해 28자 이내이며 한글을 포함해야 합니다. 줄바꿈과 URL은 사용할 수 없습니다.")
    return caption.strip()


def _validated_headline(headline: str) -> str:
    if not isinstance(headline, str):
        raise ValueError("첫 사진의 한글 문구가 올바르지 않습니다.")
    headline = unicodedata.normalize("NFC", headline)
    if (not headline.strip() or len(headline) > 28
            or any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in headline)):
        raise ValueError("첫 사진의 한글 문구는 공백을 포함해 28자 이내의 한 줄이어야 합니다.")
    # Existing short captions remain valid; new-writing prompts require a question.
    return headline.strip()


def _trim_uniform_margins(pixels: Image.Image) -> tuple[Image.Image, dict]:
    """Trim only thin, symmetric, nearly exact white/black exterior margins.

    The bounding box retains every pixel differing from the border color, so
    lettering or a logo inside the photo can never be removed by this helper.
    Wide, asymmetric, colored and photographic backgrounds remain untouched.
    """
    width, height = pixels.size
    full = (0, 0, width, height)
    color = pixels.getpixel((0, 0))
    box = full
    if max(color) <= 5 or min(color) >= 250:
        difference = ImageChops.difference(pixels, Image.new("RGB", pixels.size, color))
        bounds = difference.point(lambda value: 255 if value > 2 else 0).getbbox()
        if bounds:
            left, top, right, bottom = bounds
            margins = (left, top, width - right, height - bottom)
            def symmetric(a, b, extent):
                return (2 <= a <= extent * .08 and 2 <= b <= extent * .08
                        and abs(a - b) <= max(2, min(a, b) * .25))
            horizontal = symmetric(margins[0], margins[2], width)
            vertical = symmetric(margins[1], margins[3], height)
            if horizontal or vertical:
                box = (left if horizontal else 0, top if vertical else 0,
                       right if horizontal else width, bottom if vertical else height)
    return (pixels.crop(box) if box != full else pixels), {
        "caption_original_size": [width, height], "caption_trim_box": list(box),
        "caption_margin_trimmed": box != full}


def _line_layout(headline, drawing, font, maximum_width, maximum_height, counts=None):
    words = headline.split()
    candidates = []
    counts = counts or ((2, 3) if len(words) >= 2 else (1,))
    for count in counts:
        if count > len(words):
            continue
        for breaks in combinations(range(1, len(words)), count - 1):
            edges = (0, *breaks, len(words))
            lines = [" ".join(words[edges[index]:edges[index + 1]]) for index in range(count)]
            widths = [drawing.textlength(line, font=font) for line in lines]
            heights = [drawing.textbbox((0, 0), line, font=font)[3] - drawing.textbbox((0, 0), line, font=font)[1] for line in lines]
            spacing = max(5, round(font.size * .24))
            total_height = max(heights) * count + spacing * (count - 1)
            if max(widths) <= maximum_width and total_height <= maximum_height:
                imbalance = (max(widths) - min(widths)) / max(widths)
                # Prefer two balanced lines, keeping whole Korean word groups.
                dangling = sum(line.split()[-1] in {'지금', '언제', '어떻게', '왜', '다시', '정말', '언제까지'} for line in lines[:-1])
                score = imbalance + .14 * max(0, count - 2) + .7 * dangling
                candidates.append((score, lines, max(heights), spacing))
    if candidates:
        return min(candidates, key=lambda item: item[0])[1:]
    return None


def _overlay_layout(pixels, headline, font_path):
    width, height = pixels.size
    drawing = ImageDraw.Draw(pixels)
    short = len(headline) <= 12
    start = max(24, round(min(width, height) * (.17 if short else .14)))
    # Prefer a readable two-line phrase over three oversized lines that leave
    # an adverb ("지금", "언제") attached to the wrong meaning group.
    counts = ((1, 2) if short else (2, 3)) if len(headline.split()) >= 2 else (1,)
    for count in counts:
        minimum = max(20, round(min(width, height) * .085)) if count == 2 else 20
        for size in range(start, minimum - 1, -2):
            font = ImageFont.truetype(str(font_path), size)
            layout = _line_layout(headline, drawing, font, width * .86, height * .48, counts=(count,))
            if layout:
                return font, *layout
    # Only an exceptionally long unspaced word needs character wrapping. All
    # normal spaced questions above retain each word and its original spelling.
    for size in range(start, 19, -2):
        font = ImageFont.truetype(str(font_path), size)
        lines, current = [], ""
        for char in headline:
            if current and drawing.textlength(current + char, font=font) > width * .86:
                lines.append(current.rstrip())
                current = char.lstrip()
            else:
                current += char
        if current:
            lines.append(current.rstrip())
        if not 1 <= len(lines) <= 3:
            continue
        line_height = max(drawing.textbbox((0, 0), line, font=font)[3] - drawing.textbbox((0, 0), line, font=font)[1] for line in lines)
        spacing = max(5, round(size * .24))
        if line_height * len(lines) + spacing * (len(lines) - 1) <= height * .48:
            return font, lines, line_height, spacing
    raise ValueError("사진 문구를 읽기 쉬운 크기의 2~3줄로 배치할 수 없습니다.")


def _emphasis_words(headline, lines):
    words = [word.rstrip('?!.,') for word in headline.split()]
    quiet = {'왜', '어떻게', '무엇', '언제', '지금', '다시', '정말', '어디', '누가', '할까', '될까', '되나', '있을까', '가능할까'}
    eligible = [index for index, word in enumerate(words) if word and word not in quiet]
    if not eligible:
        eligible = list(range(len(words)))
    center = (len(words) - 1) / 2
    chosen = sorted(eligible, key=lambda index: (abs(index - center), index))[:2 if len(words) >= 3 else 1]
    emphasis = [words[index] for index in sorted(chosen)]
    emphasis = [word for word in emphasis if any(word in line for line in lines)]
    if not emphasis:
        emphasis = [lines[len(lines) // 2].rstrip('?!.,')]
    return list(dict.fromkeys(emphasis))


def _draw_question_overlay(pixels, headline, font_path=None):
    font_path = Path(font_path) if font_path else _default_korean_font()
    if not font_path.is_file():
        raise ValueError("사진 한글 문구에 필요한 한글 글꼴을 찾지 못했습니다.")
    headline = _validated_headline(headline)
    font, lines, line_height, spacing = _overlay_layout(pixels, headline, font_path)
    width, height = pixels.size
    margin = max(12, round(min(width, height) * .065))
    text_height = line_height * len(lines) + spacing * (len(lines) - 1)
    emphasis = _emphasis_words(headline, lines)
    radius = round(max(.6, min(3.0, min(width, height) * .002)), 2)
    background = pixels.filter(ImageFilter.GaussianBlur(radius=radius))
    overlay = Image.new("RGBA", pixels.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    text_width = max(draw.textlength(line, font=font) for line in lines)
    panel_width, panel_height = min(width, text_width + margin * 2), min(height, text_height + margin * 2)
    panel_left, panel_top = (width - panel_width) / 2, (height - panel_height) / 2
    draw.rectangle((round(panel_left), round(panel_top), round(panel_left + panel_width), round(panel_top + panel_height)), fill=(0, 0, 0, 140))
    shadow_x, shadow_y = max(3, round(font.size * .055)), max(7, round(font.size * .14))
    stroke = max(2, round(font.size * .045))
    for index, line in enumerate(lines):
        bounds = draw.textbbox((0, 0), line, font=font)
        glyph_width, glyph_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        x = (width - glyph_width) / 2 - bounds[0]
        y = (height - text_height) / 2 + index * (line_height + spacing) + (line_height - glyph_height) / 2 - bounds[1]
        draw.text((x + shadow_x, y + shadow_y), line, font=font, fill=(5, 9, 12, 255),
                  stroke_width=max(1, round(font.size * .018)), stroke_fill=(5, 9, 12, 255))
        draw.text((x, y), line, font=font, fill=OVERLAY_TEXT_COLORS[0], stroke_width=stroke, stroke_fill=(0, 0, 0, 245))
        for emphasis_index, word in enumerate(emphasis):
            for match in re.finditer(re.escape(word), line):
                prefix_width = draw.textlength(line[:match.start()], font=font)
                accent = OVERLAY_TEXT_COLORS[1 + emphasis_index % (len(OVERLAY_TEXT_COLORS) - 1)]
                draw.text((x + prefix_width, y), word, font=font, fill=accent,
                          stroke_width=stroke, stroke_fill=(0, 0, 0, 245))
    return Image.alpha_composite(background.convert("RGBA"), overlay).convert("RGB"), {
        "text_color": OVERLAY_TEXT_COLORS[1], "text_colors": list(OVERLAY_TEXT_COLORS),
        "text_lines": lines, "emphasis_words": emphasis, "font": font_path.name,
        "blur_radius": radius, "panel_color": "#000000", "panel_opacity": 140 / 255,
        "typography_policy": "gothic-short-hook-v1", "text_alignment": "center", "placement": "center"}


def _draw_caption(pixels: Image.Image, caption: str, font_path: str | Path | None = None) -> tuple[Image.Image, dict]:
    """Render the same readable central overlay without adding a separate band."""
    caption = _validated_caption(caption)
    result, style = _draw_question_overlay(pixels, caption, font_path)
    return result, {**{'caption_' + key: value for key, value in style.items()},
        "caption_band_height": 0, "caption_background_color": "#000000",
        "caption_layout": "center_overlay", "caption_render_version": CAPTION_RENDER_VERSION}


def _draw_cover(pixels: Image.Image, headline: str, font_path: str | Path | None = None) -> tuple[Image.Image, str]:
    result, style = _draw_question_overlay(pixels, headline, font_path)
    return result, style['text_color']


def clean_export(source: str | Path, destination: str | Path, *, target_long_side: int = 2048,
                 headline: str = "", font_path: str | Path | None = None, caption: str = "") -> dict:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("생성 원본과 업로드 사본 경로는 달라야 합니다.")
    caption = _validated_caption(caption)
    if headline:
        headline = _validated_headline(headline)
    if headline and caption:
        raise ValueError("첫 사진 후킹 문구와 참고 이미지 설명은 동시에 적용할 수 없습니다.")
    caption_style = {"caption_band_height": 0, "caption_text_color": "", "caption_background_color": "", "caption_font": ""}
    cover_style = {}
    trim_style = {"caption_original_size": [], "caption_trim_box": [], "caption_margin_trimmed": False}
    with Image.open(source) as original:
        oriented = ImageOps.exif_transpose(original)
        if "A" in oriented.getbands() or "transparency" in oriented.info:
            canvas = Image.new("RGBA", oriented.size, "white")
            canvas.alpha_composite(oriented.convert("RGBA"))
            pixels = canvas.convert("RGB")
        else:
            pixels = oriented.convert("RGB")
        if caption:
            pixels, trim_style = _trim_uniform_margins(pixels)
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
            pixels, style = _draw_question_overlay(pixels, headline, font_path)
            cover_color = style['text_color']
            cover_style = {'cover_' + key: value for key, value in style.items()}
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
            "cover_render_version": COVER_RENDER_VERSION if headline else "",
            "cover_text_color": cover_color, "cover_aspect_ratio": "1:1" if headline else "",
            "cover_text_alignment": "center" if headline else "",
            "cover_panel_color": "#000000" if headline else "",
            "cover_panel_opacity": 140 / 255 if headline else 0,
            "cover_placement": "center" if headline else "",
            "caption_text": caption, "caption_applied": bool(caption),
            "caption_placement": "center" if caption else "", "caption_layout": "center_overlay" if caption else "",
            "caption_render_version": CAPTION_RENDER_VERSION if caption else "",
            **cover_style, **caption_style, **trim_style}
