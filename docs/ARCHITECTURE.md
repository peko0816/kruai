# 架构说明

本文件定义模块边界与数据流。**PRD 说"做什么"，本文件说"怎么切"。** 建表以 `DATA_MODEL.sql` 为准。

---

## 1. 分层

```
┌──────────────────────────────────────────────────────────┐
│  入口层   bot/ (Telegram)        miniapp/ (React, M3)     │
│           —— 只做展示与采集，不做任何业务判断              │
└────────────────────────┬─────────────────────────────────┘
                         │ HTTP / WS
┌────────────────────────▼─────────────────────────────────┐
│  API 层   backend/app/api/v1/                             │
│           —— 鉴权、参数校验、编排 service，不写业务逻辑    │
└────────────────────────┬─────────────────────────────────┘
┌────────────────────────▼─────────────────────────────────┐
│  领域层   services/mastery   services/media               │
│           services/entitlements   services/experiments    │
│           —— 纯逻辑，不碰 IO，可完全单测                   │
└────────────────────────┬─────────────────────────────────┘
┌────────────────────────▼─────────────────────────────────┐
│  适配层   services/{scoring,tts,payments,llm}/            │
│           每个目录: base.py(抽象) fake.py registry.py      │
│           + 各厂商实现（M0 后新增）                        │
└────────────────────────┬─────────────────────────────────┘
┌────────────────────────▼─────────────────────────────────┐
│  基础设施 models/ (SQLAlchemy)  workers/ (RQ)  core/      │
└──────────────────────────────────────────────────────────┘

   pipeline/  —— 独立 CLI，离线运行，不属于请求链路
```

**两条不可越界的规则：**

1. **领域层不得 import 适配层的具体实现**，只能 import `base.py` 的抽象类型
2. **入口层不得包含业务判断**。媒体选 audio 还是 video、额度够不够、这一轮算不算过——全部服务端决定，客户端只执行

---

## 2. 核心数据流

### 2.1 一次开口（attempt）

```
Bot 收到 voice note
  → API: POST /api/v1/attempts  (audio + lesson_item_id)
  → entitlements.check(user, "attempt")        # 额度校验，不够直接 402
  → scoring.registry.get(language).assess(...)  # 走抽象，不知道是谁
  → cost_ledger.record(result.cost_usd_cents)   # 强制，无例外
  → attempts 落库（含 raw → phoneme_detail）
  → mastery.apply(user, concept, result)        # 纯逻辑，可单测
  → review_queue.schedule(user, concept)        # SM-2
  → 返回评分卡 + 高棉语纠错说明
```

**关键点**：`scoring` 之后的每一步都不知道分数从哪来。M0 换供应商不影响这条链路。

### 2.2 讲解卡的媒体选择（PRD 7.3）

```
GET /api/v1/lessons/{id}
  → media.resolve(lesson_item, user)
        输入: item.media_type, user.plan, user.data_saver,
              experiments.variant(user, "explain_media")
        输出: 一个 URL + 一个 fallback_url
  → 响应里 video_url 与 audio_url 同时返回
     客户端优先播 video，失败/超时/弱网 → 立即切 audio
```

**媒体选择逻辑只在服务端**。客户端只有一个动作：播主 URL，失败切备用 URL。

### 2.3 内容管线（离线，与请求链路无交集）

```
seed/*.yaml
  → generate.py   llm.batch_complete(purpose=OFFLINE_GENERATION)
  → validate.py   8 条规则，失败回流
  → review_export.py  10% 抽样 → CSV → 母语者
  → build_pack.py  固化 JSON + tts.synthesize 预生成音频
                   --with-video 时对 anchor=true 的 item 生成视频
  → import_pack.py 入库 + 上传对象存储 + 校验 sources/licence
```

管线**不 import backend 的任何 service**，只共享 `models/` 的 schema 定义。

---

## 3. Provider 抽象契约

四个适配目录结构完全一致：

```
services/scoring/
├── base.py        # 规范性，来自 interfaces/scoring_base.py，不得改
├── fake.py        # 确定性 Fake，默认启用
├── registry.py    # 按配置选实现 + 按语言路由
├── azure.py       # M0-1 后新增
└── __init__.py    # 只导出 registry 与 base 的类型
```

### 3.1 registry 的职责

```python
def get_scorer(language: Language) -> PronunciationScorer:
    """按 SCORING_PROVIDER 配置选实现；若该实现不 supports(language)，
    回退到 fallback 链；全部不支持则抛配置错误（启动期就该发现）。"""
```

registry 在**应用启动时**做一次能力自检：所有配置的 provider 能否覆盖所需语言。不通过直接启动失败，不要等到运行时。

### 3.2 Fake 实现的要求

不是"随便返回点东西"，而是**确定性可测的替身**：

| Provider | Fake 行为 |
|---|---|
| `FakeScorer` | 按 `hash(reference_text + len(audio))` 生成稳定分数；`reference_text` 含 `"__BAD__"` 时返回低分，用于测试失败分支 |
| `FakeTTS` | 生成指定时长的静音 mp3，`resolved_voice="fake"` |
| `FakePaymentProvider` | `create_checkout` 返回 `checkout_url="fake://pay/{order_id}"`；`verify_callback` 用固定 HMAC key 验签 |
| `FakeLLM` | 按 `json_schema` 生成结构合法的最小占位内容；无 schema 时回显最后一条 user message |

