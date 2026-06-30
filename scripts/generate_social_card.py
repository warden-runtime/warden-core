#!/usr/bin/env python3
"""Generate Warden OG/social card (1200x630) from brand assets."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "website/static/img/warden-social-card.png"
LOGO = ROOT / "website/static/img/warden-logo.png"
FONT_BOLD = Path("/usr/share/fonts/noto/NotoSans-Bold.ttf")
FONT_REG = Path("/usr/share/fonts/noto/NotoSans-Regular.ttf")

W, H = 1200, 630
SIDE_MARGIN = 48
LOGO_GAP = 68
TITLE_SUB_GAP = 24

# Brand tokens — website/src/css/custom.css
SURFACE = (11, 15, 25)  # --warden-surface #0b0f19
SURFACE_CANVAS = (22, 29, 47)  # --warden-surface-canvas #161d2f
BLUE_DEEP = (0, 67, 102)  # --warden-blue-deep #004366
SUBTITLE = (226, 232, 240)  # --ifm-font-color-base (dark theme)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient_135(width: int, height: int) -> Image.Image:
    """135° dark brand gradient — surface palette with blue-deep accent (custom.css)."""
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            t = (x + y) / (width + height)
            if t < 0.45:
                color = _lerp(SURFACE, SURFACE_CANVAS, t / 0.45)
            else:
                color = _lerp(SURFACE_CANVAS, BLUE_DEEP, (t - 0.45) / 0.55)
            px[x, y] = color
    return img


def _logo_content_center(logo: Image.Image) -> tuple[float, float]:
    bbox = logo.getbbox() or (0, 0, logo.width, logo.height)
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def main() -> None:
    base = gradient_135(W, H)

    logo = Image.open(LOGO).convert("RGBA")
    logo_h = 240
    logo_w = int(logo.width * (logo_h / logo.height))
    logo = logo.resize((logo_w, logo_h), Image.Resampling.LANCZOS)

    title = "Warden"
    subtitle = "The Postgres-native runtime that keeps AI agents honest."

    font_title = ImageFont.truetype(str(FONT_BOLD), 88)
    font_sub = ImageFont.truetype(str(FONT_REG), 28)

    probe = ImageDraw.Draw(base)
    title_box = probe.textbbox((0, 0), title, font=font_title)
    title_w = title_box[2] - title_box[0]
    title_h = title_box[3] - title_box[1]
    sub_box = probe.textbbox((0, 0), subtitle, font=font_sub)
    sub_w = sub_box[2] - sub_box[0]
    sub_h = sub_box[3] - sub_box[1]

    max_text_w = W - logo_w - LOGO_GAP - 2 * SIDE_MARGIN
    while sub_w > max_text_w and font_sub.size > 22:
        font_sub = ImageFont.truetype(str(FONT_REG), font_sub.size - 1)
        sub_box = probe.textbbox((0, 0), subtitle, font=font_sub)
        sub_w = sub_box[2] - sub_box[0]
        sub_h = sub_box[3] - sub_box[1]

    block_w = logo_w + LOGO_GAP + max(title_w, sub_w)
    start_x = max(SIDE_MARGIN, (W - block_w) // 2)
    logo_x = start_x
    text_x = start_x + logo_w + LOGO_GAP
    text_block_h = title_h + TITLE_SUB_GAP + sub_h
    block_center_y = H // 2
    text_y = int(block_center_y - text_block_h / 2)

    _content_cx, content_cy = _logo_content_center(logo)
    logo_y = int(block_center_y - content_cy)

    base.paste(logo, (logo_x, logo_y), logo)

    draw = ImageDraw.Draw(base)
    draw.text((text_x, text_y), title, fill=(255, 255, 255), font=font_title)
    draw.text((text_x, text_y + title_h + TITLE_SUB_GAP), subtitle, fill=SUBTITLE, font=font_sub)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    base.save(OUT, "PNG", optimize=True)
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
