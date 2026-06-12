"""V2 肌肉剪影生成：用 Wikimedia 公領域肌肉解剖圖為底，
裁掉文字標籤、肌肉去飽和成灰、目標肌群高亮藍。

來源（Public Domain）：
- Muscles anterior labeled.png by Mikael Häggström (Wikimedia Commons)
- Muscle posterior labeled.png by Mikael Häggström (Wikimedia Commons)

執行：uv run python gen_muscle_v2.py
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REF_ANT = Path("muscles/_ref_anterior.png")
REF_POS = Path("muscles/_ref_posterior.png")
OUTPUT_DIR = Path("muscles")
REF_URLS = {
    REF_ANT: "https://upload.wikimedia.org/wikipedia/commons/e/e5/Muscles_anterior_labeled.png",
    REF_POS: "https://upload.wikimedia.org/wikipedia/commons/4/46/Muscle_posterior_labeled.png",
}
OUT_SIZE = 400  # 縮 + quantize 控制在 30 KB 內，避開 HF Xet 上限

# ===== 裁掉文字標籤：保留中央身體區（裁緊） =====
# anterior: 1156 x 1342
ANT_BODY_BOX = (290, 0, 820, 1342)   # 寬 530
# posterior: 1063 x 1297
POS_BODY_BOX = (300, 0, 770, 1297)   # 寬 470

# ===== 目標肌群 bounding box（基於裁切後座標） =====
# ant 裁切後 530x1342, pos 裁切後 470x1297
MUSCLE_REGIONS = {
    "back":      ("posterior", [(70,  80,  400, 490)]),                  # 上背 + 闊背
    "chest":     ("anterior",  [(110, 240, 420, 400)]),                  # 胸大肌
    "shoulders": ("anterior",  [(10,  190, 150, 320),
                                (380, 190, 520, 320)]),                  # 兩肩三角
    "arms":      ("anterior",  [(0,   260,  110, 540),
                                (420, 260,  530, 540)]),                 # 兩上臂
    "legs":      ("anterior",  [(130, 620, 265, 1000),
                                (265, 620, 400, 1000)]),                 # 兩大腿
}


def _load_white_bg(path: Path, bg_color: tuple = (245, 245, 245)) -> Image.Image:
    """讀檔，把透明背景填為淺灰底（避免 convert RGB 變黑）。"""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        bg = Image.new("RGB", img.size, bg_color)
        alpha = img.split()[-1] if img.mode == "RGBA" else None
        bg.paste(img.convert("RGBA"), mask=alpha)
        return bg
    return img.convert("RGB")


def _detect_muscle_mask(arr: np.ndarray) -> np.ndarray:
    """偵測「紅色肌肉」像素（R 顯著大於 G、B，且不要太暗）。"""
    r = arr[:, :, 0].astype(int)
    g = arr[:, :, 1].astype(int)
    b = arr[:, :, 2].astype(int)
    return (r > g + 8) & (r > b + 8) & (r > 70)


def _erase_text_labels(arr: np.ndarray, bg: tuple = (245, 245, 245)) -> np.ndarray:
    """把文字 / 引線抹掉：偵測「灰色暗像素」（接近中性灰、暗於閾值），
    並保留肌肉的紅色暗部（避免誤抹肌肉紋路）。
    """
    r = arr[:, :, 0].astype(int)
    g = arr[:, :, 1].astype(int)
    b = arr[:, :, 2].astype(int)
    # 灰色 = RGB 三通道差距很小
    is_gray = (np.abs(r - g) < 18) & (np.abs(g - b) < 18) & (np.abs(r - b) < 18)
    # 暗：r, g, b 都比較低
    is_dark = (r < 140) & (g < 140) & (b < 140)
    # 不是純白也不是純黑亮部
    text_mask = is_gray & is_dark
    arr[text_mask] = bg
    return arr


def _wipe_edges(arr: np.ndarray, edge_width: int = 25,
                bg: tuple = (245, 245, 245)) -> np.ndarray:
    """左右邊緣強制填白（去除殘留標籤）。"""
    arr[:, :edge_width, :] = bg
    arr[:, -edge_width:, :] = bg
    return arr


def _to_gray_blue(body_arr: np.ndarray, regions: list[tuple]) -> np.ndarray:
    """所有肌肉去飽和變灰，指定矩形內的肌肉變藍。"""
    # 先把文字 / 引線抹掉
    body_arr = _erase_text_labels(body_arr.copy())
    body_arr = _wipe_edges(body_arr, edge_width=25)
    muscle = _detect_muscle_mask(body_arr)
    r = body_arr[:, :, 0].astype(int)
    g = body_arr[:, :, 1].astype(int)
    b = body_arr[:, :, 2].astype(int)
    lum = (0.299 * r + 0.587 * g + 0.114 * b).clip(0, 255)

    out = body_arr.copy()
    # 肌肉變灰：用亮度當基底，淡化飽和
    out[muscle, 0] = (lum[muscle] * 0.8 + 30).clip(0, 255)
    out[muscle, 1] = (lum[muscle] * 0.8 + 30).clip(0, 255)
    out[muscle, 2] = (lum[muscle] * 0.8 + 30).clip(0, 255)

    # 目標區域變藍
    h, w = muscle.shape
    region_mask = np.zeros_like(muscle)
    for (x1, y1, x2, y2) in regions:
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(w, x2)
        y2 = min(h, y2)
        region_mask[y1:y2, x1:x2] = True
    target = muscle & region_mask
    # 藍色高亮（保留亮度層次）
    out[target, 0] = (lum[target] * 0.20 + 20).clip(0, 255)  # 低 R
    out[target, 1] = (lum[target] * 0.40 + 40).clip(0, 255)  # 中 G
    out[target, 2] = (lum[target] * 1.05 + 120).clip(0, 255) # 高 B
    return out.astype(np.uint8)


def _fit_to_square(img: Image.Image, size: int, bg: str = "#F5F5F5") -> Image.Image:
    """等比例縮放並置中於 size×size 白底方框。"""
    w, h = img.size
    scale = size / max(w, h)
    new_w, new_h = int(w * scale), int(h * scale)
    img2 = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), bg)
    paste_x = (size - new_w) // 2
    paste_y = (size - new_h) // 2
    canvas.paste(img2, (paste_x, paste_y))
    return canvas


def _download_refs() -> None:
    """如果 ref 圖不在，從 Wikimedia 自動下載。"""
    import urllib.request
    for path, url in REF_URLS.items():
        if path.exists():
            continue
        print(f"  📥 下載 {path.name} from Wikimedia ⋯")
        path.parent.mkdir(exist_ok=True)
        req = urllib.request.Request(
            url, headers={"User-Agent": "SCULINEBOT/1.0 (educational)"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            path.write_bytes(r.read())
        print(f"     OK ({path.stat().st_size // 1024} KB)")


def generate() -> None:
    print("=" * 50)
    print("🏋️  V2 肌肉剪影生成（基於 Wikimedia 公領域解剖圖）")
    print("=" * 50)

    _download_refs()
    if not REF_ANT.exists() or not REF_POS.exists():
        print(f"❌ 找不到參考圖：{REF_ANT} 或 {REF_POS}")
        sys.exit(1)

    ant = np.array(_load_white_bg(REF_ANT).crop(ANT_BODY_BOX))
    pos = np.array(_load_white_bg(REF_POS).crop(POS_BODY_BOX))
    print(f"📐 anterior 裁切後 {ant.shape[1]}x{ant.shape[0]}")
    print(f"📐 posterior 裁切後 {pos.shape[1]}x{pos.shape[0]}")

    for name, (view, regions) in MUSCLE_REGIONS.items():
        body_arr = ant if view == "anterior" else pos
        out_arr = _to_gray_blue(body_arr, regions)
        img = Image.fromarray(out_arr)
        # 縮放成方形
        final = _fit_to_square(img, OUT_SIZE)
        out_path = OUTPUT_DIR / f"{name}.png"
        # 用 palette quantize 大幅壓縮（HF Space binary 上限會擋）
        # 64 色對解剖剪影夠用、視覺幾乎看不出差別
        final.quantize(colors=64, method=Image.Quantize.MEDIANCUT).save(
            out_path, "PNG", optimize=True
        )
        print(f"  ✅ {out_path.name} ({out_path.stat().st_size // 1024} KB)")

    print()
    print("🎉 完成。可從 muscles/ 看新版圖")


if __name__ == "__main__":
    generate()
