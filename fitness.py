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

import json
import logging
import os
from datetime import date, datetime
from typing import Optional

import anthropic
import markdown
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from flask import Flask, abort, request
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

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

CLAUDE_MODEL = "claude-opus-4-7"

app = Flask(__name__)

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# 2. Supabase DB Helpers
# ============================================================


def get_profile(user_id: str) -> Optional[dict]:
    res = supabase.table("profiles").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None


def upsert_profile(user_id: str, fields: dict) -> None:
    fields["user_id"] = user_id
    fields["updated_at"] = datetime.utcnow().isoformat()
    supabase.table("profiles").upsert(fields).execute()


def get_state(user_id: str) -> Optional[dict]:
    res = (
        supabase.table("conversation_state")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )
    return res.data[0] if res.data else None


def set_state(user_id: str, flow: str, step: str, data: Optional[dict] = None) -> None:
    payload = {
        "user_id": user_id,
        "flow": flow,
        "step": step,
        "data": data or {},
        "updated_at": datetime.utcnow().isoformat(),
    }
    supabase.table("conversation_state").upsert(payload).execute()


def clear_state(user_id: str) -> None:
    supabase.table("conversation_state").delete().eq("user_id", user_id).execute()


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


def insert_goal(user_id: str, fields: dict) -> dict:
    fields["user_id"] = user_id
    res = supabase.table("goals").insert(fields).execute()
    return res.data[0] if res.data else {}


def log_habit(user_id: str, type_: str, amount: Optional[float] = None,
              quality: Optional[str] = None, note: Optional[str] = None) -> None:
    supabase.table("habit_logs").insert({
        "user_id": user_id,
        "type": type_,
        "amount": amount,
        "quality": quality,
        "note": note,
    }).execute()


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


def insert_reflection(user_id: str, period: str, content: str, ai_summary: str) -> None:
    supabase.table("reflections").insert({
        "user_id": user_id,
        "period": period,
        "content": content,
        "ai_summary": ai_summary,
    }).execute()


def all_active_user_ids() -> list[str]:
    """推播 job 用：取出所有已建檔的使用者 ID。"""
    res = supabase.table("profiles").select("user_id").execute()
    return [row["user_id"] for row in (res.data or [])]


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
# 4. Claude API 包裝
# ============================================================


def claude_ask(prompt: str, system: str = "", max_tokens: int = 1024) -> str:
    """單次呼叫 Claude，回傳純文字（Markdown → 純文字）。

    使用 claude-opus-4-7。LINE bot 即時對話延遲敏感，
    不開啟 adaptive thinking（4.7 預設為關閉）。
    """
    try:
        kwargs = {
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        response = claude_client.messages.create(**kwargs)
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Claude error: %s", exc)
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
# 5. Flex Message 樣板
# ============================================================


def _flex(alt: str, contents: dict) -> FlexMessage:
    container = FlexContainer.from_json(json.dumps(contents))
    return FlexMessage(alt_text=alt, contents=container)


def main_menu_flex() -> FlexMessage:
    body = {
        "type": "carousel",
        "contents": [
            _menu_card("🎯 目標設定與追蹤", "用 SMART 框架設定目標，定期回顧進度。",
                       "#F25F5C", "目標設定", "目標設定"),
            _menu_card("🌱 自我成長與習慣", "運動 / 飲水 / 睡眠 / 反思 一起紀錄。",
                       "#247BA0", "自我成長", "自我成長"),
            _menu_card("🥗 飲食與健康", "TDEE 規劃、彈性菜單、運動點心。",
                       "#70C1B3", "飲食與健康", "飲食與健康"),
        ],
    }
    return _flex("主選單", body)


def _menu_card(title: str, subtitle: str, color: str, btn_label: str, btn_text: str) -> dict:
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": color, "paddingAll": "16px",
            "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg", "color": "#FFFFFF"}],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [{"type": "text", "text": subtitle, "wrap": True, "size": "sm", "color": "#555555"}],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{
                "type": "button", "style": "primary", "color": color,
                "action": {"type": "message", "label": btn_label, "text": btn_text},
            }],
        },
    }


