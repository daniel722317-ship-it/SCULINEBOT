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
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")

import httpx

import markdown
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from flask import Flask, abort, request, send_from_directory
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
# HF Space 自動注入；本機 dev 沒設就 fallback
SPACE_HOST = os.getenv("SPACE_HOST", "daniel931101-sculinebot.hf.space")

MUSCLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "muscles")

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
def log_strength(user_id: str, exercise: str, weight_kg: float,
                 reps: int, sets: int = 1) -> dict:
    """新增一筆力量紀錄。回傳含計算後 1RM 的列。"""
    one_rm = round(calc_one_rm(weight_kg, reps), 1)
    res = supabase.table("strength_logs").insert({
        "user_id": user_id,
        "exercise": exercise,
        "weight_kg": weight_kg,
        "reps": reps,
        "sets": sets,
        "one_rm": one_rm,
    }).execute()
    return res.data[0] if res.data else {"one_rm": one_rm}


@_sb_retry
def get_latest_strength(user_id: str, exercise: str) -> Optional[dict]:
    res = (
        supabase.table("strength_logs")
        .select("*").eq("user_id", user_id).eq("exercise", exercise)
        .order("recorded_at", desc=True).limit(1).execute()
    )
    return res.data[0] if res.data else None


@_sb_retry
def get_all_lifts_overview(user_id: str, limit: int = 12) -> dict:
    """取使用者紀錄過的所有動作，每個動作取最新一筆。

    最近紀錄的動作排在前面，最多 limit 個。
    """
    res = (
        supabase.table("strength_logs")
        .select("*").eq("user_id", user_id)
        .order("recorded_at", desc=True)
        .execute()
    )
    out = {}
    for row in (res.data or []):
        ex = row["exercise"]
        if ex not in out:
            out[ex] = row
        if len(out) >= limit:
            break
    return out


@_sb_retry
def get_monthly_report(user_id: str, start_date: date, end_date: date) -> dict:
    """聚合期間內所有打卡紀錄。end_date inclusive。"""
    start_iso = datetime.combine(start_date, datetime.min.time()).isoformat()
    end_iso = datetime.combine(end_date + timedelta(days=1),
                               datetime.min.time()).isoformat()

    # 一次抓 habit_logs（含 water / sleep / workout / stretch / 壞習慣）
    res_h = (
        supabase.table("habit_logs")
        .select("type, amount, quality, recorded_at")
        .eq("user_id", user_id)
        .gte("recorded_at", start_iso).lt("recorded_at", end_iso)
        .execute()
    )

    # 飲水：總 ml + 達標天數
    water_total = 0.0
    water_by_day: dict = {}
    sleep_records = []
    workout_count = 0
    workout_mins = 0.0
    stretch_count = 0

    for row in (res_h.data or []):
        t = row.get("type", "")
        ts = row.get("recorded_at", "")
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = ts
        d = dt.astimezone(TPE).date()
        amt = float(row.get("amount") or 0)

        if t == "water":
            water_total += amt
            water_by_day[d] = water_by_day.get(d, 0) + amt
        elif t == "sleep":
            sleep_records.append(row.get("quality") or "normal")
        elif t == "workout":
            workout_count += 1
            workout_mins += amt
        elif t == "stretch":
            stretch_count += 1

    # 飲水達標天數
    profile = get_profile(user_id)
    target = (profile or {}).get("daily_water_ml") or 2000
    water_days_met = sum(1 for s in water_by_day.values() if s >= target)

    # 睡眠：分組
    sleep_good = sum(1 for q in sleep_records if q == "good")
    sleep_normal = sum(1 for q in sleep_records if q == "normal")
    sleep_bad = sum(1 for q in sleep_records if q == "bad")

    # 力量紀錄
    res_s = (
        supabase.table("strength_logs")
        .select("exercise", count="exact")
        .eq("user_id", user_id)
        .gte("recorded_at", start_iso).lt("recorded_at", end_iso)
        .execute()
    )
    strength_count = res_s.count or 0
    strength_lifts = set(r["exercise"] for r in (res_s.data or []))

    # 反思
    res_r = (
        supabase.table("reflections")
        .select("period")
        .eq("user_id", user_id)
        .gte("recorded_at", start_iso).lt("recorded_at", end_iso)
        .execute()
    )
    reflections_by_period: dict = {}
    for r in (res_r.data or []):
        p = r.get("period") or ""
        reflections_by_period[p] = reflections_by_period.get(p, 0) + 1
    reflections_count = sum(reflections_by_period.values())

    # 目標
    goal = get_active_goal(user_id)

    return {
        "water_total_ml": int(water_total),
        "water_days_met": water_days_met,
        "sleep_total": len(sleep_records),
        "sleep_good": sleep_good,
        "sleep_normal": sleep_normal,
        "sleep_bad": sleep_bad,
        "workout_count": workout_count,
        "workout_mins": int(workout_mins),
        "stretch_count": stretch_count,
        "strength_count": strength_count,
        "strength_lift_set": strength_lifts,
        "reflections_count": reflections_count,
        "reflections_by_period": reflections_by_period,
        "active_goal": goal,
    }


