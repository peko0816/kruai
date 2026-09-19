-- KruAI 权威数据模型 (PostgreSQL 16)
-- 建表以本文件为准；SQLAlchemy 模型必须与之一致。
-- 变更本文件必须同时提交对应的 alembic migration。
--
-- 约定：
--   · 用户支付金额：INTEGER，单位为该币种最小货币单位，列名 amount_minor
--     并伴随 currency + currency_minor_units（USD=2, KHR=0）。禁止用 "cents" 语义，
--     因为柬埔寨双币种：USD 有 2 位小数，KHR 有 0 位小数。
--   · 内部成本核算：一律 USD，列名以 _usd_cents 结尾，语义明确无需元数据
--   · 时长一律 INTEGER，单位在列名里（_ms / _seconds）
--   · 时间一律 TIMESTAMPTZ，存 UTC
--   · 软删除用 deleted_at，不用布尔标记

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================ 用户

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id     BIGINT UNIQUE NOT NULL,
    phone           TEXT,
    locale          TEXT NOT NULL DEFAULT 'km',      -- km / zh / en
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at  TIMESTAMPTZ,
    deleted_at      TIMESTAMPTZ
);

CREATE TABLE user_profiles (
    user_id             UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    target_language     TEXT NOT NULL DEFAULT 'zh',  -- zh / en
    current_level       TEXT NOT NULL DEFAULT 'HSK1',
    daily_goal_minutes  INTEGER NOT NULL DEFAULT 10,
    timezone            TEXT NOT NULL DEFAULT 'Asia/Phnom_Penh',
    data_saver          BOOLEAN NOT NULL DEFAULT false   -- 省流量：强制音频
);

-- ============================================================ 内容

CREATE TABLE courses (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    course_type  TEXT NOT NULL CHECK (course_type IN ('exam','job')),
    language     TEXT NOT NULL,
    level        TEXT NOT NULL,
    title_km     TEXT NOT NULL,
    title_zh     TEXT,
    org_id       UUID,                                -- job 类型时归属企业
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_courses_lookup ON courses(language, course_type, level);

CREATE TABLE concepts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug              TEXT UNIQUE NOT NULL,           -- e.g. zh.hsk1.want_noun
    language          TEXT NOT NULL,
    level             TEXT NOT NULL,
    pattern           TEXT NOT NULL,                  -- "我要 + [名词]"
    km_explanation    TEXT NOT NULL,
    common_l1_errors  JSONB NOT NULL DEFAULT '[]'::jsonb,
    hskk_task_types   TEXT[] NOT NULL DEFAULT '{}',   -- 对齐 HSKK 口语任务类型
    sort_order        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_concepts_level ON concepts(language, level, sort_order);

CREATE TABLE lessons (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id    UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    concept_ids  UUID[] NOT NULL,
    sequence     INTEGER NOT NULL,
    title_km     TEXT NOT NULL,
    UNIQUE (course_id, sequence)
);

CREATE TABLE lesson_items (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lesson_id   UUID NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    concept_id  UUID REFERENCES concepts(id),
    item_type   TEXT NOT NULL CHECK (item_type IN ('explain','drill','vocab','qa')),
    payload     JSONB NOT NULL,
    media_type  TEXT NOT NULL DEFAULT 'audio' CHECK (media_type IN ('audio','video')),
    anchor      BOOLEAN NOT NULL DEFAULT false,       -- true 才参与 S2 视频生成
    sequence    INTEGER NOT NULL,
    UNIQUE (lesson_id, sequence)
);

-- 同一 item 可同时挂 audio 与 video 两行；运行时按 plan/网络/实验分组选择
CREATE TABLE media_assets (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lesson_item_id    UUID NOT NULL REFERENCES lesson_items(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL CHECK (kind IN ('audio','video')),
    url               TEXT NOT NULL,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    bytes             INTEGER NOT NULL DEFAULT 0,
    provider          TEXT NOT NULL,
    source_text_hash  TEXT NOT NULL,                  -- 文本变更检测，变更则媒体过期
    generated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lesson_item_id, kind)
);

CREATE TABLE content_packs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version       TEXT NOT NULL,
    language      TEXT NOT NULL,
    level         TEXT NOT NULL,
    checksum      TEXT NOT NULL,
    sources       JSONB NOT NULL,                     -- 非空校验，见 PRD 5.5
    licence       TEXT NOT NULL,                      -- 非空校验
    published_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (language, level, version),
    CONSTRAINT sources_not_empty CHECK (jsonb_array_length(sources) > 0),
    CONSTRAINT licence_not_blank CHECK (length(trim(licence)) > 0)
);

-- ============================================================ 学习