def tdee_result_flex(profile: dict) -> FlexMessage:
    target_label = {"bulk": "增肌", "maintain": "維持", "cut": "減脂"}.get(profile["target_type"], "維持")
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1B4965", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "你的能量基準線", "color": "#FFFFFF", "size": "sm"},
                {"type": "text", "text": f"目標：{target_label}期", "color": "#FFFFFF", "weight": "bold", "size": "xl"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                _kv("BMR", f"{int(profile['bmr'])} kcal"),
                _kv("TDEE", f"{int(profile['tdee'])} kcal"),
                _kv("目標熱量", f"{int(profile['target_kcal'])} kcal/日"),
                {"type": "separator"},
                _kv("蛋白質", f"{profile['protein_g']} g"),
                _kv("碳水", f"{profile['carb_g']} g"),
                _kv("脂肪", f"{profile['fat_g']} g"),
                {"type": "separator"},
                _kv("每日喝水", f"{profile['daily_water_ml']} ml"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1B4965",
                 "action": {"type": "message", "label": "看主選單", "text": "選單"}},
                {"type": "button", "style": "secondary",
                 "action": {"type": "message", "label": "我要重新設定", "text": "個人資料"}},
            ],
        },
    }
    return _flex("TDEE 結果", body)


def _kv(k: str, v: str) -> dict:
    return {
        "type": "box", "layout": "horizontal",
        "contents": [
            {"type": "text", "text": k, "color": "#555555", "size": "sm", "flex": 4},
            {"type": "text", "text": v, "wrap": True, "size": "sm", "flex": 5, "align": "end", "weight": "bold"},
        ],
    }


def goal_card_flex(goal: dict) -> FlexMessage:
    deadline = goal.get("deadline") or "—"
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#F25F5C", "paddingAll": "16px",
            "contents": [{"type": "text", "text": "🎯 你的目標", "color": "#FFFFFF", "weight": "bold", "size": "lg"}],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": goal.get("description") or "（未填）", "wrap": True, "weight": "bold"},
                {"type": "separator", "margin": "md"},
                _kv("可量化指標", goal.get("smart_measurable") or "—"),
                _kv("期限", str(deadline)),
                _kv("本週第一步", goal.get("first_step") or "—"),
                _kv("里程碑", goal.get("milestones") or "—"),
                _kv("回顧頻率", "每日" if goal.get("review_freq") == "daily" else "每週"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#F25F5C",
                 "action": {"type": "postback", "label": "完成這個目標", "data": "action=goal_done",
                            "displayText": "我完成這個目標了"}},
                {"type": "button", "style": "secondary",
                 "action": {"type": "message", "label": "設新目標", "text": "新目標"}},
            ],
        },
    }
    return _flex("我的目標", body)


def water_card_flex(current_ml: int, target_ml: int) -> FlexMessage:
    pct = min(100, int(current_ml * 100 / max(1, target_ml)))
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#247BA0", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "💧 今日飲水進度", "color": "#FFFFFF", "weight": "bold"},
                {"type": "text", "text": f"{current_ml} / {target_ml} ml ({pct}%)",
                 "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "box", "layout": "vertical",
                 "backgroundColor": "#E0E0E0", "height": "12px", "cornerRadius": "6px",
                 "contents": [{"type": "box", "layout": "vertical",
                               "backgroundColor": "#247BA0",
                               "width": f"{pct}%", "height": "12px", "cornerRadius": "6px",
                               "contents": [{"type": "filler"}]}]},
                {"type": "text", "text": "選一杯水量打卡：", "size": "sm", "color": "#555555", "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#247BA0",
                 "action": {"type": "postback", "label": "🥤 200 ml",
                            "data": "action=log_water&amount=200", "displayText": "我喝了 200 ml"}},
                {"type": "button", "style": "primary", "color": "#247BA0",
                 "action": {"type": "postback", "label": "🍶 350 ml",
                            "data": "action=log_water&amount=350", "displayText": "我喝了 350 ml"}},
                {"type": "button", "style": "primary", "color": "#247BA0",
                 "action": {"type": "postback", "label": "🧴 500 ml",
                            "data": "action=log_water&amount=500", "displayText": "我喝了 500 ml"}},
            ],
        },
    }
    return _flex("飲水進度", body)