@_sb_retry
def mark_coach_agreed(user_id: str) -> None:
    """記錄使用者同意免責聲明的時間。"""
    supabase.table("profiles").update({
        "ai_coach_agreed_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("user_id", user_id).execute()


def has_agreed_coach(user_id: str) -> bool:
    profile = get_profile(user_id)
    if not profile:
        return False
    return profile.get("ai_coach_agreed_at") is not None


@_sb_retry
def get_cardio_overview(user_id: str) -> dict:
    """聚合有氧（type=workout）紀錄：本週/月累計、連續打卡、最近 5 筆。"""
    now_tpe = datetime.now(TPE)
    today_tpe = now_tpe.date()
    week_start = today_tpe - timedelta(days=today_tpe.weekday())  # 週一
    month_start = today_tpe.replace(day=1)
    streak_cutoff = today_tpe - timedelta(days=60)  # 多抓 60 天供 streak 計算

    cutoff_iso = datetime.combine(streak_cutoff, datetime.min.time()).isoformat()
    res = (
        supabase.table("habit_logs")
        .select("amount, recorded_at")
        .eq("user_id", user_id).eq("type", "workout")
        .gte("recorded_at", cutoff_iso)
        .order("recorded_at", desc=True)
        .execute()
    )
    rows = res.data or []

    week_total = 0.0
    week_count = 0
    month_total = 0.0
    month_count = 0
    dates_set: set = set()

    for r in rows:
        ts = r.get("recorded_at")
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = ts
        d = dt.astimezone(TPE).date()
        dates_set.add(d)
        mins = float(r.get("amount") or 0)
        if d >= week_start:
            week_total += mins
            week_count += 1
        if d >= month_start:
            month_total += mins
            month_count += 1

    # 連續打卡（streak）：從今天或昨天往回算
    streak = 0
    cursor = today_tpe
    if cursor not in dates_set:
        cursor -= timedelta(days=1)
    while cursor in dates_set:
        streak += 1
        cursor -= timedelta(days=1)

    return {
        "week_total": int(week_total),
        "week_count": week_count,
        "month_total": int(month_total),
        "month_count": month_count,
        "streak": streak,
        "recent": rows[:5],
    }


@_sb_retry
def get_user_custom_workouts(user_id: str) -> list[dict]:
    res = (
        supabase.table("custom_workouts").select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


@_sb_retry
def get_custom_workout(workout_id: int, user_id: str) -> Optional[dict]:
    res = (
        supabase.table("custom_workouts").select("*")
        .eq("id", workout_id).eq("user_id", user_id)
        .execute()
    )
    return res.data[0] if res.data else None


@_sb_retry
def insert_custom_workout(user_id: str, name: str, items: list[dict]) -> dict:
    res = supabase.table("custom_workouts").insert({
        "user_id": user_id, "name": name, "items": items,
    }).execute()
    return res.data[0] if res.data else {}


@_sb_retry
def delete_custom_workout(workout_id: int, user_id: str) -> int:
    res = supabase.table("custom_workouts").delete()\
        .eq("id", workout_id).eq("user_id", user_id).execute()
    return len(res.data or [])


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


def calc_one_rm(weight_kg: float, reps: int) -> float:
    """Brzycki 公式估算 1RM。reps 6-10 最準，>12 失準。"""
    reps = max(1, min(reps, 12))
    return weight_kg * (36 / (37 - reps))


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


COACH_SYSTEM_PROMPT = (
    "你是一位資深健身教練，名叫『教練 Bot』。你的專長：\n"
    "- 動作姿勢與技巧調整（深蹲、硬舉、臥推等所有訓練動作）\n"
    "- 訓練計畫安排（增肌、減脂、力量、心肺）\n"
    "- 飲食原則與營養討論（熱量、蛋白質、增肌減脂飲食）\n"
    "- 增肌減脂策略、撞牆期、訓練周期\n\n"
    "風格：\n"
    "- 用繁體中文（台灣用語）回答\n"
    "- 回答精簡，3-5 句話內，重點清楚\n"
    "- 友善鼓勵但專業，不要過度奉承\n"
    "- 不知道就誠實說不知道\n"
    "- 偶爾用 1-2 個 emoji 點綴即可，不要過度\n\n"
    "嚴格安全規則：\n"
    "- 涉及『受傷 / 疼痛 / 疾病 / 藥物 / 暈眩 / 開刀』時，"
    "強烈建議使用者諮詢『醫師、物理治療師或運動傷害防護員』，並重述這點。\n"
    "- 不開立任何醫療診斷或處方\n"
    "- 不推薦特定品牌的保健食品或藥物\n\n"
    "話題邊界：\n"
    "- 不討論政治、宗教、感情、八卦、職場、學業等與健身無關的話題\n"
    "- 使用者問非健身相關問題時，禮貌拒絕並引導回健身：「我是健身教練 Bot，"
    "這個問題不在我的專業範圍內。要不要聊聊你今天的訓練？」\n\n"
    "請用此身份回答學員的問題。"
)


def claude_coach_chat(history: list, user_message: str) -> str:
    """多輪對話：把歷史 + 新訊息丟給 Gemini，回字串答覆。

    history: [{"role": "user"|"model", "text": "..."}, ...]
    """
    try:
        from google.genai.types import GenerateContentConfig
        contents = []
        for turn in history:
            contents.append({
                "role": turn["role"],
                "parts": [{"text": turn["text"]}],
            })
        contents.append({"role": "user", "parts": [{"text": user_message}]})

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=GenerateContentConfig(
                system_instruction=COACH_SYSTEM_PROMPT,
            ),
        )
        text = (response.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Coach chat error: %s", exc)
        return "（教練暫時無法回應，請稍後再試）"

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
            _menu_card("🌱", "自我成長", "反思 / 壞習慣 / 知識 / 今日進度", COLOR_HABIT, "進入", "自我成長"),
            _menu_card("🥗", "飲食與健康", "TDEE / 菜單 / 運動點心", COLOR_DIET, "進入", "飲食與健康"),
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
# 5.5 體態 / 增肌專區資料 + Flex 卡
# ============================================================

# --- 動作百科 ---
EXERCISES: dict[str, dict] = {
    # === 背 ===
    "bent_over_row_db": {
        "title": "屈體划船", "equipment": "啞鈴", "muscle": "back",
        "primary": ["闊背肌", "斜方肌中下"],
        "steps": [
            "雙手各持啞鈴，雙腳與肩同寬",
            "屈髖向前，背部保持中立，胸口微微向前",
            "手肘沿身體兩側往上拉，肩胛骨向後夾",
            "頂峰停 1 秒，慢慢放下回起始位置",
        ],
        "mistakes": ["拱背或圓背（受傷高風險）", "用慣性甩動而非肌肉控制",
                     "手肘外開太多（變成練後三角）"],
        "tips": ["想像「夾筆」夾肩胛骨", "下放時保持張力", "重量適中即可，動作正確比重量重要"],
    },
    "kneeling_pulldown_band": {
        "title": "跪姿下拉", "equipment": "彈力帶", "muscle": "back",
        "primary": ["闊背肌"],
        "steps": [
            "把彈力帶固定在門框上方",
            "跪姿，雙手寬握彈力帶兩端",
            "肩胛下沉、夾向後下方，把帶子拉到胸口",
            "慢慢回放，全程保持核心緊繃",
        ],
        "mistakes": ["用手臂發力（要靠背）", "聳肩", "身體前後晃動"],
        "tips": ["先想著「肩胛下沉」再拉", "拉到胸口而非腹部"],
    },
    "deadlift_db": {
        "title": "硬舉", "equipment": "啞鈴", "muscle": "back",
        "primary": ["豎脊肌", "臀大肌", "膕繩肌"],
        "steps": [
            "啞鈴放在腳前，雙腳與肩同寬",
            "屈髖屈膝下蹲抓握，背部打直",
            "腳推地、髖前推站起，鎖定臀部",
            "反向控制重量，緩慢放回地面",
        ],
        "mistakes": ["拱背（極易閃到腰）", "膝蓋過度前突", "啞鈴離身體太遠"],
        "tips": ["啞鈴貼著腿走", "「推地板」而非「拉起重量」", "先學徒手髖鉸鏈再加重"],
    },
    "one_arm_row_db": {
        "title": "單臂划船", "equipment": "啞鈴", "muscle": "back",
        "primary": ["闊背肌", "斜方肌"],
        "steps": [
            "單膝跪椅，同側手撐椅面",
            "另一手持啞鈴，手臂自然下垂",
            "手肘沿身體往後上方拉，肩胛收緊",
            "頂峰停 1 秒，控制下放",
        ],
        "mistakes": ["軀幹旋轉借力", "手肘外開", "肩膀聳起"],
        "tips": ["啞鈴貼著大腿往後拉", "想像「鋸樹」的動作軌跡"],
    },
    "reverse_fly_db": {
        "title": "反向飛鳥", "equipment": "啞鈴", "muscle": "back",
        "primary": ["後三角肌", "斜方肌中下"],
        "steps": [
            "雙手持輕啞鈴，屈髖向前",
            "手臂微彎，向兩側畫弧線抬起",
            "頂峰肩胛夾緊，停 1-2 秒",
            "緩慢回放，控制離心",
        ],
        "mistakes": ["重量太重變甩動", "手臂打太直", "背部沒打直"],
        "tips": ["重量寧輕勿重", "想像「擁抱大樹」反向動作"],
    },

    # === 胸 ===
    "bench_press_bb": {
        "title": "槓鈴臥推", "equipment": "槓鈴", "muscle": "chest",
        "primary": ["胸大肌", "三角肌前束", "三頭肌"],
        "steps": [
            "仰躺長凳，雙腳穩踩地面",
            "肩胛下沉後收，雙手約比肩稍寬握槓",
            "槓鈴下放至胸口下緣（約乳頭線）",
            "推回起始位置，注意手肘不打死鎖",
        ],
        "mistakes": ["手腕折太多", "屁股離凳", "槓鈴下放位置太高（傷肩）"],
        "tips": ["「肩胛先收再推」", "新手務必有保護者或史密斯架"],
    },
    "incline_press_db": {
        "title": "啞鈴上斜推", "equipment": "啞鈴", "muscle": "chest",
        "primary": ["胸大肌上束", "三角肌前束"],
        "steps": [
            "椅背調 30-45 度",
            "雙手持啞鈴於胸口兩側",
            "推到頂端時雙手稍向內靠（但不碰）",
            "控制下放到胸口側邊",
        ],
        "mistakes": ["椅背角度過陡（變成練肩）", "兩個啞鈴互相碰撞", "下放太低肩膀痛"],
        "tips": ["角度愈大、愈練上胸但肩膀壓力愈大", "30 度是新手甜蜜點"],
    },
    "fly_db": {
        "title": "啞鈴飛鳥", "equipment": "啞鈴", "muscle": "chest",
        "primary": ["胸大肌"],
        "steps": [
            "仰躺，雙手持啞鈴於胸上方，掌心相對",
            "手肘微彎、向兩側畫弧下放",
            "感受胸肌伸展，到大臂與地面平行",
            "用胸肌「夾」回頂端",
        ],
        "mistakes": ["手肘打太直（變成練肩）", "重量太重", "下放太深拉到肩膀"],
        "tips": ["想像抱大樹的動作", "重量寧輕勿重"],
    },
    "pushup_bw": {
        "title": "伏地挺身", "equipment": "徒手", "muscle": "chest",
        "primary": ["胸大肌", "三頭肌", "核心"],
        "steps": [
            "雙手撐地，比肩稍寬",
            "身體成一直線（從頭到腳跟）",
            "緩慢下放至胸口離地約一個拳頭",
            "推回起始位置，全程保持核心緊繃",
        ],
        "mistakes": ["腰下塌", "屁股翹高", "下放幅度不夠"],
        "tips": ["不行就跪姿做", "下放比推回慢 1 倍效果加倍"],
    },
    "decline_pushup_bw": {
        "title": "下斜伏地挺身", "equipment": "徒手", "muscle": "chest",
        "primary": ["胸大肌上束", "三角肌前束"],
        "steps": [
            "雙腳放在凳子或椅子上",
            "雙手撐地，身體呈一直線",
            "緩慢下放，胸口貼近地面",
            "推回起始位置",
        ],
        "mistakes": ["重心過度前傾傷手腕", "腰塌下"],
        "tips": ["腳放愈高、上胸刺激愈強", "新手先掌握平地版本"],
    },

    # === 肩 ===
    "ohp_db": {
        "title": "啞鈴肩推", "equipment": "啞鈴", "muscle": "shoulders",
        "primary": ["三角肌前束/中束", "三頭肌"],
        "steps": [
            "坐姿或站姿，雙手持啞鈴於肩膀兩側",
            "掌心朝前，手肘略低於肩膀",
            "推至頭頂上方，但不打死鎖",
            "控制下放回起始位置",
        ],
        "mistakes": ["腰背過度後仰", "推到手肘鎖死", "重量太重變借力"],
        "tips": ["核心鎖住保護腰", "感覺像「推天花板」"],
    },
    "lateral_raise_db": {
        "title": "側平舉", "equipment": "啞鈴", "muscle": "shoulders",
        "primary": ["三角肌中束"],
        "steps": [
            "站姿，雙手持輕啞鈴於身側",
            "手肘略彎，向兩側畫弧抬起",
            "抬到手臂與地面平行（不超過肩高）",
            "慢慢放下回起始位置",
        ],
        "mistakes": ["重量太重用甩的", "聳肩", "手肘打太直"],
        "tips": ["小拇指略高於大拇指 = 三角肌中束發力", "肩膀痛就是太重了"],
    },
    "front_raise_db": {
        "title": "前平舉", "equipment": "啞鈴", "muscle": "shoulders",
        "primary": ["三角肌前束"],
        "steps": [
            "站姿，雙手持啞鈴於大腿前方",
            "手臂打直或微彎，向前抬起",
            "抬到肩膀高度即可，不要過頭",
            "慢慢放下回起始位置",
        ],
        "mistakes": ["腰部後仰", "抬太高（變斜方肌）"],
        "tips": ["輕重量、慢動作效果最好"],
    },
    "rear_delt_fly_db": {
        "title": "反向飛鳥", "equipment": "啞鈴", "muscle": "shoulders",
        "primary": ["後三角肌"],
        "steps": [
            "屈髖向前，背部打直",
            "雙手持輕啞鈴於下方",
            "向兩側畫弧抬起，肩胛微收",
            "頂峰停 1 秒，控制下放",
        ],
        "mistakes": ["重量太重變甩動", "背駝"],
        "tips": ["後三角肌是新手最常忽略的部位", "輕重量就有感"],
    },
    "upright_row_db": {
        "title": "直立划船", "equipment": "啞鈴", "muscle": "shoulders",
        "primary": ["三角肌", "斜方肌"],
        "steps": [
            "站姿，雙手持啞鈴於身體前方",
            "手肘領先向上拉到胸口高度",
            "頂峰肩胛微收",
            "緩慢下放",
        ],
        "mistakes": ["拉太高（肩膀夾擠）", "用手腕拉而非手肘"],
        "tips": ["手肘高於手腕", "肩膀痛立刻停"],
    },

    # === 腿 ===
    "squat_bb": {
        "title": "槓鈴深蹲", "equipment": "槓鈴", "muscle": "legs",
        "primary": ["股四頭肌", "臀大肌", "膕繩肌", "核心"],
        "steps": [
            "槓鈴放於上背（不是脖子）",
            "雙腳與肩同寬、腳尖略外八",
            "屈髖屈膝下蹲，至大腿與地面平行",
            "腳推地站起，膝蓋對齊腳尖",
        ],
        "mistakes": ["膝蓋內夾", "重心前傾（變早安式）", "下蹲深度不夠"],
        "tips": ["每組前先做暖身組", "新手務必用深蹲架", "想著「坐椅子」"],
    },
    "rdl_db": {
        "title": "羅馬尼亞硬舉", "equipment": "啞鈴", "muscle": "legs",
        "primary": ["膕繩肌", "臀大肌"],
        "steps": [
            "站姿，雙手持啞鈴於大腿前方",
            "膝蓋微彎、屈髖向前",
            "啞鈴貼著腿往下滑，感覺膕繩肌伸展",
            "髖前推站起，鎖定臀部",
        ],
        "mistakes": ["拱背（最危險錯誤）", "膝蓋過度彎曲（變成深蹲）"],
        "tips": ["核心鎖住、背部保持中立", "練「屈髖」不是「彎腰」"],
    },
    "lunge_db": {
        "title": "弓箭步", "equipment": "啞鈴", "muscle": "legs",
        "primary": ["股四頭肌", "臀大肌"],
        "steps": [
            "站姿，雙手持啞鈴於身側",
            "一腳向前跨大步",
            "下蹲至前膝 90 度，後膝接近地面",
            "前腳推地回起始，換邊",
        ],
        "mistakes": ["前膝超過腳尖太多", "身體前傾", "步距太小"],
        "tips": ["前腳跟發力", "上半身保持直立"],
    },
    "bulgarian_split_db": {
        "title": "保加利亞分腿蹲", "equipment": "啞鈴", "muscle": "legs",
        "primary": ["股四頭肌", "臀大肌"],
        "steps": [
            "後腳放椅面，前腳向前一大步",
            "雙手持啞鈴於身側",
            "下蹲至前膝 90 度",
            "前腳推地站起",
        ],
        "mistakes": ["前腳離椅子太近（膝蓋壓力大）", "重心後傾"],
        "tips": ["前腳跨遠一點", "新手不加重量先抓平衡"],
    },
    "calf_raise_bw": {
        "title": "提踵", "equipment": "徒手", "muscle": "legs",
        "primary": ["小腿肌"],
        "steps": [
            "站在台階邊緣，腳跟懸空",
            "緩慢踮起腳尖到最高",
            "頂峰停 1-2 秒",
            "緩慢下放至腳跟低於台階",
        ],
        "mistakes": ["速度太快", "上下幅度不夠"],
        "tips": ["全程感受小腿肌伸展", "可以加負重變化"],
    },

    # === 手臂 ===
    "bicep_curl_db": {
        "title": "二頭肌彎舉", "equipment": "啞鈴", "muscle": "arms",
        "primary": ["二頭肌"],
        "steps": [
            "站姿，雙手持啞鈴於身側",
            "手肘貼身體，前臂往上彎",
            "頂峰停 1 秒、二頭擠壓",
            "緩慢下放至手臂完全伸直",
        ],
        "mistakes": ["手肘前後晃動", "用腰部借力", "下放沒到底"],
        "tips": ["想著「手肘是支點」", "下放比上舉慢一倍"],
    },
    "tricep_kickback_db": {
        "title": "三頭肌後屈伸", "equipment": "啞鈴", "muscle": "arms",
        "primary": ["三頭肌"],
        "steps": [
            "屈髖向前，手肘抬高貼身體",
            "前臂往後伸展至手臂打直",
            "頂峰停 1 秒",
            "緩慢回到起始位置",
        ],
        "mistakes": ["手肘下垂", "上臂晃動"],
        "tips": ["上臂保持不動，只動前臂", "輕重量、慢動作"],
    },
    "hammer_curl_db": {
        "title": "錘式彎舉", "equipment": "啞鈴", "muscle": "arms",
        "primary": ["肱橈肌", "二頭肌"],
        "steps": [
            "站姿，雙手持啞鈴，掌心相對",
            "保持掌心向內不旋轉",
            "上彎至胸口高度",
            "緩慢下放",
        ],
        "mistakes": ["途中手腕旋轉（變一般彎舉）", "用腰部借力"],
        "tips": ["練前臂粗大就靠這個", "可單手交替做"],
    },
    "tricep_pushdown_band": {
        "title": "三頭下壓", "equipment": "彈力帶", "muscle": "arms",
        "primary": ["三頭肌"],
        "steps": [
            "彈力帶固定在頭頂上方",
            "雙手抓帶兩端，手肘貼身體",
            "前臂往下壓直至手臂打直",
            "緩慢回起始位置",
        ],
        "mistakes": ["手肘前後移動", "用身體重心壓"],
        "tips": ["手肘像「鉸鏈」固定不動"],
    },
    "concentration_curl_db": {
        "title": "集中彎舉", "equipment": "啞鈴", "muscle": "arms",
        "primary": ["二頭肌"],
        "steps": [
            "坐姿，手肘抵在同側大腿內側",
            "另一手持啞鈴，前臂下垂",
            "緩慢上彎至頂峰",
            "頂峰停 1 秒、緩慢下放",
        ],
        "mistakes": ["用身體晃動", "手肘離開大腿"],
        "tips": ["這個動作沒得借力，最能孤立二頭"],
    },
}

# --- 訓練菜單 ---
WORKOUT_MENUS: dict[str, dict] = {
    "back": {
        "title": "背部鍛鍊", "img": "back", "color": "#3B82F6", "icon": "🔵",
        "items": [
            ("bent_over_row_db",      4,  8),
            ("kneeling_pulldown_band", 4, 8),
            ("deadlift_db",           4,  8),
            ("one_arm_row_db",        3, 10),
            ("reverse_fly_db",        3, 12),
        ],
    },
    "chest": {
        "title": "胸肌鍛鍊", "img": "chest", "color": "#3B82F6", "icon": "🔵",
        "items": [
            ("bench_press_bb",   4,  8),
            ("incline_press_db", 4,  8),
            ("fly_db",           3, 12),
            ("pushup_bw",        3, 15),
            ("decline_pushup_bw", 3, 10),
        ],
    },
    "shoulders": {
        "title": "肩膀鍛鍊", "img": "shoulders", "color": "#3B82F6", "icon": "🔵",
        "items": [
            ("ohp_db",            4,  8),
            ("lateral_raise_db",  4, 12),
            ("front_raise_db",    3, 12),
            ("rear_delt_fly_db",  3, 12),
            ("upright_row_db",    3, 10),
        ],
    },
    "legs": {
        "title": "腿部訓練", "img": "legs", "color": "#3B82F6", "icon": "🔵",
        "items": [
            ("squat_bb",            4,  8),
            ("rdl_db",              4,  8),
            ("lunge_db",            3, 10),
            ("bulgarian_split_db",  3, 10),
            ("calf_raise_bw",       3, 20),
        ],
    },
    "arms": {
        "title": "手臂塑形", "img": "arms", "color": "#3B82F6", "icon": "🔵",
        "items": [
            ("bicep_curl_db",         4, 10),
            ("tricep_kickback_db",    4, 10),
            ("hammer_curl_db",        3, 12),
            ("tricep_pushdown_band",  3, 12),
            ("concentration_curl_db", 3, 12),
        ],
    },
}


def _muscle_img_url(img_key: str) -> str:
    return f"https://{SPACE_HOST}/muscles/{img_key}.png"


def training_menu_flex() -> FlexMessage:
    """5 套訓練菜單 Carousel + 最後一張自訂菜單入口。"""
    contents = [_training_card(k, v) for k, v in WORKOUT_MENUS.items()]
    contents.append(_custom_workout_intro_card())
    return _flex("訓練菜單庫", {
        "type": "carousel",
        "contents": contents,
    })


def _training_card(menu_key: str, menu: dict) -> dict:
    items = menu["items"]
    item_rows = []
    for ex_id, sets, reps in items:
        ex = EXERCISES[ex_id]
        item_rows.append({
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "text",
                 "text": f"{ex['title']}・{ex['equipment']}",
                 "size": "sm", "flex": 7, "wrap": True, "color": C_TEXT_DARK},
                {"type": "text", "text": f"{sets}×{reps}",
                 "size": "sm", "flex": 2, "align": "end",
                 "weight": "bold", "color": menu["color"]},
            ],
        })

    return {
        "type": "bubble",
        "size": "kilo",
        "hero": {
            "type": "image",
            "url": _muscle_img_url(menu["img"]),
            "size": "full",
            "aspectRatio": "4:3",
            "aspectMode": "cover",
            "backgroundColor": "#F5F5F5",
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": menu["title"],
                 "weight": "bold", "size": "xl", "color": C_TEXT_DARK},
                {"type": "text", "text": f"{len(items)} 個動作",
                 "size": "xs", "color": C_TEXT_SOFT, "margin": "xs"},
                {"type": "separator", "color": C_DIVIDER, "margin": "md"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md",
                 "contents": item_rows},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": C_PRIMARY, "height": "sm",
                 "action": {"type": "message", "label": "✏️ 力量紀錄",
                            "text": "力量紀錄"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "postback", "label": "📖 動作詳解",
                            "data": f"action=menu_detail&key={menu_key}",
                            "displayText": f"看 {menu['title']} 動作詳解"}},
            ],
        },
    }


