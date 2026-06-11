"""
東吳大學資料系 2026 上半年 LINE Bot 進階課程
健身紀錄與調整體態的 LINE Bot — 單檔教學範例

三大功能：
  1. 目標設定與追蹤（SMART 引導 + 回顧）
  2. 自我成長與習慣（運動 / 飲水 / 睡眠 / 反思 / 壞習慣）
  3. 飲食與健康（TDEE 計算 / 飲食方針 / 彈性菜單 / 點心衛教）

部署目標：Hugging Face Spaces (Docker SDK)
依賴：Flask / linebot.v3 / google-genai / supabase / APScheduler
"""

import functools
import json
import logging
import os
import time
from datetime import date, datetime
from typing import Optional

import httpx

import markdown
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from flask import Flask, abort, request
from google import genai
from supabase import Client, create_client

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    FlexContainer,
    FlexMessage,
    MessageAction,
    MessagingApi,
    PostbackAction,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    FollowEvent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

# ============================================================
# 1. 全域設定與初始化
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("fitness")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

GEMINI_MODEL = "gemini-3.1-flash-lite"

app = Flask(__name__)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# 2. Supabase DB Helpers
# ============================================================


def _sb_retry(fn=None, *, retries=3, delay=0.4):
    """裝飾器：HTTP/2 抖動時自動重試（cold boot 常見）。"""
    def wrapper(func):
        @functools.wraps(func)
        def inner(*args, **kwargs):
            last_exc = None
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except (httpx.RemoteProtocolError, httpx.ConnectError,
                        httpx.ReadError) as exc:
                    last_exc = exc
                    logger.warning("Supabase 連線抖動 attempt=%d func=%s err=%s",
                                   attempt + 1, func.__name__, exc)
                    if attempt < retries - 1:
                        time.sleep(delay * (attempt + 1))
            raise last_exc
        return inner
    return wrapper(fn) if fn else wrapper


@_sb_retry
def get_profile(user_id: str) -> Optional[dict]:
    res = supabase.table("profiles").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None


@_sb_retry
def upsert_profile(user_id: str, fields: dict) -> None:
    fields["user_id"] = user_id
    fields["updated_at"] = datetime.utcnow().isoformat()
    supabase.table("profiles").upsert(fields).execute()


@_sb_retry
def get_state(user_id: str) -> Optional[dict]:
    res = (
        supabase.table("conversation_state")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )
    return res.data[0] if res.data else None


@_sb_retry
def set_state(user_id: str, flow: str, step: str, data: Optional[dict] = None) -> None:
    payload = {
        "user_id": user_id,
        "flow": flow,
        "step": step,
        "data": data or {},
        "updated_at": datetime.utcnow().isoformat(),
    }
    supabase.table("conversation_state").upsert(payload).execute()


@_sb_retry
def clear_state(user_id: str) -> None:
    supabase.table("conversation_state").delete().eq("user_id", user_id).execute()


