"""一次性腳本：生成 AI 教練專用頭貼 PNG。

執行：uv run python gen_coach_avatar.py
輸出：avatars/ai_coach.png
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from PIL import Image, ImageDraw, ImageFont

OUTPUT_DIR = Path(__file__).parent / "avatars"
OUTPUT_DIR.mkdir(exist_ok=True)

SIZE = 400  # LINE 會顯示成圓形，400x400 就很清晰
BG = "#FF6B35"  # 主橘
RING = "#E55B25"  # 深橘外框


def _find_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


EMOJI_FONT = _find_font([
    r"C:\Windows\Fonts\seguiemj.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
], 240)


def _paste_centered_emoji(canvas: Image.Image, emoji: str, cx: int, cy: int) -> None:
    """用實際渲染像素 bbox 置中（修彩色 emoji bearing 偏移）。"""
    pad = 60
    side = 600
    temp = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    temp_draw = ImageDraw.Draw(temp)
    try:
        temp_draw.text((pad, pad), emoji, font=EMOJI_FONT, embedded_color=True)
    except Exception:
        temp_draw.text((pad, pad), emoji, font=EMOJI_FONT,
                       fill=(255, 255, 255, 255))
    real_bbox = temp.getbbox()
    if real_bbox is None:
        return
    cropped = temp.crop(real_bbox)
    paste_x = cx - cropped.width // 2
    paste_y = cy - cropped.height // 2
    canvas.paste(cropped, (paste_x, paste_y), cropped)


def make_avatar() -> Path:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 大圓底
    margin = 8
    draw.ellipse([margin, margin, SIZE - margin, SIZE - margin],
                 fill=BG, outline=RING, width=4)
    # 中央放機器人
    _paste_centered_emoji(img, "🤖", SIZE // 2, SIZE // 2 - 5)

    out = OUTPUT_DIR / "ai_coach.png"
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


def main() -> None:
    print("=" * 50)
    print("🤖 生成 AI 教練頭貼")
    print("=" * 50)
    path = make_avatar()
    print(f"  ✅ {path.name} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