def exercise_detail_flex(ex_id: str) -> FlexMessage:
    ex = EXERCISES[ex_id]
    color = "#3B82F6"

    primary_text = "・".join(ex.get("primary", []))

    def _block(title: str, items: list, emoji: str) -> list:
        rows = [{
            "type": "text", "text": f"{emoji} {title}",
            "weight": "bold", "size": "sm", "color": color, "margin": "md",
        }]
        for i, it in enumerate(items, 1):
            rows.append({
                "type": "text", "text": f"{i}. {it}",
                "wrap": True, "size": "xs", "color": C_TEXT_DARK,
                "margin": "xs",
            })
        return rows

    body_contents = [
        {"type": "box", "layout": "horizontal",
         "contents": [
             {"type": "text", "text": "🎯 目標肌群",
              "size": "xs", "flex": 2, "color": C_TEXT_SOFT, "weight": "bold"},
             {"type": "text", "text": primary_text,
              "size": "xs", "flex": 4, "color": C_TEXT_DARK,
              "align": "end", "wrap": True},
         ]},
        {"type": "box", "layout": "horizontal", "margin": "sm",
         "contents": [
             {"type": "text", "text": "🛠️ 器材",
              "size": "xs", "flex": 2, "color": C_TEXT_SOFT, "weight": "bold"},
             {"type": "text", "text": ex["equipment"],
              "size": "xs", "flex": 4, "color": C_TEXT_DARK, "align": "end"},
         ]},
        {"type": "separator", "color": C_DIVIDER, "margin": "md"},
        *_block("動作步驟", ex["steps"], "📋"),
        *_block("常見錯誤", ex["mistakes"], "⚠️"),
        *_block("小提示", ex["tips"], "💡"),
    ]

    return _flex(ex["title"], {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": color,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": f"📖 {ex['title']}",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": f"{ex['equipment']} · {EXERCISES[ex_id].get('muscle','')}",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "16px",
            "contents": body_contents,
        },
    })


# --- 有氧動作百科 + 燃脂菜單 ---