@_sb_retry
def get_active_goal(user_id: str) -> Optional[dict]:
    res = (
        supabase.table("goals")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


@_sb_retry
def insert_goal(user_id: str, fields: dict) -> dict:
    fields["user_id"] = user_id
    res = supabase.table("goals").insert(fields).execute()
    return res.data[0] if res.data else {}


@_sb_retry
def log_habit(user_id: str, type_: str, amount: Optional[float] = None,
              quality: Optional[str] = None, note: Optional[str] = None) -> None:
    supabase.table("habit_logs").insert({
        "user_id": user_id,
        "type": type_,
        "amount": amount,
        "quality": quality,
        "note": note,
    }).execute()


@_sb_retry
def get_latest_habit(user_id: str, type_: str) -> Optional[dict]:
    """拿最新一筆指定類型的習慣紀錄。"""
    res = (
        supabase.table("habit_logs")
        .select("*")
        .eq("user_id", user_id)
        .eq("type", type_)
        .order("recorded_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


@_sb_retry
def get_today_habit_sum(user_id: str, type_: str) -> float:
    today_start = datetime.combine(date.today(), datetime.min.time()).isoformat()
    res = (
        supabase.table("habit_logs")
        .select("amount")
        .eq("user_id", user_id)
        .eq("type", type_)
        .gte("recorded_at", today_start)
        .execute()
    )
    return sum((row["amount"] or 0) for row in (res.data or []))


@_sb_retry
def insert_reflection(user_id: str, period: str, content: str, ai_summary: str) -> None:
    supabase.table("reflections").insert({
        "user_id": user_id,
        "period": period,
        "content": content,
        "ai_summary": ai_summary,
    }).execute()


@_sb_retry
def all_active_user_ids() -> list[str]:
    """推播 job 用：取出所有已建檔的使用者 ID。"""
    res = supabase.table("profiles").select("user_id").execute()
    return [row["user_id"] for row in (res.data or [])]


@_sb_retry
def users_with_goal_review(freq: str) -> list[dict]:
    """回傳 review_freq=freq 的活躍目標。"""
    res = (
        supabase.table("goals")
        .select("*")
        .eq("status", "active")
        .eq("review_freq", freq)
        .execute()
    )
    return res.data or []


@_sb_retry
def delete_latest_habit(user_id: str, type_: str) -> Optional[dict]:
    """刪除該使用者最新一筆指定類型的 habit_log。回傳被刪的那筆。"""
    res = (
        supabase.table("habit_logs")
        .select("*")
        .eq("user_id", user_id)
        .eq("type", type_)
        .order("recorded_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    record = res.data[0]
    supabase.table("habit_logs").delete().eq("id", record["id"]).execute()
    return record


@_sb_retry
def delete_latest_reflection(user_id: str) -> Optional[dict]:
    res = (
        supabase.table("reflections")
        .select("*")
        .eq("user_id", user_id)
        .order("recorded_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    record = res.data[0]
    supabase.table("reflections").delete().eq("id", record["id"]).execute()
    return record


@_sb_retry
def abandon_active_goals(user_id: str) -> int:
    """把所有 active 目標標為 abandoned，回傳被標記的筆數。"""
    res = (
        supabase.table("goals")
        .update({"status": "abandoned"})
        .eq("user_id", user_id)
        .eq("status", "active")
        .execute()
    )
    return len(res.data or [])


NOTIFY_KEYS = ("morning", "evening", "sleep", "water", "stretch", "goal")
NOTIFY_SIMPLE_KEYS = ("morning", "evening")
NOTIFY_ADVANCED_KEYS = ("sleep", "water", "stretch", "goal")


def get_notify_prefs(user_id: str) -> dict:
    """回傳 {sleep, water, stretch, goal} 的 bool。沒檔案視同全開。"""
    p = get_profile(user_id)
    if not p:
        return {k: True for k in NOTIFY_KEYS}
    return {
        k: (p.get(f"notify_{k}") if p.get(f"notify_{k}") is not None else True)
        for k in NOTIFY_KEYS
    }


@_sb_retry
def set_notify_pref(user_id: str, key: str, value: bool) -> None:
    if key not in NOTIFY_KEYS:
        return
    supabase.table("profiles").update({
        f"notify_{key}": value,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("user_id", user_id).execute()


@_sb_retry
def users_with_notify_on(key: str) -> list[str]:
    """回傳該通知開著的所有 user_id。"""
    if key not in NOTIFY_KEYS:
        return []
    res = supabase.table("profiles").select("user_id").eq(f"notify_{key}", True).execute()
    return [row["user_id"] for row in (res.data or [])]


@_sb_retry
def wipe_all_user_data(user_id: str) -> dict:
    """刪除該使用者所有資料。回傳各表刪除筆數，方便回報。"""
    counts = {}
    for table in ("habit_logs", "reflections", "goals", "conversation_state", "profiles"):
        res = supabase.table(table).delete().eq("user_id", user_id).execute()
        counts[table] = len(res.data or [])
    return counts


# ============================================================
# 3. TDEE / BMR / 營養素計算
# ============================================================

ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,    # 久坐
    "light": 1.375,      # 輕度（1-3 次/週）
    "moderate": 1.55,    # 中度（3-5 次/週）
    "active": 1.725,     # 高度（6-7 次/週）
    "very_active": 1.9,  # 極高（每天 2 次）
}

ACTIVITY_LABEL = {
    "sedentary": "久坐（幾乎不運動）",
    "light": "輕度（每週 1-3 次）",
    "moderate": "中度（每週 3-5 次）",
    "active": "高度（每週 6-7 次）",
    "very_active": "極高（每天 2 次）",
}

TARGET_KCAL_DELTA = {"bulk": 300, "maintain": 0, "cut": -400}

# 三大營養素比例（蛋白、碳水、脂肪）
MACRO_RATIO = {
    "bulk":     (0.30, 0.50, 0.20),
    "maintain": (0.25, 0.50, 0.25),
    "cut":      (0.35, 0.40, 0.25),
}


def calc_bmr(gender: str, weight_kg: float, height_cm: float, age: int) -> float:
    """Mifflin-St Jeor 公式。"""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if gender == "male" else base - 161


def calc_tdee(bmr: float, activity_level: str) -> float:
    return bmr * ACTIVITY_MULTIPLIERS.get(activity_level, 1.2)


def calc_macros(target_kcal: float, target_type: str) -> tuple[int, int, int]:
    """回傳 (protein_g, carb_g, fat_g)。"""
    p, c, f = MACRO_RATIO.get(target_type, MACRO_RATIO["maintain"])
    return (
        int(target_kcal * p / 4),
        int(target_kcal * c / 4),
        int(target_kcal * f / 9),
    )


def calc_daily_water_ml(weight_kg: float) -> int:
    """體重 x 37 ml（35-40 區間的中間值）。"""
    return int(weight_kg * 37)


# ============================================================
# 4. LLM 包裝（Gemini）
# ============================================================


def claude_ask(prompt: str, system: str = "", max_tokens: int = 1024) -> str:
    """單次呼叫 Gemini，回傳純文字（Markdown → 純文字）。

    使用 gemini-3.1-flash-lite — 免費方案、低延遲，
    適合 LINE bot 即時對話。
    （函式名歷史保留為 claude_ask，避免大量改動 caller。）
    """
    try:
        config = None
        if system:
            from google.genai.types import GenerateContentConfig
            config = GenerateContentConfig(system_instruction=system)
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt],
            config=config,
        )
        text = response.text or ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gemini error: %s", exc)
        return "（AI 教練暫時無法回應，請稍後再試）"

    html = markdown.markdown(text)
    return BeautifulSoup(html, "html.parser").get_text().strip()


def claude_smart_review(description: str) -> str:
    system = (
        "你是一位健身教練，擅長以 SMART 框架（Specific 具體、Measurable 可量化、"
        "Achievable 可達成、Relevant 相關、Time-bound 有期限）幫助學員精煉目標。"
        "請用繁體中文、不超過 5 行，先肯定學員的方向，再指出哪些 SMART 維度還可加強，"
        "並用 1 句話示範重寫後的目標。語氣溫暖、具體、不囉嗦。"
    )
    return claude_ask(f"我的目標：{description}", system=system)


def claude_reflection_summary(period: str, content: str) -> str:
    period_label = {"training": "訓練心得", "week": "週回顧", "month": "月回顧"}.get(period, "反思")
    system = (
        f"你是一位健身教練兼成長教練。學員剛完成一段 {period_label}。"
        "請用繁體中文，依以下結構回覆（每段 1-2 句）：\n"
        "1) 看見了什麼亮點\n"
        "2) 可優化的地方\n"
        "3) 下一步建議行動\n"
        "整體不超過 6 行，語氣鼓勵但具體。"
    )
    return claude_ask(content, system=system)


def claude_meal_suggestion(profile: dict, meal_slot: str) -> str:
    style = "外食族" if profile.get("eating_style") == "outside" else "自煮族"
    veg = "素食" if profile.get("is_vegetarian") else "葷食"
    target = profile.get("target_type", "maintain")
    target_label = {"bulk": "增肌", "maintain": "維持", "cut": "減脂"}.get(target, "維持")
    system = (
        "你是一位專業營養師，請用繁體中文給出簡潔可執行的單餐建議（不超過 6 行）。"
        "格式：先列出 2-3 個可選組合（含份量），最後一句白話文教練碎碎念說明選擇原因。"
    )
    prompt = (
        f"請幫一位「{target_label}期」的學員規劃一份「{meal_slot}」。"
        f"條件：{style}、{veg}、每日目標熱量約 {int(profile.get('target_kcal') or 0)} kcal、"
        f"目標蛋白質約 {int(profile.get('protein_g') or 0)} g。"
    )
    return claude_ask(prompt, system=system)


def claude_workout_snack(profile: dict, when: str) -> str:
    workout_time = profile.get("workout_time", "evening")
    workout_label = {"morning": "晨練", "afternoon": "下午練", "evening": "夜練"}.get(workout_time, "夜練")
    system = (
        "你是一位專業運動營養師。請用繁體中文回覆運動前 或 運動後 的點心建議，"
        "格式：2-3 個食物組合（含份量），最後一行白話文說明為什麼這樣搭配（營養時機學 Nutrient Timing）。"
        "整體不超過 5 行。"
    )
    prompt = f"我是 {workout_label} 的學員，請給我「{when}」點心建議。"
    return claude_ask(prompt, system=system)


# ============================================================
# 5. Flex Message 樣板（香橘活力色系）
# ============================================================

# --- 香橘活力色票 ---
C_PRIMARY = "#FF6B35"      # 主橘
C_PRIMARY_DARK = "#E55B25" # 深橘（按下/陰影）
C_ACCENT = "#A8D936"       # 奇異果綠（成就/正向）
C_PEACH = "#FF9966"        # 桃橘（次要 CTA）
C_BG_WARM = "#FFF8F0"      # 暖白底
C_TEXT_DARK = "#2D2D2D"    # 深灰主文字
C_TEXT_SOFT = "#888888"    # 次文字
C_DIVIDER = "#F0E6DA"      # 分隔線
C_TRACK = "#F5E6DA"        # 進度條底
C_WARN = "#FF4D4F"         # 警示紅
C_SLEEP = "#7B68EE"        # 睡眠紫（夜間元素）

# --- 各功能主題色（一致性） ---
COLOR_GOAL = C_PRIMARY        # 🎯 目標 — 橘
COLOR_HABIT = C_ACCENT        # 🌱 自我成長 — 綠
COLOR_DIET = C_PEACH          # 🥗 飲食 — 桃
COLOR_MGMT = "#9E9E9E"        # ⚙️ 功能設定 — 中性灰
COLOR_WATER = "#5DADE2"       # 💧 水 — 藍
COLOR_SLEEP = C_SLEEP         # 🌙 睡眠 — 紫
COLOR_STRETCH = C_PEACH       # 🪑 伸展 — 桃


def _flex(alt: str, contents: dict) -> FlexMessage:
    container = FlexContainer.from_json(json.dumps(contents))
    return FlexMessage(alt_text=alt, contents=container)


def main_menu_flex() -> FlexMessage:
    body = {
        "type": "carousel",
        "contents": [
            _menu_card("🎯", "目標設定", "用 SMART 框架設目標", COLOR_GOAL, "開始設目標", "目標設定"),
            _menu_card("🌱", "自我成長", "運動 / 飲水 / 睡眠 / 反思", COLOR_HABIT, "進入", "自我成長"),
            _menu_card("🥗", "飲食與健康", "TDEE / 菜單 / 運動點心", COLOR_DIET, "進入", "飲食與健康"),
            _menu_card("⚙️", "功能設定", "🔔 通知 / 撤銷 / 重設 / 刪除", COLOR_MGMT, "進入", "功能設定"),
        ],
    }
    return _flex("主選單", body)


def _menu_card(emoji: str, title: str, subtitle: str, color: str, btn_label: str, btn_text: str) -> dict:
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": color,
            "paddingAll": "20px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": emoji, "size": "4xl", "align": "center"},
                {"type": "text", "text": title, "weight": "bold", "size": "xl",
                 "color": "#FFFFFF", "align": "center", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": subtitle, "wrap": True,
                 "size": "sm", "color": C_TEXT_SOFT, "align": "center"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "contents": [{
                "type": "button", "style": "primary", "color": color, "height": "sm",
                "action": {"type": "message", "label": btn_label, "text": btn_text},
            }],
        },
    }


def tdee_result_flex(profile: dict) -> FlexMessage:
    target_label = {"bulk": "增肌", "maintain": "維持", "cut": "減脂"}.get(profile["target_type"], "維持")
    target_emoji = {"bulk": "🔥", "maintain": "⚖️", "cut": "✂️"}.get(profile["target_type"], "⚖️")
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": C_PRIMARY,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🎯 你的能量基準線",
                 "color": "#FFFFFF", "size": "sm", "weight": "bold"},
                {"type": "text", "text": f"{target_emoji} {target_label}期",
                 "color": "#FFFFFF", "weight": "bold", "size": "xxl", "margin": "sm"},
                {"type": "text",
                 "text": f"每日目標 {int(profile['target_kcal'])} kcal",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "📊 能量配置", "size": "sm",
                 "color": C_TEXT_SOFT, "weight": "bold"},
                _kv("基礎代謝 BMR", f"{int(profile['bmr'])} kcal"),
                _kv("每日總消耗 TDEE", f"{int(profile['tdee'])} kcal"),
                {"type": "separator", "margin": "md", "color": C_DIVIDER},
                {"type": "text", "text": "🥗 三大營養素",
                 "size": "sm", "color": C_TEXT_SOFT, "weight": "bold", "margin": "md"},
                _kv("🥩 蛋白質", f"{profile['protein_g']} g"),
                _kv("🍚 碳水", f"{profile['carb_g']} g"),
                _kv("🥑 脂肪", f"{profile['fat_g']} g"),
                {"type": "separator", "margin": "md", "color": C_DIVIDER},
                _kv("💧 每日喝水", f"{profile['daily_water_ml']} ml"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": C_PRIMARY, "height": "sm",
                 "action": {"type": "message", "label": "📋 看主選單", "text": "選單"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "message", "label": "重新設定資料", "text": "個人資料"}},
            ],
        },
    }
    return _flex("TDEE 結果", body)


def _kv(k: str, v: str) -> dict:
    return {
        "type": "box", "layout": "horizontal",
        "contents": [
            {"type": "text", "text": k, "color": C_TEXT_SOFT, "size": "sm", "flex": 5},
            {"type": "text", "text": v, "wrap": True, "size": "sm", "flex": 4,
             "align": "end", "weight": "bold", "color": C_TEXT_DARK},
        ],
    }


def goal_card_flex(goal: dict) -> FlexMessage:
    deadline_str = goal.get("deadline") or ""
    countdown = ""
    if deadline_str:
        try:
            d = datetime.strptime(deadline_str, "%Y-%m-%d").date()
            days = (d - date.today()).days
            if days > 0:
                countdown = f"⏳ 還剩 {days} 天"
            elif days == 0:
                countdown = "🔔 今天到期！"
            else:
                countdown = f"⚠️ 已過期 {-days} 天"
        except (ValueError, TypeError):
            countdown = ""

    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": C_PRIMARY,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🎯 你的目標",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": countdown or "📅 期限未設",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": goal.get("description") or "（未填）",
                 "wrap": True, "weight": "bold", "size": "md", "color": C_TEXT_DARK},
                {"type": "separator", "margin": "md", "color": C_DIVIDER},
                _kv("📏 可量化指標", goal.get("smart_measurable") or "—"),
                _kv("📅 期限", deadline_str or "—"),
                _kv("👣 本週第一步", goal.get("first_step") or "—"),
                _kv("🏁 里程碑", goal.get("milestones") or "—"),
                _kv("🔁 回顧頻率", "每日 ✨" if goal.get("review_freq") == "daily" else "每週 📆"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": C_ACCENT, "height": "sm",
                 "action": {"type": "postback", "label": "🎉 完成目標",
                            "data": "action=goal_done",
                            "displayText": "我完成這個目標了！"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "message", "label": "設新目標", "text": "新目標"}},
            ],
        },
    }
    return _flex("我的目標", body)