def sleep_card_flex() -> FlexMessage:
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#5B6CFF", "paddingAll": "16px",
            "contents": [{"type": "text", "text": "🌙 昨晚睡得好嗎？", "color": "#FFFFFF", "weight": "bold"}],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text",
                 "text": "肌肉是在睡眠中修復長大的，先回想一下昨晚的狀態。",
                 "wrap": True, "size": "sm", "color": "#555555"},
                {"type": "text", "text": "請選擇昨晚的睡眠品質：", "size": "sm", "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#5B6CFF",
                 "action": {"type": "postback", "label": "😴 很好",
                            "data": "action=log_sleep&quality=good", "displayText": "昨晚睡得很好"}},
                {"type": "button", "style": "secondary",
                 "action": {"type": "postback", "label": "😐 普通",
                            "data": "action=log_sleep&quality=normal", "displayText": "昨晚普通"}},
                {"type": "button", "style": "secondary",
                 "action": {"type": "postback", "label": "😣 不太好",
                            "data": "action=log_sleep&quality=bad", "displayText": "昨晚睡得不好"}},
            ],
        },
    }
    return _flex("睡眠回顧", body)


def stretch_card_flex() -> FlexMessage:
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#F4A261", "paddingAll": "16px",
            "contents": [{"type": "text", "text": "🪑 久坐破冰時間！", "color": "#FFFFFF", "weight": "bold"}],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "1 分鐘辦公室伸展：", "weight": "bold"},
                {"type": "text",
                 "text": "1) 起身原地踏步 20 下\n2) 雙手扶椅背，胸口前推 10 秒 x 3\n3) 髖屈肌弓箭步伸展 左右各 20 秒",
                 "wrap": True, "size": "sm", "color": "#555555"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "button", "style": "primary", "color": "#F4A261",
                          "action": {"type": "postback", "label": "✅ 我動起來了！",
                                     "data": "action=log_stretch", "displayText": "我做完伸展了"}}],
        },
    }
    return _flex("久坐伸展", body)


def macro_visual_flex(profile: dict) -> FlexMessage:
    p_g, c_g, f_g = profile["protein_g"], profile["carb_g"], profile["fat_g"]
    p_kcal, c_kcal, f_kcal = p_g * 4, c_g * 4, f_g * 9
    total = max(1, p_kcal + c_kcal + f_kcal)
    p_pct, c_pct, f_pct = int(p_kcal * 100 / total), int(c_kcal * 100 / total), int(f_kcal * 100 / total)
    target_label = {"bulk": "增肌期", "maintain": "維持期", "cut": "減脂期"}.get(profile["target_type"], "")

    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#264653", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "🥗 今日三大營養素", "color": "#FFFFFF", "weight": "bold"},
                {"type": "text", "text": target_label, "color": "#FFFFFF", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                _macro_bar("🥩 蛋白質", p_g, p_pct, "#E76F51"),
                _macro_bar("🍚 碳水", c_g, c_pct, "#E9C46A"),
                _macro_bar("🥑 脂肪", f_g, f_pct, "#2A9D8F"),
                {"type": "separator", "margin": "md"},
                {"type": "text",
                 "text": _macro_coach_note(profile["target_type"]),
                 "wrap": True, "size": "sm", "color": "#555555"},
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
                 {"type": "text", "text": label, "size": "sm", "flex": 3},
                 {"type": "text", "text": f"{grams} g ({pct}%)", "size": "sm", "align": "end", "flex": 4, "weight": "bold"},
             ]},
            {"type": "box", "layout": "vertical",
             "backgroundColor": "#EEEEEE", "height": "8px", "cornerRadius": "4px",
             "contents": [{"type": "box", "layout": "vertical",
                           "backgroundColor": color, "width": f"{max(1, pct)}%",
                           "height": "8px", "cornerRadius": "4px",
                           "contents": [{"type": "filler"}]}]},
        ],
    }


def _macro_coach_note(target_type: str) -> str:
    return {
        "bulk": "教練碎碎念：增肌期把碳水拉高、蛋白質充足，是為了給肌肉成長的原料和能量。",
        "maintain": "教練碎碎念：維持期三大營養均衡，重點在穩定攝取、不要暴衝暴掉。",
        "cut": "教練碎碎念：今天幫你拉高蛋白質，是為了讓你減脂期充滿飽足感，且不掉肌肉喔！",
    }.get(target_type, "教練碎碎念：吃好吃滿，但吃對東西，是體態改變的基石。")


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
        "我們先做一份基本檔案，算出你的每日能量基準線（TDEE）。\n第一題：你的生理性別？",
        qr(("男", "男"), ("女", "女")),
    )