CARDIO_EXERCISES: dict[str, dict] = {
    "jumping_jack": {
        "title": "開合跳", "equipment": "徒手", "category": "HIIT/全身",
        "primary": ["心肺", "全身"],
        "steps": [
            "雙腳併攏站直，雙手放身側",
            "起跳時雙腳同時往兩側開，雙手向上拍手",
            "落地時回到起始位置，腳併攏、手放下",
            "保持節奏連續做，落地前掌先著地緩衝",
        ],
        "mistakes": ["落地用整個腳掌（傷膝）", "雙手沒舉到頭頂", "節奏太快變失控"],
        "tips": ["全程膝蓋微彎", "腰腹用力穩定軀幹", "嘴巴呼吸、別憋氣"],
    },
    "high_knees": {
        "title": "高抬腿", "equipment": "徒手", "category": "HIIT/下肢",
        "primary": ["核心", "髖屈肌", "心肺"],
        "steps": [
            "雙腳與肩同寬站立，雙手放胸前或自然擺臂",
            "輪流抬起膝蓋至腰部高度",
            "前腳掌落地、保持彈性",
            "節奏快但維持膝蓋高度",
        ],
        "mistakes": ["腰駝著抬", "膝蓋抬不夠高", "用腳跟落地"],
        "tips": ["核心緊繃保護腰", "想著「膝蓋打手」"],
    },
    "lunge_jump": {
        "title": "弓箭步跳", "equipment": "徒手", "category": "HIIT/下肢",
        "primary": ["股四頭肌", "臀大肌", "心肺"],
        "steps": [
            "弓箭步預備，前膝 90 度、後膝接近地面",
            "用力跳起，空中換腳",
            "輕落地、緩衝至弓箭步",
            "繼續換腳跳",
        ],
        "mistakes": ["前膝超過腳尖", "落地太重", "上半身前傾"],
        "tips": ["核心鎖住", "腳跟向下踩穩"],
    },
    "squat_jump": {
        "title": "深蹲跳", "equipment": "徒手", "category": "HIIT/下肢",
        "primary": ["股四頭肌", "臀大肌", "小腿"],
        "steps": [
            "雙腳與肩同寬，下蹲至大腿與地面平行",
            "用力跳起，雙手往上擺",
            "前腳掌落地、緩衝下蹲",
            "連續做不停",
        ],
        "mistakes": ["蹲不夠深就跳", "膝蓋內夾", "落地僵直"],
        "tips": ["落地像踩棉花", "用呼吸節奏"],
    },
    "burpee": {
        "title": "波比跳", "equipment": "徒手", "category": "HIIT/全身",
        "primary": ["全身", "心肺"],
        "steps": [
            "站姿開始，下蹲雙手撐地",
            "雙腳向後跳成棒式（可加伏地挺身）",
            "雙腳跳回手前，蹲姿",
            "起身跳躍、雙手過頭",
        ],
        "mistakes": ["棒式時腰下塌", "落地砸地板", "節奏混亂"],
        "tips": ["新手可分解（先下後上）", "求穩不求快"],
    },
    "mountain_climber": {
        "title": "登山者", "equipment": "徒手", "category": "HIIT/核心",
        "primary": ["核心", "髖屈肌", "心肺"],
        "steps": [
            "棒式預備，手肘略彎、核心收緊",
            "輪流把膝蓋拉向胸口",
            "前腳掌點地、節奏穩定",
            "保持髖部低，不要翹屁股",
        ],
        "mistakes": ["屁股翹高", "腰下塌", "膝蓋沒拉到胸口"],
        "tips": ["想像在跑步機上跑", "肩膀正對手腕上方"],
    },
    "plank": {
        "title": "平板撐", "equipment": "徒手", "category": "核心",
        "primary": ["核心", "肩膀", "臀部"],
        "steps": [
            "前臂貼地、手肘正對肩膀下方",
            "腳尖撐地，身體呈一直線",
            "夾緊臀部、收緊核心",
            "保持自然呼吸",
        ],
        "mistakes": ["腰下塌（傷腰）", "屁股翹高（沒練到核心）", "脖子前伸"],
        "tips": ["眼睛看地板、脖子放鬆", "撐不住就降為跪姿"],
    },
    "jump_rope": {
        "title": "跳繩", "equipment": "跳繩", "category": "有氧/全身",
        "primary": ["小腿", "心肺", "協調"],
        "steps": [
            "雙手肘貼身、手腕用力轉繩",
            "前腳掌起跳、輕落地",
            "保持節奏連續跳",
            "繩子打到腿就調整繩長",
        ],
        "mistakes": ["跳太高浪費體力", "雙臂大幅擺動", "腳跟落地"],
        "tips": ["跳 1-2 公分高就夠", "新手練雙腳跳熟再進階"],
    },
    "skater": {
        "title": "側向滑板跳", "equipment": "徒手", "category": "HIIT/下肢",
        "primary": ["臀大肌", "股四頭肌", "心肺"],
        "steps": [
            "單腳站立，另一腳在後方輕點地",
            "用力側向跳，落地換邊單腳站立",
            "雙手自然擺動配合節奏",
            "連續左右換邊",
        ],
        "mistakes": ["落地膝蓋內夾", "上半身前傾", "步幅太小"],
        "tips": ["想像滑冰選手的側向步伐", "落地腳跟微微彎曲"],
    },
    "plank_up": {
        "title": "棒式撐起", "equipment": "徒手", "category": "核心/上肢",
        "primary": ["核心", "三頭肌", "肩膀"],
        "steps": [
            "從前臂棒式開始",
            "依序伸直右手、左手成伏地挺身姿",
            "再依序屈右肘、左肘回前臂棒式",
            "下一輪換邊先撐",
        ],
        "mistakes": ["臀部左右扭動", "腰塌", "速度太快"],
        "tips": ["核心鎖住、髖部穩定", "每邊輪流先撐避免肌肉不對稱"],
    },
    "bicycle_crunch": {
        "title": "自行車仰臥起坐", "equipment": "徒手", "category": "核心",
        "primary": ["腹直肌", "腹斜肌"],
        "steps": [
            "仰躺，雙手輕扶頭後、雙腳離地",
            "右肘觸左膝、同時右腳伸直",
            "換邊：左肘觸右膝、左腳伸直",
            "節奏穩定、像踩腳踏車",
        ],
        "mistakes": ["拉脖子（傷頸椎）", "腰離地", "節奏太快變晃"],
        "tips": ["手只是輕扶不出力", "感覺腹斜肌在收縮"],
    },
    "flutter_kick": {
        "title": "蹬腳", "equipment": "徒手", "category": "核心",
        "primary": ["下腹部", "髖屈肌"],
        "steps": [
            "仰躺，雙手放臀部兩側",
            "雙腳離地 15-20 公分",
            "兩腳上下交替小幅度擺動",
            "下背保持貼地",
        ],
        "mistakes": ["下背離地（會傷腰）", "幅度太大", "頭抬離地"],
        "tips": ["下背壓住地板再開始", "腿放愈低、難度愈高"],
    },
    "brisk_walk": {
        "title": "健走", "equipment": "徒手", "category": "有氧/穩定",
        "primary": ["下肢", "心肺"],
        "steps": [
            "抬頭挺胸、肩膀放鬆",
            "步幅大一點、自然擺臂",
            "保持心率 100-130（能聊天但不能唱歌）",
            "持續 30-60 分鐘",
        ],
        "mistakes": ["低頭看手機", "步幅太小變散步"],
        "tips": ["上坡或加重背包提升強度", "搭配 podcast 增加持續性"],
    },
    "jogging": {
        "title": "慢跑", "equipment": "徒手", "category": "有氧/穩定",
        "primary": ["下肢", "心肺"],
        "steps": [
            "前 5 分鐘暖身走",
            "切換慢跑，速度能說整句話",
            "目標心率 130-150 區間",
            "結束前 3-5 分鐘緩和走",
        ],
        "mistakes": ["一開始就衝太快", "跨步太大", "不暖身"],
        "tips": ["前腳掌或中足著地", "找節奏穩定的歌單配速"],
    },
    "step_up": {
        "title": "跳台階", "equipment": "台階/箱子", "category": "有氧/下肢",
        "primary": ["臀大肌", "股四頭肌", "心肺"],
        "steps": [
            "面對 30-50 公分穩固台階",
            "輪流踩上、踩下",
            "踩上時主動發力推地",
            "保持節奏不要趕",
        ],
        "mistakes": ["膝蓋內夾", "重心不穩晃動", "台階不穩"],
        "tips": ["前腳掌全踩上去", "可加啞鈴增加強度"],
    },
}

# 每筆是 (exercise_id, 顯示時長/組數)
CARDIO_MENUS: dict[str, dict] = {
    "home_20": {
        "title": "居家燃脂 20 分鐘", "emoji": "🏠",
        "subtitle": "新手友善 · 無器材",
        "color": COLOR_DIET,
        "items": [
            ("jumping_jack",   "30 秒 × 4 輪"),
            ("high_knees",     "30 秒 × 4 輪"),
            ("squat_jump",     "30 秒 × 4 輪"),
            ("mountain_climber","30 秒 × 4 輪"),
            ("plank",          "45 秒 × 3 輪"),
        ],
    },
    "hiit_25": {
        "title": "HIIT 25 分鐘", "emoji": "🔥",
        "subtitle": "高強度間歇 · 燃脂效率最高",
        "color": "#E55B25",
        "items": [
            ("burpee",         "30 秒 → 30 秒休 × 5 輪"),
            ("mountain_climber","30 秒 → 30 秒休 × 5 輪"),
            ("squat_jump",     "30 秒 → 30 秒休 × 4 輪"),
            ("lunge_jump",     "30 秒 → 30 秒休 × 4 輪"),
            ("plank_up",       "20 秒 → 20 秒休 × 4 輪"),
        ],
    },
    "jog_45": {
        "title": "戶外慢跑 45 分鐘", "emoji": "🏃",
        "subtitle": "穩定有氧 · LISS 燃脂",
        "color": "#5DADE2",
        "items": [
            ("brisk_walk",     "5 分鐘 暖身"),
            ("jogging",        "35 分鐘 維持心率 130-150"),
            ("brisk_walk",     "3 分鐘 緩和"),
        ],
    },
    "tabata_8": {
        "title": "Tabata 8 分鐘", "emoji": "⚡",
        "subtitle": "極短爆汗 · 4 分鐘核心",
        "color": "#FF6B35",
        "items": [
            ("squat_jump",     "20 秒 → 10 秒休 × 8 輪"),
            ("mountain_climber","20 秒 → 10 秒休 × 8 輪"),
            ("plank",          "30 秒 收操"),
        ],
    },
    "circuit_30": {
        "title": "全身循環 30 分鐘", "emoji": "💃",
        "subtitle": "中強度 · 4 動作 × 4 輪",
        "color": COLOR_HABIT,
        "items": [
            ("burpee",         "1 分鐘 × 4 輪"),
            ("jumping_jack",   "1 分鐘 × 4 輪"),
            ("lunge_jump",     "1 分鐘 × 4 輪"),
            ("bicycle_crunch", "1 分鐘 × 4 輪"),
        ],
    },
}

_CARDIO_MUSCLE_ZH_TO_KEY = {
    "居家": "home_20", "HIIT": "hiit_25", "慢跑": "jog_45",
    "Tabata": "tabata_8", "循環": "circuit_30",
}


def cardio_menu_flex() -> FlexMessage:
    """燃脂菜單 5 套 Carousel。"""
    return _flex("燃脂菜單", {
        "type": "carousel",
        "contents": [_cardio_card(k, v) for k, v in CARDIO_MENUS.items()],
    })


def _cardio_card(menu_key: str, menu: dict) -> dict:
    items = menu["items"]
    item_rows = []
    for ex_id, duration in items:
        ex = CARDIO_EXERCISES.get(ex_id, {})
        title = ex.get("title", ex_id)
        item_rows.append({
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "text", "text": title,
                 "size": "sm", "flex": 4, "wrap": True, "color": C_TEXT_DARK},
                {"type": "text", "text": duration,
                 "size": "xs", "flex": 5, "align": "end",
                 "weight": "bold", "color": menu["color"], "wrap": True},
            ],
        })

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": menu["color"], "paddingAll": "30px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": menu["emoji"],
                 "size": "5xl", "align": "center", "color": "#FFFFFF"},
                {"type": "text", "text": menu["title"],
                 "weight": "bold", "size": "lg", "color": "#FFFFFF",
                 "align": "center", "margin": "sm"},
                {"type": "text", "text": menu["subtitle"],
                 "size": "xs", "color": "#FFFFFF",
                 "align": "center", "margin": "xs", "wrap": True},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": f"{len(items)} 個項目",
                 "size": "xs", "color": C_TEXT_SOFT, "margin": "xs"},
                {"type": "separator", "color": C_DIVIDER, "margin": "md"},
                {"type": "box", "layout": "vertical",
                 "spacing": "sm", "margin": "md",
                 "contents": item_rows},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary",
                 "color": menu["color"], "height": "sm",
                 "action": {"type": "message", "label": "⏱️ 開始打卡",
                            "text": "有氧紀錄"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "postback", "label": "📖 動作詳解",
                            "data": f"action=cardio_menu_detail&key={menu_key}",
                            "displayText": f"看 {menu['title']} 動作詳解"}},
            ],
        },
    }


