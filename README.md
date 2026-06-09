---
title: Linebot
emoji: 💪
colorFrom: yellow
colorTo: green
sdk: docker
pinned: false
---

# SCU LINE Bot — 健身紀錄與體態調整助手

東吳大學資料科學系 2026 上半年 LINE Bot 進階課程的教學範例集。整個 repo 是**多個獨立、可單檔執行的 LINE Bot 範例**，搭配 Hugging Face Spaces（Docker SDK）部署。

---

## 各範例檔對照

| 檔名 | 重點 | 模型 |
|---|---|---|
| `replybot.py` | 最基本的單次對話樣板（正典） | gemini-3.1-flash-lite |
| `system_prompt.py` | 加入 `system_instruction` | gemini-3-flash-preview |
| `with_logs.py` | 加入 `logging` | gemini-3-flash-preview |
| `multiturn.py` | 多輪對話 (`client.chats.create`) | gemini-3-flash-preview |
| `with_search.py` | 把 Google Search 當 tool 用 | gemini-3-flash-preview |
| `gemini.py` / `example01.py` | 完整版：文字 + 圖片 + 影片 + 生圖 | gemini-3-flash-preview / pro-preview |
| `gpt4.py` | 同樣樣板換 OpenAI（gpt-4o-mini / DALL·E 3） | OpenAI |
| **`fitness.py`** | **健身紀錄與體態調整 LINE Bot（最終專案）** | gemini-3.1-flash-lite + Supabase |

`Dockerfile` 預設入口是 `fitness.py`（`CMD ["gunicorn", "-w", "1", ..., "fitness:app"]`）。要 demo 其他範例就改最後一行的模組名再 push。

---

## 🏋️ fitness.py — 健身 LINE Bot

### 三大功能

1. **🎯 目標設定與追蹤**
   - SMART 框架引導（Gemini 給目標建議）
   - 自動拆解：可量化指標 / 期限 / 本週第一步 / 里程碑
   - 每日 / 每週定時推播回顧提醒
2. **🌱 自我成長與習慣**
   - 運動打卡、飲水關卡式追蹤、睡眠回顧
   - 壞習慣紀錄（熬夜、暴食、缺乏運動）
   - 訓練心得 / 週月回顧（Gemini 自動總結）
   - 每日健身知識一則
3. **🥗 飲食與健康**
   - 首次設定 BMR / TDEE / 三大營養素（Mifflin-St Jeor 公式）
   - 目標導向（增肌 / 維持 / 減脂）自動調整熱量與營養素比例
   - 生活型態彈性菜單（外食 vs 自煮、葷素切換）
   - 運動前 / 運動後點心建議
   - 三大營養素比例視覺化 Flex Message

### 定時推播（APScheduler，台北時區）

| 工作 | 時間 | 內容 |
|---|---|---|
| 早安睡眠回顧 | 每日 07:30 | 推 Flex 卡讓使用者回報昨晚品質 |
| 飲水提醒 | 每日 09 / 12 / 15 / 18 | 顯示當天進度 + 打卡按鈕 |
| 久坐伸展 | Mon-Fri 11 / 14 / 16 | 1 分鐘伸展操卡片 |
| 目標回顧 | 每日 21:00 / 每週日 20:00 | 依使用者設定的頻率推目標卡 |

> ⚠️ APScheduler 跑在 Flask 行程內。HF Spaces 免費方案閒置會 sleep，推播可能會漏。教學用足夠，正式上線需要外部 cron。

---

## 環境變數

`fitness.py` 需要 5 個環境變數（HF Spaces 在「Settings → Variables and secrets」設定）：

| 用途 | 變數名 |
|---|---|
| Google Gemini API 金鑰 | `GEMINI_API_KEY` |
| LINE Channel Secret | `LINE_CHANNEL_SECRET` |
| LINE Channel Access Token | `LINE_CHANNEL_ACCESS_TOKEN` |
| Supabase project URL | `SUPABASE_URL` |
| Supabase service role / anon key | `SUPABASE_KEY` |

其他範例只需要前三個（含 OpenAI 的多一個 `OPENAI_API_KEY`、含媒體的多一個 `SPACE_HOST`）。

---

## 部署到 Hugging Face Spaces

### 1. 建 Supabase 專案

1. 到 [supabase.com](https://supabase.com) 開一個 free tier 專案。
2. 「SQL Editor → New query」貼上 `supabase_schema.sql` 全部內容，按 Run。
3. 「Project Settings → API」拿到 `Project URL` 與 `service_role key`（或 anon key，但 Bot 用 service_role 比較不會被 RLS 卡住）。

### 2. 建 LINE Messaging API channel

1. 到 [LINE Developers](https://developers.line.biz/console/) 建立 Messaging API channel。
2. 抓 **Channel secret** 與 **Channel access token**。
3. Webhook URL 暫時留空，等部署完再設。

### 3. 建 Hugging Face Space

1. 到 [huggingface.co/new-space](https://huggingface.co/new-space)，**SDK 選 Docker**。
2. Repo 名稱會變成你的 Space domain。
3. 把整個專案 push 上去：

```powershell
git remote add hf https://huggingface.co/spaces/<你的帳號>/<space名稱>
git push hf main
```

4. 進 Space 的「Settings → Variables and secrets」加上前面五個環境變數。
5. Space build 完後，把 `https://<你的帳號>-<space名稱>.hf.space/` 填回 LINE Console 的 Webhook URL，按「Verify」。

### 4. 加 Bot 為好友 → 開始

掃 LINE Console 顯示的 QR Code，加 bot 為好友，第一句話送出後就會引導完成 TDEE 設定。

---

## 本機開發

```powershell
# 一律用 uv，不用 pip
uv venv
uv pip install -r requirements.txt

# 設環境變數
$env:GEMINI_API_KEY = "..."
$env:LINE_CHANNEL_SECRET = "..."
$env:LINE_CHANNEL_ACCESS_TOKEN = "..."
$env:SUPABASE_URL = "https://xxxx.supabase.co"
$env:SUPABASE_KEY = "..."

# 跑 fitness.py
uv run gunicorn -w 1 -b 0.0.0.0:7860 fitness:app

# 或跑其他範例
uv run flask --app replybot run --port 7860
```

要對接 LINE webhook，請用 ngrok 之類的 tunneling 暴露 `http://localhost:7860/`，再到 LINE Console 填那個外網 URL。

---

## Docker

```powershell
docker build -t sculinebot .
docker run --rm -p 7860:7860 `
  -e GEMINI_API_KEY=$env:GEMINI_API_KEY `
  -e LINE_CHANNEL_SECRET=$env:LINE_CHANNEL_SECRET `
  -e LINE_CHANNEL_ACCESS_TOKEN=$env:LINE_CHANNEL_ACCESS_TOKEN `
  -e SUPABASE_URL=$env:SUPABASE_URL `
  -e SUPABASE_KEY=$env:SUPABASE_KEY `
  sculinebot
```

---

## 重要約定

- **環境變數命名以 `replybot.py` 為準**（`GEMINI_API_KEY` / `LINE_CHANNEL_SECRET` / `LINE_CHANNEL_ACCESS_TOKEN`），不要再用舊版 `GOOGLE_API_KEY` 之類。
- LINE SDK 統一用 `linebot.v3`，禁混用 v2。
- Gemini / GPT 回應的 Markdown 一律經過 `markdown` + `BeautifulSoup` 轉純文字再送 LINE。
- gunicorn 啟動 `fitness.py` 一定要 `-w 1`，否則 APScheduler 會在每個 worker 重複觸發。

詳細的範例分工與架構規範請見 `CLAUDE.md`。