def handle_profile_setup(user_id: str, text: str, reply_token: str, state: dict) -> None:
    step = state["step"]
    data = state["data"] or {}

    if step == "gender":
        if text in ("男", "male"):
            data["gender"] = "male"
        elif text in ("女", "female"):
            data["gender"] = "female"
        else:
            reply_text(reply_token, "請點下方按鈕選「男」或「女」。",
                       qr(("男", "男"), ("女", "女")))
            return
        set_state(user_id, "profile_setup", "age", data)
        reply_text(reply_token, "年齡是幾歲？（直接輸入數字）")
        return

    if step == "age":
        try:
            data["age"] = int(text)
        except ValueError:
            reply_text(reply_token, "請輸入數字年齡，例如 25。")
            return
        set_state(user_id, "profile_setup", "height", data)
        reply_text(reply_token, "身高幾公分？（例如 170）")
        return

    if step == "height":
        try:
            data["height_cm"] = float(text)
        except ValueError:
            reply_text(reply_token, "請輸入身高公分數，例如 170。")
            return
        set_state(user_id, "profile_setup", "weight", data)
        reply_text(reply_token, "體重幾公斤？（例如 65）")
        return

    if step == "weight":
        try:
            data["weight_kg"] = float(text)
        except ValueError:
            reply_text(reply_token, "請輸入體重公斤數，例如 65。")
            return
        set_state(user_id, "profile_setup", "activity", data)
        reply_text(
            reply_token,
            "每週運動頻率？",
            qr(
                ("久坐", "久坐"),
                ("輕度 1-3", "輕度"),
                ("中度 3-5", "中度"),
                ("高度 6-7", "高度"),
                ("極高 每天2次", "極高"),
            ),
        )
        return

    if step == "activity":
        mapping = {
            "久坐": "sedentary", "輕度": "light", "中度": "moderate",
            "高度": "active", "極高": "very_active",
        }
        if text not in mapping:
            reply_text(reply_token, "請從按鈕選一個運動頻率。")
            return
        data["activity_level"] = mapping[text]
        set_state(user_id, "profile_setup", "target", data)
        reply_text(
            reply_token,
            "目標方向？",
            qr(("增肌", "增肌"), ("維持", "維持"), ("減脂", "減脂")),
        )
        return

    if step == "target":
        mapping = {"增肌": "bulk", "維持": "maintain", "減脂": "cut"}
        if text not in mapping:
            reply_text(reply_token, "請選增肌 / 維持 / 減脂。")
            return
        data["target_type"] = mapping[text]
        set_state(user_id, "profile_setup", "eating_style", data)
        reply_text(reply_token, "主要用餐型態？",
                   qr(("外食族", "外食"), ("自己煮", "自煮")))
        return

    if step == "eating_style":
        if text in ("外食", "外食族"):
            data["eating_style"] = "outside"
        elif text in ("自煮", "自己煮"):
            data["eating_style"] = "home"
        else:
            reply_text(reply_token, "請選外食 / 自煮。",
                       qr(("外食族", "外食"), ("自己煮", "自煮")))
            return
        set_state(user_id, "profile_setup", "vegetarian", data)
        reply_text(reply_token, "你是素食者嗎？",
                   qr(("是", "素食"), ("否", "葷食")))
        return

    if step == "vegetarian":
        data["is_vegetarian"] = (text == "素食")
        set_state(user_id, "profile_setup", "workout_time", data)
        reply_text(
            reply_token,
            "通常什麼時段運動？",
            qr(("晨練", "晨練"), ("下午練", "下午練"), ("夜練", "夜練")),
        )
        return

    if step == "workout_time":
        mapping = {"晨練": "morning", "下午練": "afternoon", "夜練": "evening"}
        if text not in mapping:
            reply_text(reply_token, "請選晨練 / 下午練 / 夜練。")
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
        TextMessage(text="檔案建立完成 ✨ 這是你的能量基準線："),
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
        "🌱 自我成長與習慣，今天想做什麼？",
        qr(
            ("💪 運動打卡", "運動打卡"),
            ("💧 飲水", "飲水"),
            ("🌙 睡眠紀錄", "睡眠紀錄"),
            ("📝 反思", "反思"),
            ("🚫 壞習慣紀錄", "壞習慣"),
            ("📚 學習一則", "健身知識"),
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


def start_workout_log(user_id: str, reply_token: str) -> None:
    set_state(user_id, "workout_log", "minutes", {})
    reply_text(reply_token, "今天運動了幾分鐘？（直接輸入數字）")


def handle_workout_log(user_id: str, text: str, reply_token: str, state: dict) -> None:
    try:
        minutes = float(text)
    except ValueError:
        reply_text(reply_token, "請輸入數字（分鐘）。")
        return
    log_habit(user_id, "workout", amount=minutes)
    clear_state(user_id)
    reply_text(reply_token,
               f"📒 已紀錄今天運動 {int(minutes)} 分鐘。動了就是贏了！\n回主選單請輸入「選單」。")


def bad_habit_menu(reply_token: str) -> None:
    reply_text(
        reply_token,
        "🚫 想紀錄哪一個？",
        qr(("熬夜", "熬夜紀錄"), ("暴食", "暴食紀錄"), ("缺乏運動", "缺乏運動紀錄")),
    )


def quick_log_bad_habit(user_id: str, text: str, reply_token: str) -> None:
    mapping = {
        "熬夜紀錄": ("late_night", "今天熬夜了 ⏰ 記住：睡眠也是訓練的一部分。明天早點休息吧。"),
        "暴食紀錄": ("binge", "暴食已紀錄。下次嘴饞前，先喝一杯水、走 5 分鐘看看。"),
        "缺乏運動紀錄": ("no_exercise", "今天沒動到也沒關係，明天起來做 10 下深蹲就算開始。"),
    }
    type_, msg = mapping[text]
    log_habit(user_id, type_)
    reply_text(reply_token, msg)


def start_reflection(user_id: str, reply_token: str) -> None:
    set_state(user_id, "reflection", "period", {})
    reply_text(
        reply_token,
        "📝 要寫哪一種反思？",
        qr(("訓練心得", "訓練心得"), ("週回顧", "週回顧"), ("月回顧", "月回顧")),
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
        "🥗 飲食與健康，要看哪個？",
        qr(
            ("營養素比例", "營養素比例"),
            ("今日午餐建議", "午餐建議"),
            ("今日晚餐建議", "晚餐建議"),
            ("運動前點心", "運動前點心"),
            ("運動後點心", "運動後點心"),
            ("點心衛教", "點心衛教"),
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
    "嗨，我是你的隨身健身教練 🤖💪\n\n"
    "三個主功能：\n"
    "🎯 目標設定與追蹤\n"
    "🌱 自我成長與習慣\n"
    "🥗 飲食與健康\n\n"
    "請先輸入「個人資料」建立 TDEE 檔案，"
    "再輸入「選單」開始使用！"
)


@handler.add(FollowEvent)
def handle_follow(event):
    reply_text(event.reply_token, WELCOME,
               qr(("⚙️ 個人資料", "個人資料"), ("📋 看主選單", "選單")))


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
    if text in ("運動打卡", "💪 運動打卡"):
        start_workout_log(user_id, reply_token)
        return
    if text in ("飲水", "💧 飲水", "飲水紀錄"):
        show_water_card(user_id, reply_token)
        return
    if text in ("睡眠紀錄", "🌙 睡眠紀錄", "睡眠"):
        show_sleep_card(reply_token)
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

    if text in ("飲食與健康", "🥗 飲食與健康", "飲食"):
        diet_menu(reply_token)
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("postback error: %s", exc)
        reply_text(reply_token, "教練處理時打結了 🤯 請稍後再試。")


# ============================================================
# 13. APScheduler — 定時推播
# ============================================================


def job_morning_sleep_recap():
    logger.info("[scheduler] 推播早安睡眠回顧")
    for uid in all_active_user_ids():
        push(uid, [
            TextMessage(text="早安 ☀️ 先做今天的第一個紀錄："),
            sleep_card_flex(),
        ])


def job_water_reminder():
    logger.info("[scheduler] 推播飲水提醒")
    for uid in all_active_user_ids():
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
    logger.info("[scheduler] 推播久坐伸展")
    for uid in all_active_user_ids():
        push(uid, [stretch_card_flex()])


def job_goal_review(freq: str):
    logger.info("[scheduler] 推播目標回顧 freq=%s", freq)
    for goal in users_with_goal_review(freq):
        uid = goal["user_id"]
        push(uid, [
            TextMessage(text="該回顧目標進度啦 🎯"),
            goal_card_flex(goal),
        ])


def init_scheduler():
    sched = BackgroundScheduler(timezone="Asia/Taipei")
    sched.add_job(job_morning_sleep_recap, "cron", hour=7, minute=30, id="sleep_recap")
    sched.add_job(job_water_reminder, "cron", hour="9,12,15,18", minute=0, id="water")
    sched.add_job(job_stretch_reminder, "cron",
                  day_of_week="mon-fri", hour="11,14,16", minute=0, id="stretch")
    sched.add_job(lambda: job_goal_review("daily"), "cron",
                  hour=21, minute=0, id="goal_daily")
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