def cardio_exercise_detail_flex(ex_id: str) -> FlexMessage:
    ex = CARDIO_EXERCISES[ex_id]
    color = COLOR_DIET

    def _block(title: str, items: list, emoji: str) -> list:
        rows = [{
            "type": "text", "text": f"{emoji} {title}",
            "weight": "bold", "size": "sm", "color": color, "margin": "md",
        }]
        for i, it in enumerate(items, 1):
            rows.append({
                "type": "text", "text": f"{i}. {it}",
                "wrap": True, "size": "xs", "color": C_TEXT_DARK,
                "margin": "xs",
            })
        return rows

    body_contents = [
        {"type": "box", "layout": "horizontal",
         "contents": [
             {"type": "text", "text": "🎯 目標",
              "size": "xs", "flex": 2, "color": C_TEXT_SOFT, "weight": "bold"},
             {"type": "text", "text": "・".join(ex.get("primary", [])),
              "size": "xs", "flex": 4, "color": C_TEXT_DARK,
              "align": "end", "wrap": True},
         ]},
        {"type": "box", "layout": "horizontal", "margin": "sm",
         "contents": [
             {"type": "text", "text": "🛠️ 器材",
              "size": "xs", "flex": 2, "color": C_TEXT_SOFT, "weight": "bold"},
             {"type": "text", "text": ex["equipment"],
              "size": "xs", "flex": 4, "color": C_TEXT_DARK, "align": "end"},
         ]},
        {"type": "box", "layout": "horizontal", "margin": "sm",
         "contents": [
             {"type": "text", "text": "🏷️ 類型",
              "size": "xs", "flex": 2, "color": C_TEXT_SOFT, "weight": "bold"},
             {"type": "text", "text": ex.get("category", "—"),
              "size": "xs", "flex": 4, "color": C_TEXT_DARK, "align": "end"},
         ]},
        {"type": "separator", "color": C_DIVIDER, "margin": "md"},
        *_block("動作步驟", ex["steps"], "📋"),
        *_block("常見錯誤", ex["mistakes"], "⚠️"),
        *_block("小提示", ex["tips"], "💡"),
    ]

    return _flex(ex["title"], {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": color,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": f"📖 {ex['title']}",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": f"{ex['equipment']} · {ex.get('category', '')}",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "16px",
            "contents": body_contents,
        },
    })


def cardio_menu_detail_carousel_flex(menu_key: str) -> FlexMessage:
    """某燃脂菜單裡所有動作的詳解 Carousel。"""
    menu = CARDIO_MENUS[menu_key]
    cards = []
    seen = set()
    for ex_id, _ in menu["items"]:
        if ex_id in seen:
            continue
        seen.add(ex_id)
        card = cardio_exercise_detail_flex(ex_id)
        cards.append(json.loads(card.contents.to_json()))
    return _flex(f"{menu['title']} 動作詳解", {
        "type": "carousel", "contents": cards,
    })


# --- 自訂菜單 ---

MAX_CUSTOM_WORKOUTS_PER_USER = 5
MAX_ITEMS_PER_CUSTOM = 8
MAX_NAME_LEN = 20
MAX_EXERCISE_LEN = 30


def parse_custom_workout_items(text: str) -> list[dict]:
    """解析使用者輸入的動作清單。

    支援格式：
    - 一行一筆：「深蹲 4 8」
    - 多筆用 / | 分隔：「深蹲 4 8 / 硬舉 4 8」
    - 每筆：動作名 + 組 + 次（空白分隔，最後 2 個是數字）

    失敗回拋 ValueError 帶友善訊息。
    """
    normalized = (
        text.replace("|", "\n").replace("/", "\n").replace("｜", "\n")
    )
    lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
    items = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            raise ValueError(f"「{line}」格式不對\n要：動作名 組 次")
        try:
            reps = int(parts[-1])
            sets = int(parts[-2])
        except ValueError:
            raise ValueError(f"「{line}」最後兩個要是數字（組 次）")
        exercise = " ".join(parts[:-2]).strip()
        if not exercise:
            raise ValueError("動作名不能空")
        if len(exercise) > MAX_EXERCISE_LEN:
            raise ValueError(f"動作名「{exercise}」超過 {MAX_EXERCISE_LEN} 字")
        if sets < 1 or sets > 20:
            raise ValueError(f"組數 {sets} 不在 1-20")
        if reps < 1 or reps > 50:
            raise ValueError(f"次數 {reps} 不在 1-50")
        items.append({"exercise": exercise, "sets": sets, "reps": reps})
    return items


def custom_workouts_overview_flex(workouts: list[dict]) -> FlexMessage:
    """所有自訂菜單列表 Flex。空 state 顯示引導。"""
    if not workouts:
        body_contents = [
            {"type": "text",
             "text": "還沒建立任何自訂菜單\n打「建立自訂菜單」開始 ✨",
             "wrap": True, "size": "sm", "color": C_TEXT_SOFT,
             "align": "center"},
        ]
    else:
        rows = []
        for w in workouts:
            n_items = len(w.get("items", []) or [])
            rows.append({
                "type": "box", "layout": "horizontal", "spacing": "sm",
                "contents": [
                    {"type": "box", "layout": "vertical", "flex": 5,
                     "contents": [
                         {"type": "text", "text": f"🛠️ {w['name']}",
                          "size": "sm", "weight": "bold",
                          "color": C_TEXT_DARK, "wrap": True},
                         {"type": "text", "text": f"{n_items} 個動作",
                          "size": "xs", "color": C_TEXT_SOFT,
                          "margin": "xs"},
                     ]},
                    {"type": "button", "style": "primary",
                     "color": C_PRIMARY, "height": "sm", "flex": 2,
                     "action": {"type": "postback", "label": "查看",
                                "data": f"action=cw_view&id={w['id']}",
                                "displayText": f"看 {w['name']}"}},
                ],
            })
            rows.append({"type": "separator", "color": C_DIVIDER})
        body_contents = rows[:-1]

    bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": C_PRIMARY, "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🛠️ 我的自訂菜單",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text",
                 "text": f"{len(workouts)}/{MAX_CUSTOM_WORKOUTS_PER_USER} 套",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "paddingAll": "16px",
            "contents": body_contents,
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary",
                 "color": C_PRIMARY, "height": "sm",
                 "action": {"type": "message", "label": "+ 建立新菜單",
                            "text": "建立自訂菜單"}},
            ],
        },
    }
    return _flex("我的自訂菜單", bubble)


def custom_workout_card_flex(workout: dict) -> FlexMessage:
    """單套自訂菜單詳細卡。"""
    items = workout.get("items", []) or []
    rows = []
    for it in items:
        rows.append({
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "text",
                 "text": it.get("exercise", ""),
                 "size": "sm", "flex": 7, "wrap": True,
                 "color": C_TEXT_DARK},
                {"type": "text",
                 "text": f"{it.get('sets', 0)}×{it.get('reps', 0)}",
                 "size": "sm", "flex": 2, "align": "end",
                 "weight": "bold", "color": C_PRIMARY},
            ],
        })

    bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": C_PRIMARY, "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": f"🛠️ {workout['name']}",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": f"{len(items)} 個動作",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "paddingAll": "16px",
            "contents": rows or [
                {"type": "text", "text": "（無動作）", "size": "sm",
                 "color": C_TEXT_SOFT, "align": "center"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary",
                 "color": C_PRIMARY, "height": "sm",
                 "action": {"type": "message", "label": "✏️ 力量紀錄",
                            "text": "力量紀錄"}},
                {"type": "button", "style": "secondary", "height": "sm",
                 "action": {"type": "postback",
                            "label": "🗑️ 刪除這套",
                            "data": f"action=cw_delete&id={workout['id']}",
                            "displayText": f"刪除「{workout['name']}」"}},
            ],
        },
    }
    return _flex(workout["name"], bubble)


def cardio_overview_flex(data: dict) -> FlexMessage:
    """🏃 我的有氧 — 本週/月累計 + 連續打卡 + 最近紀錄。"""
    color = COLOR_DIET  # 桃橘色，跟減脂主題搭

    is_empty = data["week_count"] == 0 and data["month_count"] == 0

    if is_empty:
        body_contents = [{
            "type": "text",
            "text": "尚未有有氧紀錄\n\n打「有氧紀錄」開始追蹤吧 🏃",
            "wrap": True, "size": "sm", "color": C_TEXT_SOFT, "align": "center",
        }]
    else:
        streak = data["streak"]
        body_contents = [
            _stat_row("📆 本週累計",
                     f"{data['week_total']} 分 / {data['week_count']} 次", color),
            _stat_row("📅 本月累計",
                     f"{data['month_total']} 分 / {data['month_count']} 次", color),
            _stat_row("🔥 連續打卡",
                     f"{streak} 天" if streak > 0 else "—",
                     color if streak > 0 else C_TEXT_SOFT),
        ]
        if data["recent"]:
            body_contents.append({"type": "separator", "color": C_DIVIDER, "margin": "md"})
            body_contents.append({
                "type": "text", "text": "最近紀錄",
                "size": "xs", "color": C_TEXT_SOFT,
                "weight": "bold", "margin": "md",
            })
            for r in data["recent"]:
                ts = r.get("recorded_at")
                if isinstance(ts, str):
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                else:
                    dt = ts
                d = dt.astimezone(TPE).date()
                mins = int(float(r.get("amount") or 0))
                body_contents.append({
                    "type": "box", "layout": "horizontal", "spacing": "sm",
                    "contents": [
                        {"type": "text",
                         "text": d.strftime("%m/%d"), "size": "sm",
                         "color": C_TEXT_SOFT, "flex": 2},
                        {"type": "text",
                         "text": f"{mins} 分鐘", "size": "sm",
                         "color": C_TEXT_DARK, "flex": 3, "align": "end",
                         "weight": "bold"},
                    ],
                })

    return _flex("我的有氧", {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": color, "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🏃 我的有氧",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": "過去 60 天統計",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": body_contents,
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "contents": [{
                "type": "button", "style": "primary",
                "color": color, "height": "sm",
                "action": {"type": "message",
                           "label": "⏱️ 新增有氧紀錄", "text": "有氧紀錄"},
            }],
        },
    })


def _stat_row(label: str, value: str, color: str) -> dict:
    return {
        "type": "box", "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "sm",
             "weight": "bold", "color": C_TEXT_DARK, "flex": 3},
            {"type": "text", "text": value, "size": "md",
             "weight": "bold", "color": color, "flex": 4, "align": "end"},
        ],
    }