CREATE TABLE attempts (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lesson_item_id       UUID NOT NULL REFERENCES lesson_items(id),
    concept_id           UUID REFERENCES concepts(id),
    audio_url            TEXT,
    asr_text             TEXT,
    pron_score           REAL,
    accuracy_score       REAL,
    fluency_score        REAL,
    completeness_score   REAL,                        -- 仅 scripted
    prosody_score        REAL,                        -- 仅 en-US
    tone_score           REAL,                        -- 仅 zh-CN
    phoneme_detail       JSONB,                       -- provider raw，不参与业务判断
    passed               BOOLEAN NOT NULL,
    provider             TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_attempts_user_time ON attempts(user_id, created_at DESC);
CREATE INDEX idx_attempts_concept ON attempts(user_id, concept_id);

CREATE TABLE concept_mastery (
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    concept_id     UUID NOT NULL REFERENCES concepts(id),
    mastery_score  REAL NOT NULL DEFAULT 0,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    ease_factor    REAL NOT NULL DEFAULT 2.5,
    interval_days  INTEGER NOT NULL DEFAULT 1,
    last_seen_at   TIMESTAMPTZ,
    next_due_at    TIMESTAMPTZ,
    PRIMARY KEY (user_id, concept_id)
);
CREATE INDEX idx_mastery_due ON concept_mastery(user_id, next_due_at, mastery_score);

CREATE TABLE lesson_progress (
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lesson_id     UUID NOT NULL REFERENCES lessons(id),
    status        TEXT NOT NULL CHECK (status IN ('started','completed')),
    completed_at  TIMESTAMPTZ,
    PRIMARY KEY (user_id, lesson_id)
);

CREATE TABLE streaks (
    user_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    current_streak  INTEGER NOT NULL DEFAULT 0,
    longest_streak  INTEGER NOT NULL DEFAULT 0,
    last_day        DATE
);

CREATE TABLE experiments (
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    experiment_key  TEXT NOT NULL,
    variant         TEXT NOT NULL,
    assigned_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, experiment_key)
);

-- ============================================================ 商业

-- 两种续费路径都要支持，由 provider 能力决定走哪条（见 services/payments/base.py）
--   auto   : 渠道支持代扣 → mandate_ref 非空，到 next_charge_at 时发起扣款
--   manual : 渠道不支持代扣 → 到期前 SUBSCRIPTION_REMINDER_DAYS_BEFORE 天推送提醒
CREATE TABLE subscriptions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan              TEXT NOT NULL CHECK (plan IN ('free','basic','pro')),
    status            TEXT NOT NULL CHECK (status IN ('active','grace','expired','cancelled')),
    renewal_mode      TEXT NOT NULL DEFAULT 'manual'
                        CHECK (renewal_mode IN ('auto','manual')),
    period_start      TIMESTAMPTZ NOT NULL,
    period_end        TIMESTAMPTZ NOT NULL,
    grace_until       TIMESTAMPTZ,                     -- status='grace' 时有效
    payment_provider  TEXT,
    external_ref      TEXT,
    mandate_ref       TEXT,                            -- renewal_mode='auto' 时必须非空
    next_charge_at    TIMESTAMPTZ,                     -- renewal_mode='auto'
    reminder_sent_at  TIMESTAMPTZ,                     -- renewal_mode='manual'，防重复推送
    cancelled_at      TIMESTAMPTZ,
    CONSTRAINT auto_needs_mandate CHECK (
        renewal_mode <> 'auto' OR mandate_ref IS NOT NULL
    )
);
CREATE INDEX idx_sub_user_active ON subscriptions(user_id, status, period_end DESC);
-- 续费定时任务的驱动索引：auto 看 next_charge_at，manual 看 period_end
CREATE INDEX idx_sub_due_auto ON subscriptions(next_charge_at)
    WHERE renewal_mode = 'auto' AND status = 'active';
CREATE INDEX idx_sub_due_manual ON subscriptions(period_end)
    WHERE renewal_mode = 'manual' AND status = 'active';

CREATE TABLE entitlements (
    user_id                    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    realtime_seconds_remaining INTEGER NOT NULL DEFAULT 0,
    daily_attempts_used        INTEGER NOT NULL DEFAULT 0,
    daily_tasks_used           INTEGER NOT NULL DEFAULT 0,
    reset_at                   TIMESTAMPTZ NOT NULL,
    CONSTRAINT realtime_non_negative CHECK (realtime_seconds_remaining >= 0)
);

CREATE TABLE payments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id),
    order_id      TEXT UNIQUE NOT NULL,               -- 我方订单号，幂等键
    provider              TEXT NOT NULL,
    provider_ref          TEXT,
    amount_minor          INTEGER NOT NULL,            -- 该币种最小货币单位
    currency              TEXT NOT NULL DEFAULT 'USD',
    currency_minor_units  SMALLINT NOT NULL DEFAULT 2, -- USD=2, KHR=0
    is_recurring_charge   BOOLEAN NOT NULL DEFAULT false,
    status                TEXT NOT NULL,
    raw_payload   JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE realtime_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    seconds_billed      INTEGER NOT NULL DEFAULT 0,
    cost_usd_cents_est  INTEGER NOT NULL DEFAULT 0,
    transcript_url  TEXT
);
CREATE INDEX idx_rt_user_time ON realtime_sessions(user_id, started_at DESC);

-- 每一次外部 API 调用都必须在此留痕（CLAUDE.md 第 8 节）
CREATE TABLE cost_ledger (
    id              BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    provider        TEXT NOT NULL,
    unit            TEXT NOT NULL,                    -- seconds / tokens / calls / minutes
    quantity        REAL NOT NULL,
    cost_usd_cents_est  INTEGER NOT NULL,
    ref             TEXT                              -- 'attempt' / 'realtime' / 'content_production' / 'payment'
);
CREATE INDEX idx_cost_time ON cost_ledger(occurred_at DESC);
CREATE INDEX idx_cost_user_month ON cost_ledger(user_id, occurred_at);

-- ============================================================ B 端

CREATE TABLE orgs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    contact     TEXT,
    plan        TEXT NOT NULL DEFAULT 'standard',
    seats       INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE org_members (
    org_id       UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_role     TEXT,
    enrolled_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
);

CREATE TABLE org_reports (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    period        TEXT NOT NULL,                      -- 'YYYY-MM'
    metrics       JSONB NOT NULL,
    pdf_url       TEXT,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, period)
);

ALTER TABLE courses ADD CONSTRAINT fk_courses_org
    FOREIGN KEY (org_id) REFERENCES orgs(id) ON DELETE CASCADE;