def _progress_bar(pct: int, color: str) -> dict:
    """水平進度條，pct 0-100。"""
    pct = max(0, min(100, pct))
    return {
        "type": "box", "layout": "horizontal", "height": "10px",
        "backgroundColor": C_TRACK, "cornerRadius": "5px",
        "contents": [
            {"type": "box", "layout": "vertical", "width": f"{max(1, pct)}%",
             "backgroundColor": color, "cornerRadius": "5px",
             "contents": [{"type": "filler"}]},
        ],
    }


def water_card_flex(current_ml: int, target_ml: int) -> FlexMessage:
    pct = min(100, int(current_ml * 100 / max(1, target_ml)))
    encouragement = (
        "🎉 達標了！繼續保持！" if pct >= 100
        else "💪 快達標了！再加一杯！" if pct >= 75
        else "👍 進度不錯，繼續喝！" if pct >= 50
        else "💧 慢慢累積，每口都算！"
    )
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": COLOR_WATER,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "💧 今日飲水",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": f"{current_ml} / {target_ml} ml",
                 "color": "#FFFFFF", "size": "xxl", "weight": "bold", "margin": "sm"},
                {"type": "text", "text": f"{pct}% 完成",
                 "color": "#FFFFFF", "size": "sm", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                _progress_bar(pct, COLOR_WATER),
                {"type": "text", "text": encouragement, "size": "sm",
                 "color": C_TEXT_DARK, "align": "center", "margin": "md"},
                {"type": "separator", "color": C_DIVIDER, "margin": "md"},
                {"type": "text", "text": "選一杯打卡 👇", "size": "sm",
                 "color": C_TEXT_SOFT, "margin": "sm"},
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": COLOR_WATER, "height": "sm",
                 "action": {"type": "postback", "label": "🥤 200",
                            "data": "action=log_water&amount=200",
                            "displayText": "我喝了 200ml 💧"}},
                {"type": "button", "style": "primary", "color": COLOR_WATER, "height": "sm",
                 "action": {"type": "postback", "label": "🍶 350",
                            "data": "action=log_water&amount=350",
                            "displayText": "我喝了 350ml 💧"}},
                {"type": "button", "style": "primary", "color": COLOR_WATER, "height": "sm",
                 "action": {"type": "postback", "label": "🧴 500",
                            "data": "action=log_water&amount=500",
                            "displayText": "我喝了 500ml 💧"}},
            ],
        },
    }
    return _flex("飲水進度", body)


def sleep_card_flex() -> FlexMessage:
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": COLOR_SLEEP,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🌙 昨晚睡得好嗎？",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "睡眠是最強的修復神器",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px",
            "contents": [
                {"type": "text",
                 "text": "肌肉在睡眠中修復、生長激素也在這時分泌。先選一下昨晚感受 👇",
                 "wrap": True, "size": "sm", "color": C_TEXT_SOFT},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": C_ACCENT, "height": "sm",
                 "action": {"type": "postback", "label": "😴 睡得很好",
                            "data": "action=log_sleep&quality=good",
                            "displayText": "昨晚睡得很好 😴"}},
                {"type": "button", "style": "primary", "color": COLOR_SLEEP, "height": "sm",
                 "action": {"type": "postback", "label": "😐 還可以",
                            "data": "action=log_sleep&quality=normal",
                            "displayText": "昨晚還可以 😐"}},
                {"type": "button", "style": "primary", "color": C_WARN, "height": "sm",
                 "action": {"type": "postback", "label": "😣 不太好",
                            "data": "action=log_sleep&quality=bad",
                            "displayText": "昨晚沒睡好 😣"}},
            ],
        },
    }
    return _flex("睡眠回顧", body)


def stretch_card_flex() -> FlexMessage:
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": COLOR_STRETCH,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🪑 久坐破冰時間",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "1 分鐘站起來動一下",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "今日動作菜單", "weight": "bold",
                 "color": C_TEXT_DARK, "size": "sm"},
                {"type": "box", "layout": "vertical", "spacing": "xs", "margin": "md",
                 "contents": [
                     {"type": "text", "text": "1️⃣ 原地踏步 20 下",
                      "wrap": True, "size": "sm", "color": C_TEXT_DARK},
                     {"type": "text", "text": "2️⃣ 扶椅背胸口前推 10 秒 × 3",
                      "wrap": True, "size": "sm", "color": C_TEXT_DARK},
                     {"type": "text", "text": "3️⃣ 弓箭步髖伸展 左右各 20 秒",
                      "wrap": True, "size": "sm", "color": C_TEXT_DARK},
                 ]},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "contents": [{"type": "button", "style": "primary",
                          "color": COLOR_STRETCH, "height": "sm",
                          "action": {"type": "postback", "label": "✅ 動完了！",
                                     "data": "action=log_stretch",
                                     "displayText": "我動起來了！💪"}}],
        },
    }
    return _flex("久坐伸展", body)


def macro_visual_flex(profile: dict) -> FlexMessage:
    p_g, c_g, f_g = profile["protein_g"], profile["carb_g"], profile["fat_g"]
    p_kcal, c_kcal, f_kcal = p_g * 4, c_g * 4, f_g * 9
    total = max(1, p_kcal + c_kcal + f_kcal)
    p_pct = int(p_kcal * 100 / total)
    c_pct = int(c_kcal * 100 / total)
    f_pct = int(f_kcal * 100 / total)
    target_emoji = {"bulk": "🔥", "maintain": "⚖️", "cut": "✂️"}.get(profile["target_type"], "⚖️")
    target_label = {"bulk": "增肌期", "maintain": "維持期", "cut": "減脂期"}.get(profile["target_type"], "")

    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": COLOR_DIET,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🥗 今日三大營養素",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": f"{target_emoji} {target_label}",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "lg", "paddingAll": "16px",
            "contents": [
                _macro_bar("🥩 蛋白質", p_g, p_pct, "#E55B25"),
                _macro_bar("🍚 碳水", c_g, c_pct, "#F4A261"),
                _macro_bar("🥑 脂肪", f_g, f_pct, C_ACCENT),
                {"type": "separator", "margin": "md", "color": C_DIVIDER},
                {"type": "text", "text": _macro_coach_note(profile["target_type"]),
                 "wrap": True, "size": "sm", "color": C_TEXT_DARK, "margin": "md"},
            ],
        },
    }
    return _flex("三大營養素", body)


def _macro_bar(label: str, grams: int, pct: int, color: str) -> dict:
    return {
        "type": "box", "layout": "vertical", "spacing": "xs",
        "contents": [
            {"type": "box", "layout": "horizontal",
             "contents": [
                 {"type": "text", "text": label, "size": "sm",
                  "flex": 3, "color": C_TEXT_DARK, "weight": "bold"},
                 {"type": "text", "text": f"{grams} g · {pct}%", "size": "sm",
                  "align": "end", "flex": 4, "weight": "bold", "color": C_TEXT_DARK},
             ]},
            _progress_bar(pct, color),
        ],
    }


def _macro_coach_note(target_type: str) -> str:
    return {
        "bulk": "教練碎碎念：增肌期把碳水拉高、蛋白質充足，是為了給肌肉成長的原料和能量。",
        "maintain": "教練碎碎念：維持期三大營養均衡，重點在穩定攝取、不要暴衝暴掉。",
        "cut": "教練碎碎念：今天幫你拉高蛋白質，是為了讓你減脂期充滿飽足感，且不掉肌肉喔！",
    }.get(target_type, "教練碎碎念：吃好吃滿，但吃對東西，是體態改變的基石。")


def welcome_flex() -> FlexMessage:
    body = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": C_PRIMARY,
            "paddingAll": "24px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "💪", "size": "5xl", "align": "center"},
                {"type": "text", "text": "嗨，我是你的健身教練",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl",
                 "align": "center", "margin": "sm"},
                {"type": "text", "text": "目標 × 習慣 × 飲食 · 一次搞定",
                 "color": "#FFFFFF", "size": "sm", "align": "center", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "20px",
            "contents": [
                _welcome_row("🎯", "目標設定", "SMART 框架幫你拆解大目標"),
                _welcome_row("🌱", "自我成長", "運動 / 飲水 / 睡眠 / 反思紀錄"),
                _welcome_row("🥗", "飲食健康", "TDEE 計算 + 客製菜單"),
                {"type": "separator", "color": C_DIVIDER, "margin": "md"},
                {"type": "text",
                 "text": "👉 先建立個人資料、計算你的 TDEE 開始",
                 "wrap": True, "size": "sm", "color": C_TEXT_DARK,
                 "align": "center", "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": C_PRIMARY, "height": "sm",
                 "action": {"type": "message", "label": "⚙️ 建立個人資料",
                            "text": "個人資料"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "message", "label": "看主選單", "text": "選單"}},
            ],
        },
    }
    return _flex("歡迎使用健身教練", body)


def _welcome_row(emoji: str, title: str, subtitle: str) -> dict:
    return {
        "type": "box", "layout": "horizontal", "spacing": "md",
        "contents": [
            {"type": "text", "text": emoji, "size": "xl", "flex": 0, "gravity": "center"},
            {"type": "box", "layout": "vertical", "flex": 5,
             "contents": [
                 {"type": "text", "text": title, "weight": "bold",
                  "size": "sm", "color": C_TEXT_DARK},
                 {"type": "text", "text": subtitle, "size": "xs",
                  "color": C_TEXT_SOFT, "wrap": True},
             ]},
        ],
    }