def _custom_workout_intro_card() -> dict:
    """訓練菜單 Carousel 最後一張：自訂菜單入口（bubble dict 直接給 carousel 用）。"""
    return {
        "type": "bubble", "size": "kilo",
        "hero": {
            "type": "box", "layout": "vertical",
            "backgroundColor": C_PRIMARY,
            "paddingAll": "40px", "spacing": "md",
            "contents": [
                {"type": "text", "text": "🛠️", "size": "5xl",
                 "align": "center", "color": "#FFFFFF"},
                {"type": "text", "text": "自訂菜單",
                 "weight": "bold", "size": "xl", "color": "#FFFFFF",
                 "align": "center", "margin": "md"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "paddingAll": "16px",
            "contents": [
                {"type": "text",
                 "text": "用你自己的菜單訓練",
                 "size": "sm", "weight": "bold", "color": C_TEXT_DARK,
                 "align": "center"},
                {"type": "text",
                 "text": "現有的訓練菜單不夠用？\n建立自己的，最多 5 套。",
                 "wrap": True, "size": "xs", "color": C_TEXT_SOFT,
                 "align": "center", "margin": "sm"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary",
                 "color": C_PRIMARY, "height": "sm",
                 "action": {"type": "message", "label": "🛠️ 我的自訂菜單",
                            "text": "自訂菜單"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "message", "label": "+ 建立新菜單",
                            "text": "建立自訂菜單"}},
            ],
        },
    }


def menu_detail_carousel_flex(menu_key: str) -> FlexMessage:
    """某個訓練菜單裡所有動作的詳解 Carousel。"""
    menu = WORKOUT_MENUS[menu_key]
    cards = []
    for ex_id, _, _ in menu["items"]:
        card_msg = exercise_detail_flex(ex_id)
        # 取出 bubble dict
        cards.append(json.loads(card_msg.contents.to_json()))
    return _flex(f"{menu['title']} 動作詳解", {
        "type": "carousel", "contents": cards,
    })


# --- 力量追蹤 Flex ---

def _exercise_icon(name: str) -> str:
    """根據動作名稱推測 emoji 圖示，常見 keyword 對應。"""
    n = name.lower()
    if any(k in n for k in ("深蹲", "squat")):
        return "🏋️"
    if any(k in n for k in ("臥推", "bench")):
        return "💪"
    if any(k in n for k in ("硬舉", "deadlift")):
        return "🦴"
    if any(k in n for k in ("肩推", "ohp", "shoulder")):
        return "🙌"
    if any(k in n for k in ("二頭", "彎舉", "curl", "bicep")):
        return "💪"
    if any(k in n for k in ("三頭", "tricep")):
        return "💪"
    if any(k in n for k in ("划船", "row")):
        return "🚣"
    if any(k in n for k in ("飛鳥", "fly")):
        return "🦅"
    if any(k in n for k in ("下拉", "pulldown", "pull")):
        return "🪢"
    return "💪"


def strength_overview_flex(records: dict) -> FlexMessage:
    """我的力量總覽：動態列出使用者紀錄過的所有動作（最近排前面）。"""
    if not records:
        body_contents = [{
            "type": "text",
            "text": "尚未紀錄任何動作\n\n打「力量紀錄」開始追蹤你的訓練 💪",
            "wrap": True, "size": "sm", "color": C_TEXT_SOFT,
            "align": "center",
        }]
    else:
        rows = []
        for ex_name, r in records.items():
            icon = _exercise_icon(ex_name)
            val = f"{r['one_rm']:.0f} kg"
            sets_s = r.get("sets") or 1
            sub = f"最近：{int(r['weight_kg'])}kg × {r['reps']} × {sets_s}組"
            rows.append({
                "type": "box", "layout": "vertical", "spacing": "xs",
                "contents": [
                    {"type": "box", "layout": "horizontal",
                     "contents": [
                         {"type": "text", "text": f"{icon} {ex_name}",
                          "size": "sm", "weight": "bold",
                          "color": C_TEXT_DARK, "flex": 5, "wrap": True},
                         {"type": "text", "text": val, "size": "lg",
                          "weight": "bold", "color": C_PRIMARY,
                          "flex": 3, "align": "end"},
                     ]},
                    {"type": "text", "text": sub, "size": "xs",
                     "color": C_TEXT_SOFT, "margin": "xs"},
                ],
            })
            rows.append({"type": "separator", "color": C_DIVIDER})
        body_contents = rows[:-1]

    return _flex("我的力量", {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": C_PRIMARY,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "💪 我的力量",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": "估算 1RM（Brzycki 公式）",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "lg", "paddingAll": "16px",
            "contents": body_contents,
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary",
                 "color": C_PRIMARY, "height": "sm",
                 "action": {"type": "message", "label": "✏️ 紀錄新一筆",
                            "text": "力量紀錄"}},
            ],
        },
    })


def results_flex(report: dict, period_label: str,
                 next_period_key: str, next_period_label: str) -> FlexMessage:
    """📊 成果報告 Flex 卡。"""

    # 睡眠詳細
    sleep_detail = (
        f"{report['sleep_total']} 天 (好{report['sleep_good']}/普{report['sleep_normal']}/差{report['sleep_bad']})"
        if report['sleep_total'] > 0 else "尚未紀錄"
    )

    goal_line = "尚未設定"
    goal = report.get("active_goal")
    if goal:
        desc = (goal.get("description") or "")[:14]
        deadline_str = goal.get("deadline") or ""
        if deadline_str:
            try:
                d = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                days = (d - date.today()).days
                countdown = f" ⏳{days}d" if days > 0 else " 🔔到期"
                goal_line = desc + countdown
            except (ValueError, TypeError):
                goal_line = desc
        else:
            goal_line = desc

    rows = [
        _result_row("💧 飲水",
                    f"{report['water_total_ml']:,} ml",
                    f"達標 {report['water_days_met']} 天"),
        _result_row("🌙 睡眠", sleep_detail, ""),
        _result_row("⏱️ 有氧 / 運動",
                    f"{report['workout_count']} 次",
                    f"{report['workout_mins']} 分鐘"),
        _result_row("🏋️ 力量",
                    f"{report['strength_count']} 筆",
                    f"練了 {len(report['strength_lift_set'])} 個動作"),
        _result_row("📝 反思",
                    f"{report['reflections_count']} 篇", ""),
        _result_row("🎯 目標", goal_line, ""),
    ]
    if report["stretch_count"] > 0:
        rows.insert(3, _result_row("🪑 伸展",
                                   f"{report['stretch_count']} 次", ""))

    bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#5DADE2", "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "📊 成果",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": period_label,
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "lg", "paddingAll": "16px",
            "contents": rows,
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm",
                 "action": {"type": "postback",
                            "label": f"📅 {next_period_label}",
                            "data": f"action=results&period={next_period_key}",
                            "displayText": f"看{next_period_label}成果"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "message", "label": "📋 主選單", "text": "選單"}},
            ],
        },
    }
    return _flex(f"成果 · {period_label}", bubble)


def _result_row(label: str, value: str, sub: str) -> dict:
    contents = [
        {"type": "box", "layout": "horizontal",
         "contents": [
             {"type": "text", "text": label, "size": "sm",
              "weight": "bold", "color": C_TEXT_DARK, "flex": 3},
             {"type": "text", "text": value, "size": "sm",
              "color": C_TEXT_DARK, "weight": "bold",
              "flex": 4, "align": "end", "wrap": True},
         ]},
    ]
    if sub:
        contents.append({
            "type": "text", "text": sub, "size": "xs",
            "color": C_TEXT_SOFT, "align": "end",
        })
    return {"type": "box", "layout": "vertical", "spacing": "xs",
            "contents": contents}


COACH_MAX_TURNS = 20
COACH_EXIT_WORDS = {"結束", "離開", "退出", "結束對話", "主選單", "選單", "menu"}


def coach_disclaimer_flex() -> FlexMessage:
    body = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": C_PRIMARY,
            "paddingAll": "20px", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🤖 AI 教練",
                 "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": "使用前請閱讀",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "我能協助你：",
                 "weight": "bold", "size": "sm", "color": C_TEXT_DARK},
                _bullet("✅", "動作姿勢與技巧調整"),
                _bullet("✅", "訓練計畫安排建議"),
                _bullet("✅", "飲食原則與營養討論"),
                _bullet("✅", "增肌減脂策略"),
                {"type": "separator", "color": C_DIVIDER, "margin": "md"},
                {"type": "text", "text": "請注意：",
                 "weight": "bold", "size": "sm", "color": C_WARN, "margin": "md"},
                _bullet("❗", "我不是醫師、物治師或營養師"),
                _bullet("❗", "受傷 / 疾病 / 藥物相關 → 請就醫"),
                _bullet("❗", "建議僅供參考，執行前請評估自身狀況"),
                _bullet("❗", "訓練如有疼痛或不適，立刻停止"),
                _bullet("❗", "不討論非健身相關話題"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary",
                 "color": C_ACCENT, "height": "sm",
                 "action": {"type": "postback",
                            "label": "✅ 我同意，開始",
                            "data": "action=coach_agree",
                            "displayText": "我同意，開始對話"}},
                {"type": "button", "style": "secondary", "height": "sm",
                 "action": {"type": "message",
                            "label": "❌ 取消", "text": "選單"}},
            ],
        },
    }
    return _flex("AI 教練使用須知", body)


