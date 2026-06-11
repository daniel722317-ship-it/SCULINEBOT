"""一次性腳本：建立 LINE Rich Menu 並設為預設。

執行：
    uv run python setup_rich_menu.py

需要的環境變數（會自動從 .env 載入）：
    LINE_CHANNEL_ACCESS_TOKEN
"""
import os
import sys
from pathlib import Path

# Windows 終端機預設 cp950，print emoji 會炸。強制走 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# --- 載入 .env ---
ENV_PATH = Path(__file__).parent / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
if not TOKEN:
    print("❌ LINE_CHANNEL_ACCESS_TOKEN 沒設好，請檢查 .env")
    sys.exit(1)

# --- 依賴 ---
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("❌ 需要 Pillow：uv pip install Pillow")
    sys.exit(1)

import requests
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessageAction,
    MessagingApi,
    RichMenuArea,
    RichMenuBounds,
    RichMenuRequest,
    RichMenuSize,
)

# ============================================================
# 1. 畫 PNG
# ============================================================

WIDTH, HEIGHT = 2500, 1686
CELL_W = WIDTH // 3
CELL_H = HEIGHT // 2

# (row, col, 背景色, emoji, 中文標籤, 點擊送出文字)
CELLS = [
    (0, 0, "#6C757D", "📋", "選單", "選單"),
    (0, 1, "#A8D936", "🏋️", "運動", "運動"),
    (0, 2, "#FF6B35", "✅", "打卡", "打卡"),
    (1, 0, "#E55B25", "🎯", "目標", "我的目標"),
    (1, 1, "#5DADE2", "📅", "日曆", "日曆"),
    (1, 2, "#7B68EE", "⚙️", "設定", "功能設定"),
]


def _find_font(candidates, size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    print(f"⚠️  找不到字型，fallback 用 default（size={size}）")
    return ImageFont.load_default()


LABEL_FONT = _find_font([
    r"C:\Windows\Fonts\msjhbd.ttc",
    r"C:\Windows\Fonts\msjh.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
], 180)

EMOJI_FONT = _find_font([
    r"C:\Windows\Fonts\seguiemj.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
], 280)


def _paste_centered_emoji(canvas: Image.Image, emoji: str,
                          cx: int, top_y: int) -> None:
    """把彩色 emoji 真實渲染後，用實際像素 bbox 水平置中貼上。

    為什麼不用 draw.textbbox 直接算：彩色 emoji（如 🏋️ ⚙️）的字型
    bbox 跟實際渲染像素有 bearing 偏差，直接套公式會偏右。
    """
    pad = 60
    side = 500
    temp = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    temp_draw = ImageDraw.Draw(temp)
    try:
        temp_draw.text((pad, pad), emoji, font=EMOJI_FONT, embedded_color=True)
    except Exception:
        temp_draw.text((pad, pad), emoji, font=EMOJI_FONT, fill=(255, 255, 255, 255))
    real_bbox = temp.getbbox()
    if real_bbox is None:
        return
    cropped = temp.crop(real_bbox)
    paste_x = cx - cropped.width // 2
    canvas.paste(cropped, (paste_x, top_y), cropped)


def draw_image() -> Path:
    img = Image.new("RGBA", (WIDTH, HEIGHT), (255, 248, 240, 255))  # 暖白底
    draw = ImageDraw.Draw(img)

    gap = 16
    for (row, col, color, emoji, label, _) in CELLS:
        x0 = col * CELL_W + gap
        y0 = row * CELL_H + gap
        x1 = (col + 1) * CELL_W - gap
        y1 = (row + 1) * CELL_H - gap

        # 圓角色塊
        draw.rounded_rectangle([x0, y0, x1, y1], radius=40, fill=color)

        cx = (x0 + x1) // 2

        # Emoji（上半）— 真實渲染置中
        _paste_centered_emoji(img, emoji, cx, y0 + 100)

        # 中文標籤（下半）— 用 anchor="mt" 直接居中
        draw.text((cx, y0 + 560), label, font=LABEL_FONT,
                  fill="white", anchor="mt")

    out_path = Path(__file__).parent / "richmenu.png"
    img.convert("RGB").save(out_path, "PNG", optimize=True)
    print(f"🖼️  已產生 {out_path} ({out_path.stat().st_size // 1024} KB)")
    return out_path


# ============================================================
# 2. 上傳 LINE
# ============================================================


def deploy_rich_menu(image_path: Path) -> str:
    configuration = Configuration(access_token=TOKEN)

    areas = []
    for (row, col, _, _, _, text_action) in CELLS:
        areas.append(RichMenuArea(
            bounds=RichMenuBounds(
                x=col * CELL_W, y=row * CELL_H,
                width=CELL_W, height=CELL_H,
            ),
            action=MessageAction(text=text_action),
        ))

    menu_request = RichMenuRequest(
        size=RichMenuSize(width=WIDTH, height=HEIGHT),
        selected=True,
        name="fitness_main_menu_v1",
        chat_bar_text="點開選單 💪",
        areas=areas,
    )

    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)

        # 刪掉舊的 rich menu（保持乾淨）
        try:
            existing = api.get_rich_menu_list()
            for m in (existing.richmenus or []):
                try:
                    api.delete_rich_menu(m.rich_menu_id)
                    print(f"🧹 刪除舊 rich menu: {m.rich_menu_id}")
                except Exception as exc:
                    print(f"   (刪除失敗，略過：{exc})")
        except Exception as exc:
            print(f"   (列舉舊 menu 失敗，略過：{exc})")

        # 建新 rich menu
        resp = api.create_rich_menu(menu_request)
        rid = resp.rich_menu_id
        print(f"✨ 建立 rich menu: {rid}")

        # 上傳圖 — linebot.v3 的 blob API 在 Python 3.13 有 bug，
        # 直接打 LINE 的 binary upload endpoint
        with open(image_path, "rb") as f:
            r = requests.post(
                f"https://api-data.line.me/v2/bot/richmenu/{rid}/content",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "image/png",
                },
                data=f.read(),
                timeout=30,
            )
        if r.status_code != 200:
            raise RuntimeError(f"圖片上傳失敗 ({r.status_code}): {r.text}")
        print("📤 圖片上傳完成")

        # 設為預設（所有現有 + 新好友都會看到）
        api.set_default_rich_menu(rid)
        print("✅ 已設為預設 rich menu")

    return rid


def main():
    print("=" * 60)
    print("🏋️  Fitness Bot — Rich Menu 設定")
    print("=" * 60)
    image_path = draw_image()
    rid = deploy_rich_menu(image_path)
    print()
    print("🎉 完成！打開 LINE 任何一個跟 bot 的對話應該都會看到底部選單。")
    print(f"   Rich Menu ID: {rid}")


if __name__ == "__main__":
    main()