def today_progress_flex(profile: dict, water_ml: int, water_target: int,
                        sleep_recent: Optional[dict], workout_min: float,
                        active_goal: Optional[dict]) -> FlexMessage:
    today_str = date.today().strftime("%m/%d (%a)")
    water_pct = min(100, int(water_ml * 100 / max(1, water_target)))

    sleep_text = "—"
    if sleep_recent:
        q = sleep_recent.get("quality", "")
        sleep_text = {"good": "😴 睡得好", "normal": "😐 還可以",
                      "bad": "😣 沒睡好"}.get(q, "已紀錄")

    workout_text = f"{int(workout_min)} 分鐘 💪" if workout_min > 0 else "尚未紀錄"

    goal_text = "尚未設定"
    if active_goal:
        desc = (active_goal.get("description") or "")[:18]
        goal_text = desc + ("…" if len(active_goal.get("description") or "") > 18 else "")

    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": C_PRIMARY,
            "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "📊 今日進度",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": today_str,
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "lg", "paddingAll": "16px",
            "contents": [
                # 飲水
                {"type": "box", "layout": "vertical", "spacing": "xs",
                 "contents": [
                     {"type": "box", "layout": "horizontal",
                      "contents": [
                          {"type": "text", "text": "💧 飲水", "size": "sm",
                           "color": C_TEXT_DARK, "weight": "bold", "flex": 2},
                          {"type": "text", "text": f"{water_ml}/{water_target} ml",
                           "size": "sm", "color": C_TEXT_DARK, "weight": "bold",
                           "flex": 3, "align": "end"},
                      ]},
                     _progress_bar(water_pct, COLOR_WATER),
                 ]},
                # 睡眠
                {"type": "box", "layout": "horizontal",
                 "contents": [
                     {"type": "text", "text": "🌙 昨晚睡眠", "size": "sm",
                      "color": C_TEXT_DARK, "weight": "bold", "flex": 3},
                     {"type": "text", "text": sleep_text, "size": "sm",
                      "color": C_TEXT_DARK, "flex": 3, "align": "end"},
                 ]},
                # 運動
                {"type": "box", "layout": "horizontal",
                 "contents": [
                     {"type": "text", "text": "💪 今日運動", "size": "sm",
                      "color": C_TEXT_DARK, "weight": "bold", "flex": 3},
                     {"type": "text", "text": workout_text, "size": "sm",
                      "color": C_TEXT_DARK, "flex": 3, "align": "end"},
                 ]},
                {"type": "separator", "color": C_DIVIDER},
                # 目標
                {"type": "box", "layout": "vertical", "spacing": "xs",
                 "contents": [
                     {"type": "text", "text": "🎯 進行中目標", "size": "xs",
                      "color": C_TEXT_SOFT},
                     {"type": "text", "text": goal_text, "size": "sm",
                      "color": C_TEXT_DARK, "weight": "bold", "wrap": True},
                 ]},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": COLOR_WATER, "height": "sm",
                 "action": {"type": "message", "label": "💧 喝水打卡", "text": "飲水"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "message", "label": "📋 主選單", "text": "選單"}},
            ],
        },
    }
    return _flex("今日進度", body)


def daily_briefing_flex(profile: dict, water_target: int,
                        sleep_recent: Optional[dict],
                        goal: Optional[dict]) -> FlexMessage:
    """每日早報（09:00）：早安 + 昨晚睡眠 + 飲水目標 + 目標倒數 + 一句話。"""
    today_str = date.today().strftime("%m/%d (%a)")

    sleep_label = "尚未紀錄"
    if sleep_recent:
        q = sleep_recent.get("quality", "")
        sleep_label = {"good": "😴 睡得好", "normal": "😐 還可以",
                       "bad": "😣 沒睡好"}.get(q, "已紀錄")

    goal_block = []
    if goal:
        desc = (goal.get("description") or "")[:24]
        deadline = goal.get("deadline") or ""
        countdown = ""
        if deadline:
            try:
                d = datetime.strptime(deadline, "%Y-%m-%d").date()
                days = (d - date.today()).days
                countdown = f"⏳ 還剩 {days} 天" if days > 0 else "🔔 今天到期"
            except (ValueError, TypeError):
                pass
        goal_block = [
            {"type": "separator", "color": C_DIVIDER, "margin": "md"},
            {"type": "box", "layout": "vertical", "spacing": "xs", "margin": "md",
             "contents": [
                 {"type": "text", "text": "🎯 進行中目標", "size": "xs",
                  "color": C_TEXT_SOFT},
                 {"type": "text", "text": desc, "size": "sm",
                  "color": C_TEXT_DARK, "weight": "bold", "wrap": True},
                 {"type": "text", "text": countdown, "size": "xs",
                  "color": C_PRIMARY, "weight": "bold"} if countdown else {"type": "filler"},
             ]},
        ]

    body = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": C_PRIMARY,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "📰 每日早報",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": f"早安 ☀️ {today_str}",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                # 昨晚睡眠
                {"type": "box", "layout": "horizontal",
                 "contents": [
                     {"type": "text", "text": "🌙 昨晚睡眠", "size": "sm",
                      "color": C_TEXT_DARK, "weight": "bold", "flex": 3},
                     {"type": "text", "text": sleep_label, "size": "sm",
                      "color": C_TEXT_DARK, "flex": 3, "align": "end"},
                 ]},
                # 飲水目標
                {"type": "box", "layout": "horizontal",
                 "contents": [
                     {"type": "text", "text": "💧 今日飲水目標", "size": "sm",
                      "color": C_TEXT_DARK, "weight": "bold", "flex": 3},
                     {"type": "text", "text": f"{water_target} ml", "size": "sm",
                      "color": C_TEXT_DARK, "flex": 3, "align": "end",
                      "weight": "bold"},
                 ]},
                # 久坐提醒
                {"type": "box", "layout": "horizontal",
                 "contents": [
                     {"type": "text", "text": "🪑 提醒", "size": "sm",
                      "color": C_TEXT_DARK, "weight": "bold", "flex": 3},
                     {"type": "text", "text": "每 2h 起來動一下", "size": "sm",
                      "color": C_TEXT_DARK, "flex": 4, "align": "end"},
                 ]},
                *goal_block,
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": COLOR_SLEEP,
                 "height": "sm", "flex": 1,
                 "action": {"type": "message", "label": "🌙 睡眠", "text": "睡眠"}},
                {"type": "button", "style": "primary", "color": COLOR_WATER,
                 "height": "sm", "flex": 1,
                 "action": {"type": "message", "label": "💧 喝水", "text": "飲水"}},
            ],
        },
    }
    return _flex("每日早報", body)


def evening_review_flex(profile: dict, water_ml: int, water_target: int,
                        workout_min: float, sleep_recent: Optional[dict],
                        goal: Optional[dict]) -> FlexMessage:
    """晚安回顧（21:00）：今日達成 + 反思引導。"""
    water_pct = min(100, int(water_ml * 100 / max(1, water_target)))
    today_str = date.today().strftime("%m/%d (%a)")

    coach_note = (
        "🎉 全部到位！今晚好好休息" if water_pct >= 90 and workout_min > 0
        else "💧 飲水沒達標，明天補回來" if water_pct < 60
        else "💪 動了就是贏了，繼續加油" if workout_min > 0
        else "🌱 今天沒動到也沒關係，明天再試"
    )

    goal_block = []
    if goal:
        desc = (goal.get("description") or "")[:24]
        goal_block = [
            {"type": "separator", "color": C_DIVIDER, "margin": "md"},
            {"type": "box", "layout": "horizontal", "margin": "md",
             "contents": [
                 {"type": "text", "text": "🎯 目標", "size": "sm",
                  "color": C_TEXT_DARK, "weight": "bold", "flex": 2},
                 {"type": "text", "text": desc, "size": "sm",
                  "color": C_TEXT_DARK, "flex": 5, "align": "end", "wrap": True},
             ]},
        ]

    body = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": COLOR_SLEEP,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🌙 晚安回顧",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": f"今晚 {today_str} 的小盤點",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                # 飲水
                {"type": "box", "layout": "vertical", "spacing": "xs",
                 "contents": [
                     {"type": "box", "layout": "horizontal",
                      "contents": [
                          {"type": "text", "text": "💧 今日飲水", "size": "sm",
                           "color": C_TEXT_DARK, "weight": "bold", "flex": 3},
                          {"type": "text",
                           "text": f"{water_ml}/{water_target} ml",
                           "size": "sm", "color": C_TEXT_DARK,
                           "flex": 3, "align": "end", "weight": "bold"},
                      ]},
                     _progress_bar(water_pct, COLOR_WATER),
                 ]},
                # 運動
                {"type": "box", "layout": "horizontal",
                 "contents": [
                     {"type": "text", "text": "💪 今日運動", "size": "sm",
                      "color": C_TEXT_DARK, "weight": "bold", "flex": 3},
                     {"type": "text",
                      "text": f"{int(workout_min)} 分鐘" if workout_min > 0 else "尚未紀錄",
                      "size": "sm", "color": C_TEXT_DARK,
                      "flex": 3, "align": "end"},
                 ]},
                *goal_block,
                {"type": "separator", "color": C_DIVIDER, "margin": "md"},
                {"type": "text", "text": coach_note,
                 "wrap": True, "size": "sm", "color": C_TEXT_DARK,
                 "align": "center", "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": C_ACCENT, "height": "sm",
                 "action": {"type": "message", "label": "📝 寫今日反思",
                            "text": "反思"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "message", "label": "看完整今日進度",
                            "text": "今日"}},
            ],
        },
    }
    return _flex("晚安回顧", body)


NOTIFY_META = {
    "morning": ("📰 每日早報",      "每日 09:00",                 C_PRIMARY),
    "evening": ("🌙 晚安回顧",      "每日 21:00",                 COLOR_SLEEP),
    "sleep":   ("🛌 早安睡眠回顧",  "每日 07:30",                 COLOR_SLEEP),
    "water":   ("💧 飲水提醒",      "每日 09 / 12 / 15 / 18",     COLOR_WATER),
    "stretch": ("🪑 久坐伸展",      "週一-五 11 / 14 / 16",       COLOR_STRETCH),
    "goal":    ("🎯 目標回顧",      "每日 21:00 / 週日 20:00",    COLOR_GOAL),
}