def _bullet(emoji: str, text: str) -> dict:
    return {
        "type": "box", "layout": "horizontal", "spacing": "sm",
        "contents": [
            {"type": "text", "text": emoji, "flex": 0, "size": "sm"},
            {"type": "text", "text": text, "wrap": True, "size": "xs",
             "color": C_TEXT_DARK, "flex": 5},
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
    """打卡子選單：運動 / 睡眠 / 飲水。"""
    reply_text(
        reply_token,
        "✅ 想打哪一張卡？",
        qr(
            ("💪 運動打卡", "運動打卡"),
            ("🌙 睡眠打卡", "睡眠打卡"),
            ("💧 飲水打卡", "飲水打卡"),
        ),
    )


def workout_checkin_menu(reply_token: str) -> None:
    """運動打卡子選單：增肌 / 減脂。"""
    reply_text(
        reply_token,
        "💪 訓練打卡 — 選一種",
        qr(
            ("🔥 增肌打卡", "增肌打卡"),
            ("✂️ 減脂打卡", "減脂打卡"),
        ),
    )


def bulk_checkin_menu(reply_token: str) -> None:
    """增肌打卡：力量紀錄 / 我的力量。"""
    reply_text(
        reply_token,
        "🔥 增肌打卡 — 想做什麼？",
        qr(
            ("✏️ 力量紀錄", "力量紀錄"),
            ("💪 我的力量", "我的力量"),
        ),
    )


def cut_checkin_menu(reply_token: str) -> None:
    """減脂打卡：有氧紀錄 + 我的有氧。"""
    reply_text(
        reply_token,
        "✂️ 減脂打卡 — 想做什麼？",
        qr(
            ("⏱️ 有氧紀錄", "有氧紀錄"),
            ("🏃 我的有氧", "我的有氧"),
        ),
    )


def show_cardio_overview(user_id: str, reply_token: str) -> None:
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」建立檔案 🙏")
        return
    data = get_cardio_overview(user_id)
    reply(reply_token, [cardio_overview_flex(data)])


def body_composition_menu(reply_token: str) -> None:
    """運動主選單：分增肌 / 減脂。"""
    reply_text(
        reply_token,
        "🏋️ 運動 — 你想往哪個方向？",
        qr(
            ("🔥 增肌", "增肌"),
            ("✂️ 減脂", "減脂"),
        ),
    )


def bulk_section_menu(reply_token: str) -> None:
    """增肌主選單：訓練菜單 / 動作圖書館。
    （力量紀錄與我的力量已搬到「打卡 → 運動打卡 → 增肌打卡」）"""
    reply_text(
        reply_token,
        "🔥 增肌專區 — 想看什麼？",
        qr(
            ("📋 訓練菜單", "訓練菜單"),
            ("📖 動作圖書館", "動作圖書館"),
        ),
    )


def cut_section_menu(reply_token: str) -> None:
    """減脂主選單：燃脂菜單 / 有氧動作圖書館。"""
    reply_text(
        reply_token,
        "✂️ 減脂專區 — 想看什麼？",
        qr(
            ("🔥 燃脂菜單", "燃脂菜單"),
            ("📖 有氧動作圖書館", "有氧動作圖書館"),
        ),
    )


def show_cardio_menus(reply_token: str) -> None:
    reply(reply_token, [cardio_menu_flex()])


def cardio_library_menu(reply_token: str) -> None:
    reply_text(
        reply_token,
        "📖 有氧動作圖書館 — 想看哪一套？",
        qr(
            ("🏠 居家", "有氧動作 居家"),
            ("🔥 HIIT", "有氧動作 HIIT"),
            ("🏃 慢跑", "有氧動作 慢跑"),
            ("⚡ Tabata", "有氧動作 Tabata"),
            ("💃 全身循環", "有氧動作 循環"),
        ),
    )


def show_cardio_exercises_by_menu(menu_zh: str, reply_token: str) -> None:
    key = _CARDIO_MUSCLE_ZH_TO_KEY.get(menu_zh)
    if not key:
        reply_text(reply_token, "找不到這套菜單 🤔")
        return
    reply(reply_token, [cardio_menu_detail_carousel_flex(key)])


def show_training_menus(reply_token: str) -> None:
    reply(reply_token, [training_menu_flex()])


def exercise_library_menu(reply_token: str) -> None:
    reply_text(
        reply_token,
        "📖 動作圖書館 — 想看哪個部位？",
        qr(
            ("背", "動作圖書館 背"),
            ("胸", "動作圖書館 胸"),
            ("肩", "動作圖書館 肩"),
            ("腿", "動作圖書館 腿"),
            ("手臂", "動作圖書館 手臂"),
        ),
    )


_MUSCLE_ZH_TO_KEY = {
    "背": "back", "胸": "chest", "肩": "shoulders",
    "腿": "legs", "手臂": "arms",
}


def show_exercises_by_muscle(muscle_zh: str, reply_token: str) -> None:
    key = _MUSCLE_ZH_TO_KEY.get(muscle_zh)
    if not key:
        reply_text(reply_token, "找不到這個部位 🤔")
        return
    # 從 WORKOUT_MENUS 拿該部位的動作清單
    menu = WORKOUT_MENUS.get(key)
    if not menu:
        reply_text(reply_token, "目前沒有這個部位的動作。")
        return
    # 顯示 carousel：該部位所有動作詳解
    reply(reply_token, [menu_detail_carousel_flex(key)])


def show_strength_overview(user_id: str, reply_token: str) -> None:
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」建立檔案 🙏")
        return
    records = get_all_lifts_overview(user_id)
    reply(reply_token, [strength_overview_flex(records)])


def show_custom_workouts(user_id: str, reply_token: str) -> None:
    """列出該使用者所有自訂菜單。"""
    workouts = get_user_custom_workouts(user_id)
    reply(reply_token, [custom_workouts_overview_flex(workouts)])


def start_custom_workout_create(user_id: str, reply_token: str) -> None:
    """開始建立自訂菜單精靈。"""
    workouts = get_user_custom_workouts(user_id)
    if len(workouts) >= MAX_CUSTOM_WORKOUTS_PER_USER:
        reply_text(
            reply_token,
            f"⚠️ 你已有 {len(workouts)} 套自訂菜單，達上限 "
            f"{MAX_CUSTOM_WORKOUTS_PER_USER} 套\n"
            "請先刪除一套再建立新的。",
        )
        return
    set_state(user_id, "custom_workout_create", "name", {})
    reply_text(
        reply_token,
        "🛠️ 建立自訂菜單\n\n"
        f"先幫菜單取個名（≤ {MAX_NAME_LEN} 字）\n"
        "例：腿日加強、推日、家裡訓練\n\n"
        "想中途離開隨時輸入「取消」",
    )


def handle_custom_workout_create(user_id: str, text: str,
                                 reply_token: str, state: dict) -> None:
    step = state["step"]
    data = state["data"] or {}

    if text in ("取消", "cancel"):
        clear_state(user_id)
        reply_text(reply_token, "已取消建立自訂菜單。")
        return

    if step == "name":
        name = text.strip()
        if not name or len(name) > MAX_NAME_LEN:
            reply_text(reply_token,
                       f"名稱要 1-{MAX_NAME_LEN} 字，請再打一次")
            return
        data["name"] = name
        data["items_buffer"] = []
        set_state(user_id, "custom_workout_create", "items", data)
        reply_text(
            reply_token,
            f"OK 菜單名：「{name}」\n\n"
            f"🏋️ 接著輸入動作清單（最多 {MAX_ITEMS_PER_CUSTOM} 個）\n\n"
            "格式「動作名 組 次」（空白分隔）\n"
            "可以一次貼多筆，用 / 或換行分隔\n\n"
            "例 1（一行一筆）：\n"
            "深蹲 4 8\n"
            "羅馬尼亞硬舉 4 8\n"
            "弓箭步 3 10\n\n"
            "例 2（一行貼完）：\n"
            "深蹲 4 8 / 硬舉 4 8 / 弓箭步 3 10\n\n"
            "打完輸入「完成」儲存、「取消」離開",
        )
        return

    if step == "items":
        if text in ("完成", "done", "結束"):
            items = data.get("items_buffer") or []
            if not items:
                reply_text(reply_token,
                           "還沒輸入任何動作，先輸入再打「完成」")
                return
            insert_custom_workout(user_id, data["name"], items)
            clear_state(user_id)
            summary = "\n".join(
                f"  {i+1}. {it['exercise']} {it['sets']}×{it['reps']}"
                for i, it in enumerate(items)
            )
            reply_text(
                reply_token,
                f"✅ 已建立「{data['name']}」\n\n{summary}\n\n"
                "輸入「自訂菜單」查看所有菜單",
                qr(("🛠️ 查看", "自訂菜單"),
                   ("✏️ 力量紀錄", "力量紀錄")),
            )
            return

        try:
            new_items = parse_custom_workout_items(text)
        except ValueError as exc:
            reply_text(reply_token,
                       f"❌ {exc}\n\n再試一次，或打「取消」")
            return

        existing = data.get("items_buffer") or []
        if len(existing) + len(new_items) > MAX_ITEMS_PER_CUSTOM:
            reply_text(
                reply_token,
                f"動作總數會超過 {MAX_ITEMS_PER_CUSTOM} 個"
                f"（已有 {len(existing)}，再加 {len(new_items)} 個）\n"
                "請少加幾個或直接打「完成」",
            )
            return

        merged = existing + new_items
        data["items_buffer"] = merged
        set_state(user_id, "custom_workout_create", "items", data)
        summary = "\n".join(
            f"  {i+1}. {it['exercise']} {it['sets']}×{it['reps']}"
            for i, it in enumerate(merged)
        )
        reply_text(
            reply_token,
            f"目前 {len(merged)}/{MAX_ITEMS_PER_CUSTOM} 個動作：\n\n"
            f"{summary}\n\n"
            "繼續加動作 / 打「完成」儲存 / 「取消」離開",
        )
        return


def start_strength_log(user_id: str, reply_token: str) -> None:
    """力量紀錄精靈：自由輸入動作名稱 → 重量 → 次數 → 組數。"""
    set_state(user_id, "strength_log", "exercise", {})
    reply_text(
        reply_token,
        "💪 今天練什麼？直接打動作名稱\n（例：深蹲、啞鈴二頭、滑輪下拉、保加利亞分腿蹲）",
    )


def handle_strength_log(user_id: str, text: str, reply_token: str, state: dict) -> None:
    step = state["step"]
    data = state["data"] or {}

    if step == "exercise":
        ex_name = text.strip()
        if not ex_name or len(ex_name) > 50:
            reply_text(reply_token,
                       "動作名稱不能空白或超過 50 字，請再打一次")
            return
        data["exercise"] = ex_name
        set_state(user_id, "strength_log", "weight", data)
        reply_text(reply_token, f"{ex_name} 多少公斤？（直接打數字）")
        return

    if step == "weight":
        try:
            data["weight"] = float(text)
        except ValueError:
            reply_text(reply_token, "請打數字，例如 80")
            return
        set_state(user_id, "strength_log", "reps", data)
        reply_text(reply_token, "做了幾下？（直接打數字）")
        return

    if step == "reps":
        try:
            reps = int(text)
        except ValueError:
            reply_text(reply_token, "請打數字，例如 5")
            return
        if reps < 1 or reps > 20:
            reply_text(reply_token, "次數請填 1-20 之間。")
            return
        data["reps"] = reps
        set_state(user_id, "strength_log", "sets", data)
        reply_text(reply_token, "做了幾組？（直接打數字）")
        return

    if step == "sets":
        try:
            sets = int(text)
        except ValueError:
            reply_text(reply_token, "請打數字，例如 4")
            return
        if sets < 1 or sets > 20:
            reply_text(reply_token, "組數請填 1-20 之間。")
            return
        reps = data["reps"]
        rec = log_strength(user_id, data["exercise"],
                           data["weight"], reps, sets)
        clear_state(user_id)
        one_rm = rec.get("one_rm", calc_one_rm(data["weight"], reps))
        reply_text(
            reply_token,
            f"✅ 已紀錄 {data['exercise']} "
            f"{int(data['weight'])}kg × {reps} × {sets} 組\n\n"
            f"💪 估算 1RM：{one_rm:.0f} kg",
            qr(("💪 看我的力量", "我的力量"),
               ("✏️ 再紀錄一筆", "力量紀錄")),
        )
        return


def _period_dates(key: str) -> tuple:
    """回傳 (start_date, end_date, label, next_key, next_label)。"""
    today = datetime.now(TPE).date()
    if key == "this_month":
        start = today.replace(day=1)
        end = today
        label = today.strftime("%Y年 %m 月")
        return (start, end, label, "last_month", "上個月")
    if key == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        start = last_prev.replace(day=1)
        end = last_prev
        label = start.strftime("%Y年 %m 月")
        return (start, end, label, "last_7", "過去 7 天")
    if key == "last_7":
        start = today - timedelta(days=6)
        end = today
        label = "過去 7 天"
        return (start, end, label, "this_month", "本月")
    # default
    start = today.replace(day=1)
    return (start, today, today.strftime("%Y年 %m 月"), "last_month", "上個月")


def show_results(user_id: str, reply_token: str,
                 period: str = "this_month") -> None:
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」建立檔案 🙏")
        return
    start, end, label, next_key, next_label = _period_dates(period)
    report = get_monthly_report(user_id, start, end)
    reply(reply_token, [results_flex(report, label, next_key, next_label)])


def calendar_section_placeholder(reply_token: str, user_id: str = "") -> None:
    """日曆已被「成果」取代，自動跳轉。"""
    if user_id:
        show_results(user_id, reply_token)
    else:
        reply_text(reply_token, "請輸入「成果」看本月統計")


def start_ai_coach(user_id: str, reply_token: str) -> None:
    """AI 教練入口：第一次先看免責聲明，同意過就直接開聊。"""
    profile = get_profile(user_id)
    if not profile:
        reply_text(reply_token, "請先輸入「個人資料」建立檔案 🙏")
        return
    if not has_agreed_coach(user_id):
        reply(reply_token, [coach_disclaimer_flex()])
        return
    _enter_coach_chat(user_id, reply_token)


def _enter_coach_chat(user_id: str, reply_token: str) -> None:
    """正式進入聊天模式：設 state、送歡迎訊息。"""
    set_state(user_id, "coach_chat", "active", {"history": [], "turn": 0})
    reply_text(
        reply_token,
        "🤖 嗨，我是你的 AI 健身教練。\n"
        "想聊動作姿勢、訓練計畫、飲食增肌減脂，都可以問我。\n\n"
        "🚪 想結束對話打「結束」或「主選單」",
        qr(
            ("📝 問動作姿勢", "我想問動作姿勢相關的問題"),
            ("🍱 問飲食營養", "我想問飲食營養相關的問題"),
            ("📋 問訓練計畫", "我想問訓練計畫相關的問題"),
            ("🚪 結束", "結束"),
        ),
    )


def handle_coach_chat(user_id: str, text: str,
                     reply_token: str, state: dict) -> None:
    """聊天回合處理。"""
    # 退出指令
    if text.strip() in COACH_EXIT_WORDS:
        clear_state(user_id)
        reply(reply_token, [
            TextMessage(text="🤖 對話結束，動起來吧 💪",
                        quick_reply=qr(("📋 主選單", "選單"))),
        ])
        return

    data = state.get("data") or {}
    history = data.get("history") or []
    turn = int(data.get("turn") or 0)

    # 回合上限
    if turn >= COACH_MAX_TURNS:
        clear_state(user_id)
        reply_text(
            reply_token,
            f"🤖 我們聊滿 {COACH_MAX_TURNS} 輪了，今天聊夠多 👍\n"
            "去動一下吧，需要再打「AI教練」回來聊。",
            qr(("📋 主選單", "選單")),
        )
        return

    # 丟給 Gemini
    answer = claude_coach_chat(history, text)

    # 更新歷史 + 回合數
    history.append({"role": "user", "text": text})
    history.append({"role": "model", "text": answer})
    # 只留最近 12 則，避免 state 越長越大
    history = history[-12:]
    set_state(user_id, "coach_chat", "active",
              {"history": history, "turn": turn + 1})

    remaining = COACH_MAX_TURNS - (turn + 1)
    footer_hint = f"\n\n💬 剩 {remaining} 輪 · 打「結束」可離開" if remaining <= 5 else ""

    reply_text(
        reply_token,
        f"{answer}{footer_hint}",
        qr(("🚪 結束對話", "結束")),
    )


def start_workout_log(user_id: str, reply_token: str) -> None:
    set_state(user_id, "workout_log", "minutes", {})
    reply_text(reply_token, "⏱️ 今天有氧多少分鐘？（直接打數字）")


def handle_workout_log(user_id: str, text: str, reply_token: str, state: dict) -> None:
    try:
        minutes = float(text)
    except ValueError:
        reply_text(reply_token, "請打數字喔，例如 45")
        return
    log_habit(user_id, "workout", amount=minutes)
    clear_state(user_id)
    reply_text(
        reply_token,
        f"📒 紀錄完成：今天 {int(minutes)} 分鐘 ✅\n動了就是贏了 💪",
        qr(
            ("🏃 我的有氧", "我的有氧"),
            ("📋 主選單", "選單"),
        ),
    )


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
        if flow == "strength_log":
            handle_strength_log(user_id, text, reply_token, state)
            return
        if flow == "custom_workout_create":
            handle_custom_workout_create(user_id, text, reply_token, state)
            return
        if flow == "coach_chat":
            handle_coach_chat(user_id, text, reply_token, state)
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
        workout_checkin_menu(reply_token)
        return
    if text in ("增肌打卡", "🔥 增肌打卡"):
        bulk_checkin_menu(reply_token)
        return
    if text in ("減脂打卡", "✂️ 減脂打卡"):
        cut_checkin_menu(reply_token)
        return
    if text in ("有氧紀錄", "⏱️ 有氧紀錄", "運動分鐘", "運動分鐘紀錄"):
        start_workout_log(user_id, reply_token)
        return
    if text in ("我的有氧", "🏃 我的有氧", "有氧總覽"):
        show_cardio_overview(user_id, reply_token)
        return
    if text in ("飲水", "💧 飲水", "飲水紀錄", "飲水打卡", "💧 飲水打卡"):
        show_water_card(user_id, reply_token)
        return
    if text in ("睡眠紀錄", "🌙 睡眠紀錄", "睡眠", "睡眠打卡", "🌙 睡眠打卡"):
        show_sleep_card(reply_token)
        return
    if text in ("運動", "🏋️ 運動", "體態", "🏋️ 體態",
                "健身", "🏋️ 健身", "健身專區"):
        body_composition_menu(reply_token)
        return
    if text in ("增肌", "🔥 增肌", "增肌專區"):
        bulk_section_menu(reply_token)
        return
    if text in ("訓練菜單", "📋 訓練菜單", "課表"):
        show_training_menus(reply_token)
        return
    if text in ("動作圖書館", "📖 動作圖書館", "動作百科"):
        exercise_library_menu(reply_token)
        return
    # 部位查詢
    if text.startswith("動作圖書館 "):
        muscle_zh = text.replace("動作圖書館 ", "", 1).strip()
        show_exercises_by_muscle(muscle_zh, reply_token)
        return
    if text in ("我的力量", "💪 我的力量", "力量總覽"):
        show_strength_overview(user_id, reply_token)
        return
    if text in ("力量紀錄", "✏️ 力量紀錄", "紀錄力量"):
        start_strength_log(user_id, reply_token)
        return
    if text in ("自訂菜單", "🛠️ 自訂菜單", "我的菜單", "我的自訂菜單"):
        show_custom_workouts(user_id, reply_token)
        return
    if text in ("建立自訂菜單", "新增自訂菜單", "建立菜單"):
        start_custom_workout_create(user_id, reply_token)
        return
    if text in ("減脂", "✂️ 減脂", "減脂專區"):
        cut_section_menu(reply_token)
        return
    if text in ("燃脂菜單", "🔥 燃脂菜單", "燃脂課表"):
        show_cardio_menus(reply_token)
        return
    if text in ("有氧動作圖書館", "📖 有氧動作圖書館", "有氧動作"):
        cardio_library_menu(reply_token)
        return
    if text.startswith("有氧動作 "):
        menu_zh = text.replace("有氧動作 ", "", 1).strip()
        show_cardio_exercises_by_menu(menu_zh, reply_token)
        return
    if text in ("日曆", "📅 日曆", "行事曆"):
        calendar_section_placeholder(reply_token, user_id)
        return
    if text in ("成果", "📊 成果", "報告", "我的成果"):
        show_results(user_id, reply_token)
        return
    if text in ("AI教練", "AI 教練", "🤖 AI 教練", "🤖 AI教練", "教練 Bot"):
        start_ai_coach(user_id, reply_token)
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
    if text in ("功能設定", "⚙️ 功能設定", "設定", "⚙️ 設定", "刪除", "刪除資料"):
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
            pct = min(100, int(current * 100 / max(1, target)))
            cheer = (
                "🎉 達標了！繼續保持！" if pct >= 100
                else "💪 快達標了！再加一杯！" if pct >= 75
                else "👍 進度不錯，繼續喝！" if pct >= 50
                else "💧 慢慢累積，每口都算！"
            )
            reply_text(
                reply_token,
                f"🥤 乾杯！\n今天累積 {current} / {target} ml ({pct}%)\n{cheer}",
                qr(
                    ("📋 主選單", "選單"),
                    ("📊 今日進度", "今日"),
                ),
            )
            return

        if action == "log_sleep":
            quality = params.get("quality", "normal")
            log_habit(user_id, "sleep", quality=quality)
            label = {"good": "睡得好 😴", "normal": "普通 😐", "bad": "睡得不好 😣"}.get(quality, "")
            cheer = {
                "good": "💪 修復滿格，今天有體力好好訓練！",
                "normal": "🌱 累積好習慣，明天可以更好",
                "bad": "🛌 今晚試試提早 30 分鐘關燈，肌肉是在睡眠中長大的",
            }.get(quality, "")
            reply_text(
                reply_token,
                f"🌙 已紀錄昨晚 {label}\n{cheer}",
                qr(
                    ("📋 主選單", "選單"),
                    ("📊 今日進度", "今日"),
                ),
            )
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

        if action == "menu_detail":
            menu_key = params.get("key", "")
            if menu_key not in WORKOUT_MENUS:
                reply_text(reply_token, "找不到這個菜單 🤔")
                return
            reply(reply_token, [menu_detail_carousel_flex(menu_key)])
            return

        if action == "cardio_menu_detail":
            menu_key = params.get("key", "")
            if menu_key not in CARDIO_MENUS:
                reply_text(reply_token, "找不到這個菜單 🤔")
                return
            reply(reply_token, [cardio_menu_detail_carousel_flex(menu_key)])
            return

        if action == "coach_agree":
            profile = get_profile(user_id)
            if not profile:
                reply_text(reply_token, "請先輸入「個人資料」建立檔案 🙏")
                return
            mark_coach_agreed(user_id)
            _enter_coach_chat(user_id, reply_token)
            return

        if action == "results":
            period = params.get("period", "this_month")
            show_results(user_id, reply_token, period)
            return

        if action == "cw_view":
            try:
                wid = int(params.get("id", "0"))
            except ValueError:
                reply_text(reply_token, "找不到這套自訂菜單 🤔")
                return
            workout = get_custom_workout(wid, user_id)
            if not workout:
                reply_text(reply_token, "找不到這套自訂菜單 🤔")
                return
            reply(reply_token, [custom_workout_card_flex(workout)])
            return

        if action == "cw_delete":
            try:
                wid = int(params.get("id", "0"))
            except ValueError:
                reply_text(reply_token, "找不到這套自訂菜單 🤔")
                return
            workout = get_custom_workout(wid, user_id)
            if not workout:
                reply_text(reply_token, "找不到這套自訂菜單 🤔")
                return
            delete_custom_workout(wid, user_id)
            workouts = get_user_custom_workouts(user_id)
            reply(reply_token, [
                TextMessage(text=f"🗑️ 已刪除「{workout['name']}」"),
                custom_workouts_overview_flex(workouts),
            ])
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


@app.route("/muscles/<path:filename>")
def serve_muscle(filename):
    """提供肌肉剪影 PNG 給 Flex Message 用。"""
    return send_from_directory(MUSCLE_DIR, filename)


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
