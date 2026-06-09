# 健身訓練 LINE Bot 計畫

> 2026 上半年 東吳大學資料科學系 LINE Bot 進階課程
> 最後更新：2026-06-02

## 專案目標

打造一支**個人健身教練 LINE Bot**，首次使用時引導使用者完成身體資料與器材設定，
再由 Gemini 根據個人條件產生自然、貼合現實的訓練計畫，並記錄每次訓練、追蹤進度。

---

## 使用流程（User Journey）

```
第一次傳訊息
    │
    ▼
【Step 1】身體資料收集（對話式引導）
  姓名 → 性別 → 年齡 → 身高 → 體重 → 骨骼肌量 → 體脂率 → 訓練目標
    │
    ▼
【Step 2】可用器材收集
  使用者列出家裡／健身房有的器材
  Bot 確認並儲存到資料庫
    │
    ▼
【Step 3】生成初始訓練計畫
  Gemini 依身體數據 + 器材限制 + 目標，產生週訓練計畫
    │
    ▼
【日常使用】
  ┌─────────────────────────────────┐
  │  輸入今天訓練 → 記錄 + 比較上次  │
  │  查詢動作 → 注意事項提示         │
  │  /本週 /PR /進度 /計畫 /reset   │
  └─────────────────────────────────┘
```

---

## 核心功能規劃

### F0. 首次設定流程（Onboarding）

Bot 以對話方式一步步引導收集，不讓使用者一次填一堆欄位。

**收集項目：**

| 欄位 | 說明 | 範例 |
|---|---|---|
| 姓名 | 稱呼用 | 小明 |
| 性別 | 影響基礎代謝計算 | 男／女 |
| 年齡 | 影響恢復建議 | 22 |
| 身高（cm） | 計算 BMI | 175 |
| 體重（kg） | 訓練強度基準 | 70 |
| 骨骼肌量（kg） | 判斷肌肉基底，調整增肌目標 | 32 |
| 體脂率（%） | 減脂目標依據 | 18 |
| 訓練目標 | 增肌／減脂／增強體能／維持 | 增肌 |
| 每週可訓練天數 | 決定訓練頻率 | 4 |
| 程度 | 初學／中階／進階 | 中階 |

**骨骼肌量／體脂率的處理方式：**

使用者不一定知道這兩個數值，Onboarding 時給選項：

```
Bot：你知道自己的骨骼肌量和體脂率嗎？
  1️⃣ 知道 → 直接輸入數字
  2️⃣ 不知道 → 傳一張正面照片，Bot 用 Gemini Vision 估算體態分類
```

**照片估算流程：**
- 使用者傳照片
- Gemini Vision 分析，回覆體態分類 + 估計體脂率範圍：
  ```
  根據照片，你的體態大約落在「體脂偏高」範圍（估計 25~30%）。
  我會以這個基礎調整你的訓練量與計畫。
  若之後有更精確的數據，可以用 /更新資料 隨時修正。

  ⚠️ 此為視覺估算，僅供訓練參考，非醫療數據。
  ```
- 體態分類對應訓練量調整：

  | 視覺分類 | 估計體脂範圍 | 訓練量調整方向 |
  |---|---|---|
  | 偏瘦 | < 12%（男）/ < 18%（女） | 增加熱量盈餘、以增肌為主 |
  | 正常 | 12~20%（男）/ 18~28%（女） | 依目標調整 |
  | 體脂偏高 | 20~28%（男）/ 28~35%（女） | 增加有氧比例、控制熱量 |
  | 肥胖 | > 28%（男）/ > 35%（女） | 低強度入門、優先減脂 |

**觸發器材收集：**
> 「很好！最後，請告訴我你現有的健身器材（例如：啞鈴、彈力帶、槓鈴、健身房器械），沒有任何器材也可以打「徒手」。」

---

### F1. 器材設定

使用者輸入可用器材後，Bot 解析並儲存，後續所有訓練計畫都在這個範圍內規劃。

- **輸入範例**：`「啞鈴、彈力帶、Pull-up bar」`
- **Bot 回覆**：確認清單 + 說明會在這些器材限制內給出最佳方案
- **指令**：`/器材` 查看目前設定；`/器材更新` 重新設定

---

### F2. 個人化訓練計畫

