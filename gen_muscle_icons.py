"""一次性腳本：生成 5 張肌肉剪影 PNG，給 Flex 訓練菜單卡用。

執行：uv run python gen_muscle_icons.py
輸出：muscles/back.png / chest.png / shoulders.png / legs.png / arms.png
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from PIL import Image, ImageDraw, ImageFont

OUTPUT_DIR = Path(__file__).parent / "muscles"
OUTPUT_DIR.mkdir(exist_ok=True)

W, H = 800, 600
BG = "#F5F5F5"
BODY_GRAY = "#C8CCD0"
BODY_DARK = "#9CA3AF"
HIGHLIGHT = "#3B82F6"  # 藍色高亮
HIGHLIGHT_DARK = "#1E40AF"


def _draw_base_body(draw: ImageDraw.ImageDraw) -> None:
    """畫一個基本的正面人形剪影（簡化幾何）。"""
    # 中軸：x=400
    # 頭
    draw.ellipse([(360, 60), (440, 150)], fill=BODY_GRAY, outline=BODY_DARK, width=3)
    # 脖子
    draw.rectangle([(385, 145), (415, 175)], fill=BODY_GRAY, outline=BODY_DARK, width=2)
    # 軀幹（梯形：上窄下窄、中間寬）
    draw.polygon([
        (320, 175), (480, 175),     # 肩膀
        (510, 230),                 # 右側肩寬
        (490, 380),                 # 右腰
        (450, 420),                 # 右臀
        (350, 420),                 # 左臀
        (310, 380),                 # 左腰
        (290, 230),                 # 左側肩寬
    ], fill=BODY_GRAY, outline=BODY_DARK, width=3)
    # 手臂（上臂）
    draw.polygon([
        (290, 220), (250, 240),
        (240, 350), (270, 360),
    ], fill=BODY_GRAY, outline=BODY_DARK, width=3)
    draw.polygon([
        (510, 220), (550, 240),
        (560, 350), (530, 360),
    ], fill=BODY_GRAY, outline=BODY_DARK, width=3)
    # 前臂
    draw.polygon([
        (240, 350), (270, 360),
        (290, 460), (260, 470),
    ], fill=BODY_GRAY, outline=BODY_DARK, width=3)
    draw.polygon([
        (530, 360), (560, 350),
        (540, 470), (510, 460),
    ], fill=BODY_GRAY, outline=BODY_DARK, width=3)
    # 腿（大腿）
    draw.polygon([
        (350, 420), (395, 420),
        (390, 530), (340, 540),
    ], fill=BODY_GRAY, outline=BODY_DARK, width=3)
    draw.polygon([
        (405, 420), (450, 420),
        (460, 540), (410, 530),
    ], fill=BODY_GRAY, outline=BODY_DARK, width=3)


def _highlight_back(draw: ImageDraw.ImageDraw) -> None:
    """背部：闊背肌 + 斜方肌（雖然是正面圖但用箭頭/區塊表示背肌位置）。"""
    # 上斜方（脖子兩側）
    draw.polygon([
        (350, 175), (450, 175),
        (430, 210), (370, 210),
    ], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)
    # 闊背（軀幹兩側下方）
    draw.polygon([
        (315, 250), (340, 250),
        (370, 380), (340, 400),
        (305, 350),
    ], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)
    draw.polygon([
        (460, 250), (485, 250),
        (495, 350), (460, 400),
        (430, 380),
    ], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)


def _highlight_chest(draw: ImageDraw.ImageDraw) -> None:
    """胸：左右胸大肌。"""
    # 左胸
    draw.polygon([
        (340, 195), (395, 195),
        (390, 280), (335, 270),
    ], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)
    # 右胸
    draw.polygon([
        (405, 195), (460, 195),
        (465, 270), (410, 280),
    ], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)


def _highlight_shoulders(draw: ImageDraw.ImageDraw) -> None:
    """肩：三角肌前束/中束。"""
    # 左肩
    draw.ellipse([(275, 185), (340, 250)], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)
    # 右肩
    draw.ellipse([(460, 185), (525, 250)], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)


def _highlight_legs(draw: ImageDraw.ImageDraw) -> None:
    """腿：股四頭肌（大腿前側）。"""
    # 左大腿
    draw.polygon([
        (352, 425), (393, 425),
        (388, 525), (345, 535),
    ], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)
    # 右大腿
    draw.polygon([
        (407, 425), (448, 425),
        (455, 535), (412, 525),
    ], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)


def _highlight_arms(draw: ImageDraw.ImageDraw) -> None:
    """手臂：肱二頭肌 + 肱三頭肌（上臂）。"""
    # 左上臂
    draw.polygon([
        (293, 225), (255, 245),
        (245, 345), (273, 355),
    ], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)
    # 右上臂
    draw.polygon([
        (507, 225), (545, 245),
        (555, 345), (527, 355),
    ], fill=HIGHLIGHT, outline=HIGHLIGHT_DARK, width=2)


HIGHLIGHTS = {
    "back":      _highlight_back,
    "chest":     _highlight_chest,
    "shoulders": _highlight_shoulders,
    "legs":      _highlight_legs,
    "arms":      _highlight_arms,
}


def _find_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


LABEL_FONT = _find_font([
    r"C:\Windows\Fonts\msjhbd.ttc",
    r"C:\Windows\Fonts\msjh.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
], 60)


def make_muscle_png(name: str, label: str) -> Path:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    _draw_base_body(draw)
    HIGHLIGHTS[name](draw)
    # 標籤（底部置中）
    bbox = draw.textbbox((0, 0), label, font=LABEL_FONT)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) // 2 - bbox[0], H - 80),
              label, font=LABEL_FONT, fill=HIGHLIGHT_DARK)

    out = OUTPUT_DIR / f"{name}.png"
    img.save(out, "PNG", optimize=True)
    return out


def main() -> None:
    print("=" * 50)
    print("🏋️  生成肌肉剪影 PNG")
    print("=" * 50)
    for name, label in [
        ("back",      "背"),
        ("chest",     "胸"),
        ("shoulders", "肩"),
        ("legs",      "腿"),
        ("arms",      "手臂"),
    ]:
        path = make_muscle_png(name, label)
        size_kb = path.stat().st_size // 1024
        print(f"  ✅ {path.name} ({size_kb} KB)")
    print()
    print(f"🎉 5 張圖存到 {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
