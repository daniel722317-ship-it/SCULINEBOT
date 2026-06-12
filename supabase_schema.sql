-- ============================================================
-- Fitness LINE Bot - Supabase schema
-- 在 Supabase Dashboard → SQL Editor 貼上執行一次即可
-- ============================================================

-- 1. 個人資料 + TDEE/BMR + 飲食偏好
create table if not exists profiles (
    user_id text primary key,
    display_name text,
    gender text,                     -- 'male' | 'female'
    age int,
    height_cm numeric,
    weight_kg numeric,
    activity_level text,             -- 'sedentary' | 'light' | 'moderate' | 'active' | 'very_active'
    bmr numeric,
    tdee numeric,
    target_type text,                -- 'bulk' | 'maintain' | 'cut'
    target_kcal numeric,
    protein_g numeric,
    carb_g numeric,
    fat_g numeric,
    is_vegetarian boolean default false,
    eating_style text,               -- 'outside' | 'home'
    workout_time text,               -- 'morning' | 'afternoon' | 'evening'
    daily_water_ml int,
    -- 簡易模式（預設開）：兩則合併推播
    notify_morning boolean default true,    -- 每日早報 09:00
    notify_evening boolean default true,    -- 晚安回顧 21:00
    -- 進階模式（預設關，使用者手動開）：原本的分時段推播
    notify_sleep boolean default false,     -- 早安睡眠回顧 07:30
    notify_water boolean default false,     -- 飲水提醒 9/12/15/18
    notify_stretch boolean default false,   -- 久坐伸展 週一-五 11/14/16
    notify_goal boolean default false,      -- 目標回顧 21:30 / 週日 20:00
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

-- 2. 對話狀態（精靈式流程的暫存）
create table if not exists conversation_state (
    user_id text primary key,
    flow text,                       -- 'profile_setup' | 'goal_setting' | 'reflection' | ...
    step text,
    data jsonb default '{}'::jsonb,
    updated_at timestamptz default now()
);

-- 3. 目標
create table if not exists goals (
    id bigserial primary key,
    user_id text not null,
    description text,
    smart_specific text,
    smart_measurable text,
    deadline date,
    first_step text,
    milestones text,
    review_freq text default 'weekly',  -- 'daily' | 'weekly'
    status text default 'active',       -- 'active' | 'done' | 'abandoned'
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);
create index if not exists goals_user_status_idx on goals(user_id, status);

-- 4. 習慣 / 壞習慣紀錄
create table if not exists habit_logs (
    id bigserial primary key,
    user_id text not null,
    type text not null,              -- 'workout' | 'water' | 'sleep' | 'stretch'
                                     -- | 'late_night' | 'binge' | 'no_exercise'
    amount numeric,                  -- 飲水 ml / 睡眠 hours / 運動 minutes
    quality text,                    -- 'good' | 'normal' | 'bad'
    note text,
    recorded_at timestamptz default now()
);
create index if not exists habit_logs_user_type_idx on habit_logs(user_id, type, recorded_at desc);

-- 5. 力量追蹤紀錄
create table if not exists strength_logs (
    id bigserial primary key,
    user_id text not null,
    exercise text not null,          -- 'squat' | 'bench' | 'deadlift' | 'ohp'
    weight_kg numeric not null,
    reps int not null,
    sets int default 1,              -- 組數
    one_rm numeric not null,         -- Brzycki 公式估算
    recorded_at timestamptz default now()
);
create index if not exists strength_logs_user_ex_idx
    on strength_logs(user_id, exercise, recorded_at desc);


-- 6. 自訂訓練菜單（使用者建立）
create table if not exists custom_workouts (
    id bigserial primary key,
    user_id text not null,
    name text not null,
    items jsonb not null,            -- strength: [{exercise, sets, reps}, ...]
                                     -- cardio:   [{exercise, duration}, ...]
    category text not null default 'strength',  -- 'strength' | 'cardio'
    created_at timestamptz default now()
);
create index if not exists custom_workouts_user_idx
    on custom_workouts(user_id, category, created_at desc);


-- 7. 反思紀錄
create table if not exists reflections (
    id bigserial primary key,
    user_id text not null,
    period text,                     -- 'training' | 'week' | 'month'
    content text,
    ai_summary text,
    recorded_at timestamptz default now()
);
create index if not exists reflections_user_idx on reflections(user_id, recorded_at desc);