依身體數據 + 器材 + 目標，由 Gemini 生成週計畫，用 LINE 友善的純文字格式輸出。

- **觸發**：Onboarding 完成後自動生成 / `/計畫` 手動觸發
- **輸出格式**：
  ```
  【本週訓練計畫】小明 · 增肌 · 4天
  ─────────────────
  週一 胸＋三頭
    ・啞鈴臥推 4x10
    ・啞鈴飛鳥 3x12
    ・窄距伏地挺身 3x15
  週三 背＋二頭
    ...
  週五 腿
    ...
  週六 肩＋核心
    ...
  ─────────────────
  休息日：週二、四、日
  ```

---

### F3. 訓練紀錄

使用者用自然語言紀錄，Gemini 解析後寫進資料庫。

- **輸入範例**：`「今天做了啞鈴臥推 4x10 20kg，深蹲 4x8 徒手」`
- **Bot 回覆**：確認紀錄 + 與上次同動作比較
  ```
  已記錄！
  ✓ 啞鈴臥推 4x10 20kg（上次 18kg，進步 2kg！）
  ✓ 深蹲 4x8 徒手
  今天共 8 組，繼續加油！
  ```
- **指令**：`/紀錄` 最近 7 天摘要

---

### F4. 動作注意事項提示

使用者詢問動作時，Bot 給出**執行前的重點提醒**，而非完整教學文章。

- **輸入**：`「深蹲怎麼做」`、`「臥推注意事項`」
- **Bot 回覆格式**：
  ```
  【深蹲 執行前提醒】
  • 沉肩、夾背，不要聳肩
  • 膝蓋對準腳尖方向，不要內扣
  • 核心收緊，下背保持自然弧度
  • 呼吸：下蹲吸氣，站起吐氣
  ```
- **傳圖片**：Gemini Vision 分析姿勢，同樣以「注意事項」格式回覆

---

### F5. 進度追蹤

- `/進度 深蹲` — 深蹲歷史重量／次數趨勢
- `/PR` — 各動作個人最佳紀錄
- `/本週` — 本週訓練天數、總組數

---

### F6. 訓練後注意事項 + 飲食補充建議

每次使用者記錄訓練完，Bot **自動附上**收尾提醒，不需另外詢問。

- **觸發**：訓練紀錄寫入資料庫後，緊接著回覆
- **輸出格式**：
  ```
  ─────────────────
  【訓練後注意事項】
  • 緩和運動 5~10 分鐘，避免血液積聚在肌肉
  • 伸展今天使用到的肌群，每個動作維持 20~30 秒
  • 補充水分，訓練中每 15 分鐘建議喝 150~200ml

  【今日飲食補充建議】
  訓練量：中等（8 組，約 45 分鐘）
  • 蛋白質：建議攝取 140g（體重 70kg × 2g）
  • 熱量：今日消耗約 350 kcal，建議熱量盈餘 200~300 kcal
  • 訓練後 30 分鐘內補充蛋白質效果最佳
  ─────────────────
  ```
- **`/飲食`**：可單獨查詢今日建議（若忘記看）

---

## 資料庫設計（Supabase）

```sql
-- 使用者基本資料 + 身體數據
users (
  line_user_id     TEXT PRIMARY KEY,
  name             TEXT,
  gender           TEXT,        -- 'male' | 'female'
  age              INT,
  height_cm        NUMERIC,
  weight_kg        NUMERIC,
  skeletal_muscle_kg NUMERIC,   -- 骨骼肌量
  body_fat_pct     NUMERIC,     -- 體脂率
  goal             TEXT,        -- 'muscle' | 'fat_loss' | 'endurance' | 'maintain'
  level            TEXT,        -- 'beginner' | 'intermediate' | 'advanced'
  days_per_week    INT,
  onboarding_done  BOOLEAN DEFAULT false,
  created_at       TIMESTAMPTZ DEFAULT now()
)

-- 可用器材
equipment (
  id               BIGSERIAL PRIMARY KEY,
  line_user_id     TEXT REFERENCES users(line_user_id),
  name             TEXT,        -- 器材名稱，如 "啞鈴"、"彈力帶"
  created_at       TIMESTAMPTZ DEFAULT now()
)

-- 訓練紀錄（每次訓練一筆）
workouts (
  id               BIGSERIAL PRIMARY KEY,
  line_user_id     TEXT REFERENCES users(line_user_id),
  workout_date     DATE DEFAULT CURRENT_DATE,
  raw_input        TEXT,
  created_at       TIMESTAMPTZ DEFAULT now()
)

-- 單一動作紀錄（一次訓練多筆）
sets (
  id               BIGSERIAL PRIMARY KEY,
  workout_id       BIGINT REFERENCES workouts(id),
  exercise         TEXT,
  sets             INT,
  reps             INT,
  weight_kg        NUMERIC,
  duration_min     INT,         -- 有氧用
  created_at       TIMESTAMPTZ DEFAULT now()
)

-- 對話歷史（per-user AI 多輪）
messages (
  id               BIGSERIAL PRIMARY KEY,
  line_user_id     TEXT,
  role             TEXT,        -- 'user' | 'assistant'
  content          TEXT,
  created_at       TIMESTAMPTZ DEFAULT now()
)
```