def _notify_flex(title: str, subtitle: str, keys: tuple, prefs: dict,
                 footer_extra: Optional[dict] = None) -> FlexMessage:
    rows = []
    for key in keys:
        t, sched, color = NOTIFY_META[key]
        is_on = bool(prefs.get(key, True))
        rows.append(_notify_row(t, sched, color, is_on, key))
        rows.append({"type": "separator", "color": C_DIVIDER})
    rows = rows[:-1]

    footer_contents = [
        {"type": "button", "style": "link", "height": "sm",
         "action": {"type": "message", "label": "回功能設定", "text": "功能設定"}},
    ]
    if footer_extra:
        footer_contents.insert(0, footer_extra)

    body = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": C_PRIMARY,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": title,
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": subtitle,
                 "color": "#FFFFFF", "size": "sm", "margin": "sm", "wrap": True},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": rows,
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": footer_contents,
        },
    }
    return _flex(title, body)


def notify_settings_flex(prefs: dict) -> FlexMessage:
    """簡易版：只顯示 每日早報 + 晚安回顧。"""
    return _notify_flex(
        "🔔 通知設定",
        "預設每天兩則：09:00 早報 + 21:00 晚安回顧。\n想要更細的時段，去進階模式 ↓",
        NOTIFY_SIMPLE_KEYS,
        prefs,
        footer_extra={
            "type": "button", "style": "secondary", "height": "sm",
            "action": {"type": "message",
                       "label": "⚙️ 進階通知設定", "text": "進階通知設定"},
        },
    )


def advanced_notify_settings_flex(prefs: dict) -> FlexMessage:
    """進階版：分開的 4 個推播時段。"""
    return _notify_flex(
        "⚙️ 進階通知設定",
        "每個項目獨立時段。需要請手動開啟。",
        NOTIFY_ADVANCED_KEYS,
        prefs,
    )


def _notify_row(title: str, sched: str, color: str, is_on: bool, key: str) -> dict:
    state_label = "🟢 ON" if is_on else "⚪ OFF"
    btn_style = "primary" if is_on else "secondary"
    btn_color = color if is_on else "#BBBBBB"
    return {
        "type": "box", "layout": "horizontal", "spacing": "md",
        "contents": [
            {"type": "box", "layout": "vertical", "flex": 5,
             "contents": [
                 {"type": "text", "text": title, "size": "sm",
                  "weight": "bold", "color": C_TEXT_DARK},
                 {"type": "text", "text": sched, "size": "xs",
                  "color": C_TEXT_SOFT, "margin": "xs"},
                 {"type": "text", "text": state_label, "size": "xs",
                  "color": (color if is_on else C_TEXT_SOFT),
                  "margin": "xs", "weight": "bold"},
             ]},
            {"type": "button", "style": btn_style, "color": btn_color,
             "height": "sm", "flex": 3, "gravity": "center",
             "action": {"type": "postback",
                        "label": "關閉" if is_on else "開啟",
                        "data": f"action=notify_toggle&type={key}",
                        "displayText": f"{'關閉' if is_on else '開啟'} {title}"}},
        ],
    }


# ============================================================
# 6. Quick Reply 與選單工具
# ============================================================


