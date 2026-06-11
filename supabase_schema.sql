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
    notify_sleep boolean default true,    -- 早安睡眠回顧推播
    notify_water boolean default true,    -- 飲水提醒推播
    notify_stretch boolean default true,  -- 久坐伸展推播
    notify_goal boolean default true,     -- 目標回顧推播
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

-- 5. 反思紀錄
create table if not exists reflections (
    id bigserial primary key,
    user_id text not null,
    period text,                     -- 'training' | 'week' | 'month'
    content text,
    ai_summary text,
    recorded_at timestamptz default now()
);
create index if not exists reflections_user_idx on reflections(user_id, recorded_at desc);