---

## 開發階段

### 階段一：Onboarding 流程（最高優先）

- [ ] 新建 `fitness.py`，沿用 `replybot.py` 的 Flask + LINE webhook 結構
- [ ] Supabase 建立 schema（`apply_migration`）
- [ ] 實作對話式 Onboarding：依 `onboarding_done` 判斷是否引導
- [ ] 完成 `users` 資料收集 → `onboarding_done = true`
- [ ] 器材收集 → 寫入 `equipment` 資料表

### 階段二：訓練計畫生成（F2）

- [ ] 設計 system prompt，讓 Gemini 依身體數據 + 器材產生週計畫
- [ ] `/計畫` 指令觸發，回覆 LINE 友善格式

### 階段三：訓練紀錄（F3）

- [ ] Gemini JSON mode 解析自然語言 → 寫入 `workouts` + `sets`
- [ ] 回覆：確認 + 與上次比較

### 階段四：查詢 + 注意事項（F4, F5）

- [ ] `/紀錄`、`/進度`、`/PR`、`/本週`
- [ ] 動作注意事項：Gemini 回覆重點提示格式
- [ ] 圖片：Gemini Vision → 注意事項格式回覆

### 階段五：飲食建議 + 部署（F6）

- [ ] 訓練後自動附上飲食補充建議
- [ ] 更新 `Dockerfile` CMD → `fitness:app`
- [ ] HF Spaces Secrets 加入 `SUPABASE_URL` / `SUPABASE_KEY`
- [ ] 更新 README

---

## 指令對照表

| 使用者輸入 | Bot 行為 |
|---|---|
| 第一次傳任何訊息 | 開始 Onboarding 流程 |
| 一般文字 | 記錄訓練 or 回答問題（Gemini 判斷意圖） |
| 傳圖片 | Gemini Vision → 動作注意事項提示 |
| `/計畫` | 生成本週訓練計畫 |
| `/紀錄` | 最近 7 天訓練摘要 |
| `/進度 深蹲` | 深蹲歷史趨勢 |
| `/PR` | 各動作個人最佳紀錄 |
| `/本週` | 本週統計 |
| `/飲食` | 依今日訓練量給補充建議 |
| `/器材` | 查看目前器材設定 |
| `/器材更新` | 重新設定可用器材 |
| `/更新資料` | 重新輸入身體數據（含照片估算） |
| `/reset` | 清除對話歷史 |

---

## 技術選型

| 項目 | 選擇 | 原因 |
|---|---|---|
| LLM | Gemini 2.5 Flash | 免費額度夠、支援中文、Vision 可用 |
| 資料庫 | Supabase (PostgreSQL) | 已整合 MCP，schema 容易管理 |
| 部署 | Hugging Face Spaces Docker | 與其他範例一致 |
| 圖片分析 | Gemini Vision（沿用 `gemini.py`） | 不需額外套件 |
| 自然語言解析 | Gemini JSON mode | 口語紀錄 → 結構化資料 |

---

## 學習重點

- Gemini JSON mode — 把自然語言轉成結構化訓練資料
- 對話式 Onboarding 狀態機——依 DB 欄位判斷使用者在哪個步驟
- Supabase per-user 資料隔離（解決全域 session 問題）
- System prompt 設計——讓 Bot 同時能「判斷意圖」又能「執行對應動作」