def qr(*items: tuple[str, str]) -> QuickReply:
    """items: list of (label, text)"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label=label, text=text))
        for label, text in items
    ])


def qr_postback(*items: tuple[str, str, str]) -> QuickReply:
    """items: list of (label, data, displayText)"""
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label=label, data=data, display_text=display))
        for label, data, display in items
    ])


def parse_postback(data: str) -> dict:
    """action=log_water&amount=350 → {'action': 'log_water', 'amount': '350'}"""
    out = {}
    for pair in data.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k] = v
    return out


# ============================================================
# 7. 回覆與推播工具
# ============================================================


def reply(reply_token: str, messages: list) -> None:
    if not isinstance(messages, list):
        messages = [messages]
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )


def reply_text(reply_token: str, text: str, quick_reply: Optional[QuickReply] = None) -> None:
    msg = TextMessage(text=text, quick_reply=quick_reply) if quick_reply else TextMessage(text=text)
    reply(reply_token, [msg])


def push(user_id: str, messages: list) -> None:
    if not isinstance(messages, list):
        messages = [messages]
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=messages)
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("push 失敗 user=%s err=%s", user_id, exc)


# ============================================================
# 8. 個人資料 / TDEE 精靈式設定
# ============================================================

PROFILE_STEPS = ["gender", "age", "height", "weight", "activity",
                 "target", "eating_style", "vegetarian", "workout_time"]


def start_profile_setup(user_id: str, reply_token: str) -> None:
    set_state(user_id, "profile_setup", "gender", {})
    reply_text(
        reply_token,
        "✨ 來建檔吧！我會幫你算出每日能量基準線（TDEE）。\n\n第 1 題：你的生理性別？",
        qr(("👨 男", "男"), ("👩 女", "女")),
    )


def handle_profile_setup(user_id: str, text: str, reply_token: str, state: dict) -> None:
    step = state["step"]
    data = state["data"] or {}

    if step == "gender":
        if text in ("男", "👨 男", "male"):
            data["gender"] = "male"
        elif text in ("女", "👩 女", "female"):
            data["gender"] = "female"
        else:
            reply_text(reply_token, "請點按鈕選 👨 或 👩",
                       qr(("👨 男", "男"), ("👩 女", "女")))
            return
        set_state(user_id, "profile_setup", "age", data)
        reply_text(reply_token, "第 2 題：年齡幾歲？（直接打數字）")
        return

    if step == "age":
        try:
            data["age"] = int(text)
        except ValueError:
            reply_text(reply_token, "年齡要打數字喔，例如 25")
            return
        set_state(user_id, "profile_setup", "height", data)
        reply_text(reply_token, "第 3 題：身高幾公分？（例如 170）")
        return

    if step == "height":
        try:
            data["height_cm"] = float(text)
        except ValueError:
            reply_text(reply_token, "身高要打數字，例如 170")
            return
        set_state(user_id, "profile_setup", "weight", data)
        reply_text(reply_token, "第 4 題：體重幾公斤？（例如 65）")
        return

    if step == "weight":
        try:
            data["weight_kg"] = float(text)
        except ValueError:
            reply_text(reply_token, "體重要打數字，例如 65")
            return
        set_state(user_id, "profile_setup", "activity", data)
        reply_text(
            reply_token,
            "第 5 題：每週運動頻率？",
            qr(
                ("🛋️ 久坐", "久坐"),
                ("🚶 輕度 1-3", "輕度"),
                ("🏃 中度 3-5", "中度"),
                ("💪 高度 6-7", "高度"),
                ("🔥 極高", "極高"),
            ),
        )
        return

    if step == "activity":
        mapping = {
            "久坐": "sedentary", "輕度": "light", "中度": "moderate",
            "高度": "active", "極高": "very_active",
        }
        if text not in mapping:
            reply_text(reply_token, "請從按鈕選一個運動頻率")
            return
        data["activity_level"] = mapping[text]
        set_state(user_id, "profile_setup", "target", data)
        reply_text(
            reply_token,
            "第 6 題：目標方向？",
            qr(("🔥 增肌", "增肌"), ("⚖️ 維持", "維持"), ("✂️ 減脂", "減脂")),
        )
        return

    if step == "target":
        mapping = {"增肌": "bulk", "維持": "maintain", "減脂": "cut"}
        if text not in mapping:
            reply_text(reply_token, "請選增肌 / 維持 / 減脂")
            return
        data["target_type"] = mapping[text]
        set_state(user_id, "profile_setup", "eating_style", data)
        reply_text(reply_token, "第 7 題：主要用餐型態？",
                   qr(("🍱 外食族", "外食"), ("🥘 自己煮", "自煮")))
        return

    if step == "eating_style":
        if text in ("外食", "外食族"):
            data["eating_style"] = "outside"
        elif text in ("自煮", "自己煮"):
            data["eating_style"] = "home"
        else:
            reply_text(reply_token, "請選外食 / 自煮",
                       qr(("🍱 外食族", "外食"), ("🥘 自己煮", "自煮")))
            return
        set_state(user_id, "profile_setup", "vegetarian", data)
        reply_text(reply_token, "第 8 題：你是素食者嗎？",
                   qr(("🥬 是", "素食"), ("🍖 否", "葷食")))
        return

    if step == "vegetarian":
        data["is_vegetarian"] = (text == "素食")
        set_state(user_id, "profile_setup", "workout_time", data)
        reply_text(
            reply_token,
            "最後一題：通常什麼時段運動？",
            qr(("🌅 晨練", "晨練"), ("☀️ 下午練", "下午練"), ("🌙 夜練", "夜練")),
        )
        return

    if step == "workout_time":
        mapping = {"晨練": "morning", "下午練": "afternoon", "夜練": "evening"}
        if text not in mapping:
            reply_text(reply_token, "請選晨練 / 下午練 / 夜練")
            return
        data["workout_time"] = mapping[text]
        finalize_profile(user_id, data, reply_token)
        return


def finalize_profile(user_id: str, data: dict, reply_token: str) -> None:
    bmr = calc_bmr(data["gender"], data["weight_kg"], data["height_cm"], data["age"])
    tdee = calc_tdee(bmr, data["activity_level"])
    target_kcal = tdee + TARGET_KCAL_DELTA.get(data["target_type"], 0)
    p_g, c_g, f_g = calc_macros(target_kcal, data["target_type"])
    daily_water = calc_daily_water_ml(data["weight_kg"])

    fields = {
        **data,
        "bmr": round(bmr, 1),
        "tdee": round(tdee, 1),
        "target_kcal": round(target_kcal, 1),
        "protein_g": p_g,
        "carb_g": c_g,
        "fat_g": f_g,
        "daily_water_ml": daily_water,
    }
    upsert_profile(user_id, fields)
    clear_state(user_id)

    profile = get_profile(user_id)
    reply(reply_token, [
        TextMessage(text="✨ 檔案建好了！這是你的能量基準線 👇"),
        tdee_result_flex(profile),
    ])


# ============================================================
# 9. 目標設定與追蹤
# ============================================================


def start_goal_flow(user_id: str, reply_token: str, force_new: bool = False) -> None:
    if not force_new:
        existing = get_active_goal(user_id)
        if existing:
            reply(reply_token, [
                TextMessage(text="這是你上次設定的目標："),
                goal_card_flex(existing),
                TextMessage(text="想要「繼續舊目標」還是「設新目標」？",
                            quick_reply=qr(("繼續舊目標", "繼續舊目標"), ("設新目標", "新目標"))),
            ])
            return
    set_state(user_id, "goal_setting", "describe", {})
    reply_text(
        reply_token,
        "我們開始設定新目標。\n想達成什麼？請用一段話描述，可以包含「想要的成果」與「希望什麼時候完成」。\n"
        "例如：我想在 3 個月內減脂 5 公斤，並穩定能做 10 下標準伏地挺身。",
    )


def handle_goal_setting(user_id: str, text: str, reply_token: str, state: dict) -> None:
    step = state["step"]
    data = state["data"] or {}

    if step == "describe":
        data["description"] = text
        review = claude_smart_review(text)
        data["smart_specific"] = text
        set_state(user_id, "goal_setting", "measurable", data)
        reply(reply_token, [
            TextMessage(text=f"教練看完你的目標：\n\n{review}"),
            TextMessage(text="接下來，請告訴我「具體可量化的指標」是什麼？\n"
                             "例如：體脂率降到 18%、深蹲 1RM 100kg、跑 5K 在 30 分內。"),
        ])
        return

    if step == "measurable":
        data["smart_measurable"] = text
        set_state(user_id, "goal_setting", "deadline", data)
        reply_text(
            reply_token,
            "期限訂在哪一天？請用 YYYY-MM-DD 格式，例如 2026-09-30。",
        )
        return

    if step == "deadline":
        try:
            datetime.strptime(text, "%Y-%m-%d")
            data["deadline"] = text
        except ValueError:
            reply_text(reply_token, "格式不太對，請用 YYYY-MM-DD，例如 2026-09-30。")
            return
        set_state(user_id, "goal_setting", "first_step", data)
        reply_text(
            reply_token,
            "本週的「第一步」是什麼？挑一個 7 天內可完成的小行動。\n"
            "例如：每週重訓 3 次、把外食改 2 餐為高蛋白便當。",
        )
        return

    if step == "first_step":
        data["first_step"] = text
        set_state(user_id, "goal_setting", "milestones", data)
        reply_text(
            reply_token,
            "再來訂個「里程碑」（中途檢查點）。\n例如：第 4 週體脂量降 1.5%、第 8 週深蹲達 90kg。",
        )
        return

    if step == "milestones":
        data["milestones"] = text
        set_state(user_id, "goal_setting", "review_freq", data)
        reply_text(
            reply_token,
            "希望我多久提醒你回顧一次？",
            qr(("每日提醒", "每日提醒"), ("每週提醒", "每週提醒")),
        )
        return

    if step == "review_freq":
        if "每日" in text:
            data["review_freq"] = "daily"
        elif "每週" in text:
            data["review_freq"] = "weekly"
        else:
            reply_text(reply_token, "請選每日或每週。",
                       qr(("每日提醒", "每日提醒"), ("每週提醒", "每週提醒")))
            return

        # 把舊的 active 目標標為完成或先放著（教學上保留歷史，這裡先標 abandoned）
        supabase.table("goals").update({"status": "abandoned"}).eq(
            "user_id", user_id).eq("status", "active").execute()

        goal = insert_goal(user_id, {
            "description": data["description"],
            "smart_specific": data.get("smart_specific"),
            "smart_measurable": data.get("smart_measurable"),
            "deadline": data.get("deadline"),
            "first_step": data.get("first_step"),
            "milestones": data.get("milestones"),
            "review_freq": data["review_freq"],
            "status": "active",
        })
        clear_state(user_id)

        reply(reply_token, [
            TextMessage(text="✨ 目標已存入記憶庫。"),
            goal_card_flex(goal),
            TextMessage(text="我會定時提醒你回顧。下次見面時，我們再看你進度到哪 💪"),
        ])
        return


# ============================================================
# 10. 自我成長與習慣
# ============================================================


def self_growth_menu(reply_token: str) -> None:
    reply_text(
        reply_token,
        "🌱 今天想紀錄什麼？",
        qr(
            ("💪 運動打卡", "運動打卡"),
            ("💧 喝水", "飲水"),
            ("🌙 睡眠", "睡眠紀錄"),
            ("📝 寫反思", "反思"),
            ("🚫 壞習慣", "壞習慣"),
            ("📚 知識補給", "健身知識"),
            ("📊 今日進度", "今日"),
        ),
    )


def show_water_card(user_id: str, reply_token: str) -> None:
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」設定一下檔案。")
        return
    current = int(get_today_habit_sum(user_id, "water"))
    target = profile.get("daily_water_ml") or 2000
    reply(reply_token, [water_card_flex(current, target)])


def show_sleep_card(reply_token: str) -> None:
    reply(reply_token, [sleep_card_flex()])


def checkin_menu(reply_token: str) -> None:
    """打卡子選單：運動 / 睡眠。"""
    reply_text(
        reply_token,
        "✅ 想打哪一張卡？",
        qr(
            ("💪 運動打卡", "運動打卡"),
            ("🌙 睡眠打卡", "睡眠打卡"),
        ),
    )


def fitness_section_placeholder(reply_token: str) -> None:
    """健身區（內容建置中）。"""
    reply_text(
        reply_token,
        "🏋️ 健身專區建置中⋯\n敬請期待 ✨\n\n暫時可用「打卡」紀錄今天的運動分鐘。",
    )


def start_workout_log(user_id: str, reply_token: str) -> None:
    set_state(user_id, "workout_log", "minutes", {})
    reply_text(reply_token, "💪 今天動了幾分鐘？（直接打數字）")


def handle_workout_log(user_id: str, text: str, reply_token: str, state: dict) -> None:
    try:
        minutes = float(text)
    except ValueError:
        reply_text(reply_token, "請打數字喔，例如 45")
        return
    log_habit(user_id, "workout", amount=minutes)
    clear_state(user_id)
    reply_text(reply_token,
               f"📒 紀錄完成：今天 {int(minutes)} 分鐘 ✅\n動了就是贏了 💪")


def bad_habit_menu(reply_token: str) -> None:
    reply_text(
        reply_token,
        "🚫 紀錄一下發生了什麼？\n紀錄不是責備，是讓你下次更清楚 ✨",
        qr(("😴 熬夜", "熬夜紀錄"), ("🍔 暴食", "暴食紀錄"), ("🛋️ 沒動", "缺乏運動紀錄")),
    )


def quick_log_bad_habit(user_id: str, text: str, reply_token: str) -> None:
    mapping = {
        "熬夜紀錄": ("late_night", "已紀錄熬夜 ⏰\n睡眠 = 修復時間 = 變強的開關。\n今晚提早 30 分鐘關燈試試 🌙"),
        "暴食紀錄": ("binge", "已紀錄 🍱\n下次嘴饞前，先喝一杯水 + 走 5 分鐘。\n通常 80% 的渴望會自己消失 ✨"),
        "缺乏運動紀錄": ("no_exercise", "已紀錄 🛋️\n沒事，明天起來做 10 下深蹲就算開始。\n小開始 > 大計畫 💪"),
    }
    type_, msg = mapping[text]
    log_habit(user_id, type_)
    reply_text(reply_token, msg)


def start_reflection(user_id: str, reply_token: str) -> None:
    set_state(user_id, "reflection", "period", {})
    reply_text(
        reply_token,
        "📝 要寫哪一種反思？",
        qr(("🏋️ 訓練心得", "訓練心得"), ("📆 週回顧", "週回顧"), ("📅 月回顧", "月回顧")),
    )


def handle_reflection(user_id: str, text: str, reply_token: str, state: dict) -> None:
    step = state["step"]
    data = state["data"] or {}

    if step == "period":
        mapping = {"訓練心得": "training", "週回顧": "week", "月回顧": "month"}
        if text not in mapping:
            reply_text(reply_token, "請選訓練心得 / 週回顧 / 月回顧。")
            return
        data["period"] = mapping[text]
        set_state(user_id, "reflection", "content", data)
        reply_text(reply_token,
                   "好的，把這次的成功、失敗、收穫寫下來吧，我會幫你做一個小總結。")
        return

    if step == "content":
        summary = claude_reflection_summary(data["period"], text)
        insert_reflection(user_id, data["period"], text, summary)
        clear_state(user_id)
        reply_text(reply_token, f"📌 教練看完你的反思：\n\n{summary}")
        return


def show_today_progress(user_id: str, reply_token: str) -> None:
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」建立檔案再來看進度 🙏")
        return
    water_ml = int(get_today_habit_sum(user_id, "water"))
    water_target = profile.get("daily_water_ml") or 2000
    sleep_recent = get_latest_habit(user_id, "sleep")
    workout_min = get_today_habit_sum(user_id, "workout")
    goal = get_active_goal(user_id)
    reply(reply_token, [
        today_progress_flex(profile, water_ml, water_target,
                            sleep_recent, workout_min, goal),
    ])


def share_fitness_knowledge(reply_token: str) -> None:
    text = claude_ask(
        "請用繁體中文，給一條今天的「健身知識每日一則」，限 3 句內，要有具體可執行的小建議。",
    )
    reply_text(reply_token, f"📚 今日健身知識：\n\n{text}")


# ============================================================
# 11. 飲食與健康
# ============================================================


def diet_menu(reply_token: str) -> None:
    reply_text(
        reply_token,
        "🥗 想看哪個？",
        qr(
            ("📊 營養素比例", "營養素比例"),
            ("🍱 午餐建議", "午餐建議"),
            ("🍽️ 晚餐建議", "晚餐建議"),
            ("🍌 運動前點心", "運動前點心"),
            ("🥛 運動後點心", "運動後點心"),
            ("📖 點心衛教", "點心衛教"),
        ),
    )


def show_macros(user_id: str, reply_token: str) -> None:
    profile = get_profile(user_id)
    if not profile or not profile.get("target_kcal"):
        reply_text(reply_token, "請先輸入「個人資料」建立 TDEE 檔案。")
        return
    reply(reply_token, [macro_visual_flex(profile)])


def show_meal_suggestion(user_id: str, slot: str, reply_token: str) -> None:
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」建立檔案。")
        return
    text = claude_meal_suggestion(profile, slot)
    reply_text(reply_token, f"🍱 {slot}建議：\n\n{text}")


def show_workout_snack(user_id: str, when: str, reply_token: str) -> None:
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」建立檔案。")
        return
    text = claude_workout_snack(profile, when)
    reply_text(reply_token, f"⚡ {when}：\n\n{text}")


# ============================================================
# 11.5 功能設定（撤銷 / 重設 / 刪除）
# ============================================================


def data_mgmt_menu(reply_token: str) -> None:
    reply_text(
        reply_token,
        "⚙️ 想做什麼？",
        qr(
            ("🔔 通知設定", "通知設定"),
            ("⚙️ 進階通知設定", "進階通知設定"),
            ("↩️ 撤銷飲水", "撤銷飲水"),
            ("↩️ 撤銷睡眠", "撤銷睡眠"),
            ("↩️ 撤銷運動", "撤銷運動"),
            ("↩️ 撤銷伸展", "撤銷伸展"),
            ("↩️ 撤銷反思", "撤銷反思"),
            ("🎯 放棄目標", "放棄目標"),
            ("🔄 重設 TDEE", "重設資料"),
            ("⚠️ 刪除全部", "刪除全部"),
        ),
    )


def show_notify_settings(user_id: str, reply_token: str) -> None:
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」建立檔案 🙏")
        return
    prefs = get_notify_prefs(user_id)
    reply(reply_token, [notify_settings_flex(prefs)])


def show_advanced_notify_settings(user_id: str, reply_token: str) -> None:
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」建立檔案 🙏")
        return
    prefs = get_notify_prefs(user_id)
    reply(reply_token, [
        TextMessage(
            text="⚙️ 進階通知設定\n\n這裡可以「個別」開啟原本的分時段提醒（會跟每日早報/晚安回顧並存）。"
                 "預設都是關閉，避免一天被打擾太多次。",
        ),
        advanced_notify_settings_flex(prefs),
    ])


def undo_latest_habit(user_id: str, type_: str, label: str, reply_token: str) -> None:
    record = delete_latest_habit(user_id, type_)
    if not record:
        reply_text(reply_token, f"找不到{label}紀錄可以撤銷。")
        return
    detail = ""
    if record.get("amount") is not None:
        detail = f"（{int(record['amount'])}）"
    elif record.get("quality"):
        detail = f"（{record['quality']}）"
    reply_text(reply_token,
               f"↩️ 已撤銷最近一筆{label}紀錄{detail}。\n輸入「功能設定」回到選單。")


def undo_latest_reflection_cmd(user_id: str, reply_token: str) -> None:
    record = delete_latest_reflection(user_id)
    if not record:
        reply_text(reply_token, "找不到反思紀錄可以撤銷。")
        return
    period_label = {"training": "訓練心得", "week": "週回顧", "month": "月回顧"}.get(
        record.get("period", ""), "反思")
    reply_text(reply_token, f"↩️ 已撤銷最近一筆「{period_label}」。")


def abandon_goal_cmd(user_id: str, reply_token: str) -> None:
    n = abandon_active_goals(user_id)
    if n == 0:
        reply_text(reply_token, "你目前沒有正在進行的目標。")
    else:
        reply_text(reply_token,
                   f"已放棄 {n} 個目標（紀錄保留為 abandoned）。輸入「新目標」重新設定。")


def reset_profile_cmd(user_id: str, reply_token: str) -> None:
    """重新跑 TDEE 設定精靈（upsert 會覆蓋舊值）。"""
    start_profile_setup(user_id, reply_token)


def request_wipe_confirm(user_id: str, reply_token: str) -> None:
    """第一次確認：要使用者打『確定刪除全部』。"""
    set_state(user_id, "wipe_all", "confirm", {})
    reply_text(
        reply_token,
        "⚠️ 你確定要刪除「全部資料」嗎？\n"
        "包含：個人資料 / 目標 / 所有打卡 / 所有反思。\n"
        "這個動作無法復原。\n\n"
        "如果確定，請輸入「**確定刪除全部**」（一字不差）。\n"
        "輸入其他任何文字或「取消」即取消。",
    )


def handle_wipe_confirm(user_id: str, text: str, reply_token: str) -> None:
    clear_state(user_id)
    if text.strip() != "確定刪除全部":
        reply_text(reply_token, "已取消，沒有刪除任何資料。")
        return
    counts = wipe_all_user_data(user_id)
    detail = " / ".join(f"{k}:{v}" for k, v in counts.items())
    reply_text(reply_token,
               f"🗑️ 已刪除全部資料。\n細項：{detail}\n\n"
               "如果之後想再用，輸入「個人資料」重新開始 ✨")


def snack_education(reply_token: str) -> None:
    text = (
        "📖 運動點心衛教\n\n"
        "🍌 運動前（30-60 分鐘）\n"
        "  • 選高升糖、易消化的碳水（香蕉、吐司、燕麥）\n"
        "  • 避免油脂與高纖維，會壓垮腸胃、影響表現\n\n"
        "🥛 運動後（30 分鐘內）\n"
        "  • 蛋白質 + 碳水快充，幫肌肉啟動修復\n"
        "  • 乳清 + 香蕉、無糖豆漿 + 茶葉蛋都是經典組合\n\n"
        "❌ 迷思：「運動後吃東西會變胖」\n"
        "  → 反之，胰島素分泌會把營養帶進肌肉。空腹反而會分解肌肉。"
    )
    reply_text(reply_token, text)


# ============================================================
# 12. 主路由：文字訊息 / Postback / Follow
# ============================================================

WELCOME = (
    "嗨，我是你的隨身健身教練 💪✨\n\n"
    "請先輸入「個人資料」算 TDEE，\n"
    "再輸入「選單」開始使用 ☺️"
)


@handler.add(FollowEvent)
def handle_follow(event):
    reply(event.reply_token, [welcome_flex()])


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    reply_token = event.reply_token

    try:
        _route_text(user_id, text, reply_token)
    except Exception as exc:  # noqa: BLE001
        logger.exception("route error: %s", exc)
        reply_text(reply_token, "教練暫時打結了 🤯 請稍後再試或輸入「選單」回到主畫面。")


def _route_text(user_id: str, text: str, reply_token: str) -> None:
    state = get_state(user_id)

    # 1) 萬用退出指令
    if text in ("選單", "menu", "主選單"):
        clear_state(user_id)
        reply(reply_token, [TextMessage(text="主選單："), main_menu_flex()])
        return
    if text in ("取消", "cancel", "結束"):
        clear_state(user_id)
        reply_text(reply_token, "已取消目前流程。輸入「選單」回主畫面。")
        return

    # 2) 進行中的精靈式流程優先處理
    if state and state.get("flow"):
        flow = state["flow"]
        if flow == "profile_setup":
            handle_profile_setup(user_id, text, reply_token, state)
            return
        if flow == "goal_setting":
            handle_goal_setting(user_id, text, reply_token, state)
            return
        if flow == "workout_log":
            handle_workout_log(user_id, text, reply_token, state)
            return
        if flow == "reflection":
            handle_reflection(user_id, text, reply_token, state)
            return
        if flow == "wipe_all":
            handle_wipe_confirm(user_id, text, reply_token)
            return

    # 3) 沒有狀態 → 走頂層指令
    # 個人資料
    if text in ("個人資料", "TDEE", "重新設定"):
        start_profile_setup(user_id, reply_token)
        return

    # 三大入口
    if text in ("目標設定", "🎯 目標設定", "目標設定與追蹤"):
        start_goal_flow(user_id, reply_token)
        return
    if text in ("新目標",):
        start_goal_flow(user_id, reply_token, force_new=True)
        return
    if text == "繼續舊目標":
        existing = get_active_goal(user_id)
        if existing:
            reply(reply_token, [TextMessage(text="繼續加油 💪"), goal_card_flex(existing)])
        else:
            reply_text(reply_token, "找不到舊目標，幫你開新的吧！")
            start_goal_flow(user_id, reply_token, force_new=True)
        return
    if text in ("我的目標", "看目標"):
        existing = get_active_goal(user_id)
        if existing:
            reply(reply_token, [goal_card_flex(existing)])
        else:
            reply_text(reply_token, "你目前沒有正在進行的目標，輸入「目標設定」開始。")
        return

    if text in ("自我成長", "自我成長與習慣", "🌱 自我成長"):
        self_growth_menu(reply_token)
        return
    if text in ("打卡", "✅ 打卡"):
        checkin_menu(reply_token)
        return
    if text in ("運動打卡", "💪 運動打卡"):
        start_workout_log(user_id, reply_token)
        return
    if text in ("飲水", "💧 飲水", "飲水紀錄"):
        show_water_card(user_id, reply_token)
        return
    if text in ("睡眠紀錄", "🌙 睡眠紀錄", "睡眠", "睡眠打卡", "🌙 睡眠打卡"):
        show_sleep_card(reply_token)
        return
    if text in ("健身", "🏋️ 健身", "健身專區"):
        fitness_section_placeholder(reply_token)
        return
    if text in ("反思", "📝 反思"):
        start_reflection(user_id, reply_token)
        return
    if text in ("壞習慣", "🚫 壞習慣紀錄"):
        bad_habit_menu(reply_token)
        return
    if text in ("熬夜紀錄", "暴食紀錄", "缺乏運動紀錄"):
        quick_log_bad_habit(user_id, text, reply_token)
        return
    if text in ("健身知識", "📚 學習一則"):
        share_fitness_knowledge(reply_token)
        return
    if text in ("今日", "今日進度", "📊 今日進度", "進度"):
        show_today_progress(user_id, reply_token)
        return

    if text in ("飲食與健康", "🥗 飲食與健康", "飲食"):
        diet_menu(reply_token)
        return

    # 功能設定 / 刪除
    if text in ("功能設定", "⚙️ 功能設定", "刪除", "刪除資料"):
        data_mgmt_menu(reply_token)
        return
    if text in ("通知設定", "🔔 通知設定", "推播設定"):
        show_notify_settings(user_id, reply_token)
        return
    if text in ("進階通知設定", "⚙️ 進階通知設定", "進階通知"):
        show_advanced_notify_settings(user_id, reply_token)
        return
    if text == "撤銷飲水":
        undo_latest_habit(user_id, "water", "飲水", reply_token)
        return
    if text == "撤銷睡眠":
        undo_latest_habit(user_id, "sleep", "睡眠", reply_token)
        return
    if text == "撤銷運動":
        undo_latest_habit(user_id, "workout", "運動", reply_token)
        return
    if text == "撤銷伸展":
        undo_latest_habit(user_id, "stretch", "伸展", reply_token)
        return
    if text == "撤銷反思":
        undo_latest_reflection_cmd(user_id, reply_token)
        return
    if text in ("放棄目標", "🎯 放棄目前目標"):
        abandon_goal_cmd(user_id, reply_token)
        return
    if text in ("重設資料", "重設個人資料", "重設 TDEE"):
        reset_profile_cmd(user_id, reply_token)
        return
    if text in ("刪除全部", "刪除全部資料", "⚠️ 刪除全部資料"):
        request_wipe_confirm(user_id, reply_token)
        return
    if text == "營養素比例":
        show_macros(user_id, reply_token)
        return
    if text in ("午餐建議",):
        show_meal_suggestion(user_id, "午餐", reply_token)
        return
    if text in ("晚餐建議",):
        show_meal_suggestion(user_id, "晚餐", reply_token)
        return
    if text == "運動前點心":
        show_workout_snack(user_id, "運動前點心", reply_token)
        return
    if text == "運動後點心":
        show_workout_snack(user_id, "運動後點心", reply_token)
        return
    if text in ("點心衛教",):
        snack_education(reply_token)
        return

    # 4) 第一次互動或無法判讀 → 視 profile 狀態決定
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, WELCOME,
                   qr(("⚙️ 個人資料", "個人資料"), ("📋 看主選單", "選單")))
        return

    # 5) 自由提問 → 丟給 Claude 當教練回答
    answer = claude_ask(
        text,
        system=(
            "你是繁體中文的健身教練，回答簡潔（5 行內），給具體可執行的建議，"
            "並可以提醒使用者輸入「選單」回到主畫面。"
        ),
    )
    reply_text(reply_token, answer)


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    params = parse_postback(event.postback.data)
    action = params.get("action")

    try:
        if action == "log_water":
            amount = int(params.get("amount", 0))
            log_habit(user_id, "water", amount=amount)
            profile = get_profile(user_id)
            target = (profile or {}).get("daily_water_ml") or 2000
            current = int(get_today_habit_sum(user_id, "water"))
            reply(reply_token, [
                TextMessage(text=f"🥤 乾杯！今天累積 {current} / {target} ml"),
                water_card_flex(current, target),
            ])
            return

        if action == "log_sleep":
            quality = params.get("quality", "normal")
            log_habit(user_id, "sleep", quality=quality)
            label = {"good": "睡得好 😴", "normal": "普通 😐", "bad": "睡得不好 😣"}.get(quality, "")
            reply_text(reply_token,
                       f"已紀錄昨晚 {label}。\n如果想補記睡眠時數，輸入「睡眠 7.5」這樣的格式即可。")
            return

        if action == "log_stretch":
            log_habit(user_id, "stretch")
            reply_text(reply_token, "🙌 動起來啦！記下了，下個 2 小時再叫你。")
            return

        if action == "goal_done":
            supabase.table("goals").update({"status": "done"}).eq(
                "user_id", user_id).eq("status", "active").execute()
            reply_text(reply_token,
                       "🎉 恭喜完成目標！要不要設下一個？輸入「新目標」開始。")
            return

        if action == "notify_toggle":
            key = params.get("type", "")
            if key not in NOTIFY_KEYS:
                reply_text(reply_token, "未知的通知類型 🤔")
                return
            profile = get_profile(user_id)
            if not profile:
                reply_text(reply_token, "請先輸入「個人資料」建立檔案 🙏")
                return
            prefs = get_notify_prefs(user_id)
            new_val = not prefs[key]
            set_notify_pref(user_id, key, new_val)
            prefs[key] = new_val
            title = NOTIFY_META[key][0]
            # 切換進階項目就回進階卡，否則回簡易卡
            card = (advanced_notify_settings_flex(prefs)
                    if key in NOTIFY_ADVANCED_KEYS
                    else notify_settings_flex(prefs))
            reply(reply_token, [
                TextMessage(text=f"{'🟢 已開啟' if new_val else '⚪ 已關閉'} {title}"),
                card,
            ])
            return
    except Exception as exc:  # noqa: BLE001
        logger.exception("postback error: %s", exc)
        reply_text(reply_token, "教練處理時打結了 🤯 請稍後再試。")


# ============================================================
# 13. APScheduler — 定時推播
# ============================================================


def job_daily_briefing():
    """每日早報 09:00 — 簡易模式主推播。"""
    uids = users_with_notify_on("morning")
    logger.info("[scheduler] 推播每日早報 → %d 人", len(uids))
    for uid in uids:
        profile = get_profile(uid)
        if not profile:
            continue
        sleep_recent = get_latest_habit(uid, "sleep")
        goal = get_active_goal(uid)
        water_target = profile.get("daily_water_ml") or 2000
        push(uid, [daily_briefing_flex(profile, water_target, sleep_recent, goal)])


def job_evening_review():
    """晚安回顧 21:00 — 簡易模式主推播。"""
    uids = users_with_notify_on("evening")
    logger.info("[scheduler] 推播晚安回顧 → %d 人", len(uids))
    for uid in uids:
        profile = get_profile(uid)
        if not profile:
            continue
        water_ml = int(get_today_habit_sum(uid, "water"))
        water_target = profile.get("daily_water_ml") or 2000
        workout_min = get_today_habit_sum(uid, "workout")
        sleep_recent = get_latest_habit(uid, "sleep")
        goal = get_active_goal(uid)
        push(uid, [
            evening_review_flex(profile, water_ml, water_target,
                                workout_min, sleep_recent, goal),
        ])


def job_morning_sleep_recap():
    """進階：原本 07:30 的單獨睡眠回顧（預設關，僅給手動開的人）。"""
    uids = users_with_notify_on("sleep")
    logger.info("[scheduler] 進階：早安睡眠回顧 → %d 人", len(uids))
    for uid in uids:
        push(uid, [
            TextMessage(text="早安 ☀️ 先做今天的第一個紀錄："),
            sleep_card_flex(),
        ])


def job_water_reminder():
    uids = users_with_notify_on("water")
    logger.info("[scheduler] 推播飲水提醒 → %d 人", len(uids))
    for uid in uids:
        profile = get_profile(uid)
        if not profile:
            continue
        current = int(get_today_habit_sum(uid, "water"))
        target = profile.get("daily_water_ml") or 2000
        push(uid, [water_card_flex(current, target)])


def job_stretch_reminder():
    now = datetime.now()
    if now.weekday() >= 5:  # 週六日不推
        return
    uids = users_with_notify_on("stretch")
    logger.info("[scheduler] 推播久坐伸展 → %d 人", len(uids))
    for uid in uids:
        push(uid, [stretch_card_flex()])


def job_goal_review(freq: str):
    notify_uids = set(users_with_notify_on("goal"))
    targets = [g for g in users_with_goal_review(freq) if g["user_id"] in notify_uids]
    logger.info("[scheduler] 推播目標回顧 freq=%s → %d 人", freq, len(targets))
    for goal in targets:
        uid = goal["user_id"]
        push(uid, [
            TextMessage(text="該回顧目標進度啦 🎯"),
            goal_card_flex(goal),
        ])


def init_scheduler():
    sched = BackgroundScheduler(timezone="Asia/Taipei")
    # 簡易模式（預設開啟）
    sched.add_job(job_daily_briefing, "cron", hour=9, minute=0, id="morning_briefing")
    sched.add_job(job_evening_review, "cron", hour=21, minute=0, id="evening_review")
    # 進階模式（預設關閉，使用者手動開）
    sched.add_job(job_morning_sleep_recap, "cron", hour=7, minute=30, id="sleep_recap")
    sched.add_job(job_water_reminder, "cron", hour="9,12,15,18", minute=0, id="water")
    sched.add_job(job_stretch_reminder, "cron",
                  day_of_week="mon-fri", hour="11,14,16", minute=0, id="stretch")
    sched.add_job(lambda: job_goal_review("daily"), "cron",
                  hour=21, minute=30, id="goal_daily")  # 移到 21:30 避開晚安回顧
    sched.add_job(lambda: job_goal_review("weekly"), "cron",
                  day_of_week="sun", hour=20, minute=0, id="goal_weekly")
    sched.start()
    logger.info("APScheduler started, jobs=%s", [j.id for j in sched.get_jobs()])
    return sched


# ============================================================
# 14. Flask Webhook 端點
# ============================================================


@app.route("/", methods=["GET"])
def home():
    return {"message": "Fitness LINE Bot is running", "status": "ok"}


@app.route("/", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logger.info("Request body: %s", body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning("Invalid signature.")
        abort(400)
    return "OK"


# ============================================================
# 15. 啟動 Scheduler
# ============================================================
# 注意：gunicorn 啟動時請用 -w 1（單 worker），否則排程會被多個 worker 重複觸發。
# Dockerfile 已經設定好了。

scheduler = init_scheduler()