**验收标准：全部 provider 设为 fake 时，系统端到端跑通，全部测试通过。**

---

## 3.3 支付渠道可移植性（设计目标）

**换收单机构不应波及任何业务逻辑。** ABA PayWay 只是"接入 KHQR 的一个通道"。

依据：KHQR 是柬埔寨国家银行的统一商户 QR 标准，经 Bakong 与所有参与银行和主要
钱包互通。换通道时用户扫码体验不变，变的只是结算方。

换一个渠道的完整改动面：

| 要改 | 量 |
|---|---|
| 新增 adapter，实现 `PaymentProvider` 的 3 个抽象方法 | 1 个文件 |
| registry 注册 | 1 行 |
| `PAYMENT_PROVIDERS` + 密钥 | 配置 |

**不动**：`payments` 表（`order_id` 是我方的，`provider` 是 text 列）、订阅开通逻辑、
entitlements、API 路由、Bot、前端。这依赖 `verify_callback` 被规定为**无副作用**。

三处 adapter 层吸收不了、必须由业务层处理的差异，都已在设计中容纳：

| 差异 | 如何容纳 |
|---|---|
| 是否支持代扣 | `supports_recurring` + `subscriptions.renewal_mode`，auto / manual 双路径（见 3.4） |
| 币种 | `Money(amount_minor, currency, currency_minor_units)`；KHR 无小数位 |
| 收款形态 | `CheckoutResult` 同时支持 `checkout_url`（跳转）与 `qr_payload`（KHQR） |

若将来出现第四种形态（SDK 内嵌、需先建 customer 对象），扩 `CheckoutResult`，
不要在业务层加分支。

## 3.4 订阅续费双路径

由 `provider.supports_recurring` 决定，**业务层不得假设任一种**。

```
支付成功
  ├── provider.supports_recurring 且拿到 mandate_ref
  │     → renewal_mode='auto'，写 next_charge_at = period_end
  │     → 定时任务扫 next_charge_at 到期 → charge_recurring
  │         成功 → 顺延 period；失败 → 按 RETRY_DAYS 重试，全败转 grace
  └── 否则
        → renewal_mode='manual'
        → 定时任务扫 period_end - REMINDER_DAYS_BEFORE → 推送续费提醒
            （reminder_sent_at 防重复）
        → 用户一键再买一次，走普通 checkout

到期后统一进入 grace（SUBSCRIPTION_GRACE_DAYS），期间权益仍有效；
grace 结束仍未续 → expired，降级到 free 的限额。
```

两条路径共用 `subscriptions` 一张表，共用同一套权益判定。**不得为 auto 和 manual
各写一套订阅逻辑。**

## 4. 配置驱动的边界

以下决策**运行时由配置决定**，代码里不得出现分支硬编码：

| 决策 | 配置项 |
|---|---|
| 用哪个评分 provider | `SCORING_PROVIDER` |
| 及格阈值 | `SCORING_PASS_THRESHOLD` |
| mastery 各 item 权重 | `MASTERY_WEIGHT_*` |
| SM-2 参数 | `SM2_*` |
| 各档限额 | `LIMIT_*` |
| 续费模式与宽限期 | `SUBSCRIPTION_*` |
| 支持币种 | `SUPPORTED_CURRENCIES` |
| 实时会话上限 | `REALTIME_SESSION_MAX_SECONDS` |
| 视频生成开关 | `PIPELINE_WITH_VIDEO`（默认 false） |

完整清单见 `CONFIG_REFERENCE.md`。

---

## 5. 错误与降级

| 场景 | 行为 |
|---|---|
| scoring provider 超时/失败 | 返回 `ok=False`，前端提示"稍后重试"，**不扣额度**，不写 attempts |
| TTS 在管线中失败 | 该 item 标记 `media_pending`，pack 仍可构建，运行时降级为纯文本 + 目标句展示 |
| 视频加载失败 | 客户端切 audio（PRD 7.3，强制） |
| 支付回调验签失败 | 拒绝开通，记录告警，**不得**先放行 |
| LLM 运行时失败 | Q&A 回合降级为"仅评分 + 标准示范"，不阻断学习流程 |

**原则：任何外部依赖失败都不能让用户卡在流程中间。**

---

## 6. 目录结构（建仓时按此创建）

```
kruai/
├── CLAUDE.md
├── Makefile                      # check / test / lint / fmt / migrate / run
├── docker-compose.yml
├── .env.example
├── docs/                         # 本 bootstrap 包的 docs/ 全部放这里
├── backend/
│   ├── pyproject.toml
│   ├── alembic/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/v1/
│   │   ├── core/                 # config, security, logging, errors
│   │   ├── models/
│   │   ├── services/
│   │   │   ├── scoring/ tts/ payments/ llm/      # 适配层
│   │   │   ├── mastery/ media/ entitlements/ experiments/  # 领域层
│   │   │   └── reports/
│   │   └── workers/
│   └── tests/
│       ├── unit/ integration/ fixtures/
├── bot/
├── miniapp/                      # M3 才建
└── pipeline/
    ├── seed/{zh-hsk3.0,zh-hskk,zh-bct,en-gse}/
    ├── generate.py validate.py review_export.py build_pack.py import_pack.py
    ├── video/                    # S2 才实现，先留空目录 + README
    └── packs/
```
