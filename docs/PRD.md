# KruAI 产品需求设计说明书 v1.1

> **面向高棉语母语者的中文 / 英文口语陪练系统**  
> 文档用途：交付 Claude Code 直接编码实现  
> 日期：2026-09-15 ｜ 状态：待实施

**v1.1 相对 v1.0 的变更**

| # | 变更 | 影响 |
|---|---|---|
| 1 | 新增第 5 节「课程标准与内容来源」，纳入 HSKK / BCT / GSE / 柬埔寨 MoEYS OER / Tatoeba，并明确版权边界 | 内容管线 seed 来源扩大；新增合规校验 |
| 2 | **补入 HSKK 口语考试大纲** —— v1.0 只对齐笔试大纲，是结构性缺陷 | `qa` item 的任务形式与判定标准有了权威来源 |
| 3 | 新增第 7 节「AI 数字人演进路线」，数字人从营销资产升格为**课程交付形态的目标状态**，分 S1–S4 四阶段，每阶段有硬性进入条件 | 数据模型增加媒体双形态；客户端必须实现音频回退 |
| 4 | 第 3.1 节讲解层由「纯音频」改为「音频为默认、视频为可升级形态」 | 内容管线增加 `--with-video` 开关 |
| 5 | 章节重新编号（原 6–14 顺延为 8–16） | 交叉引用已同步更新 |

---

## 0. 给实施方（Claude Code）的阅读说明

本文档是**实现规格**，不是构想文档。以下约定具有强制力：

| 约定 | 说明 |
|---|---|
| 范围边界 | 第 14 节「非目标」列出的内容**不得实现**，即使看起来顺手 |
| 里程碑 | 严格按 M0→M4 顺序交付，每个里程碑有验收标准，未通过不进入下一阶段 |
| 内容与代码分离 | 课程内容是数据（JSON content pack），不是代码。运行时**不得**调用 LLM 生成课程内容 |
| 成本红线 | 第 11 节的单位成本上限是硬约束，任何实现不得突破 |
| 版权红线 | 第 5.5 节的内容来源白名单是硬约束，白名单外的教材不得以任何形式进入管线 |
| 语言 | 代码标识符、注释、commit 全用英文；用户可见文案走 i18n 文件（km/zh/en） |

---

## 1. 产品定义

### 1.1 一句话

**KruAI 是一个以高棉语为教学语言的口语陪练系统，用同一套引擎服务两个入口：C 端 HSK 备考者，B 端中资雇主的岗位中文培训。**

（Kru = 高棉语「老师」的音译）

### 1.2 存在理由

| 事实 | 来源 |
|---|---|
| Duolingo 无以高棉语为教学语言的课程 | 公开产品事实 |
| 应用市场中 Khmer 相关应用绝大多数是「教外国人学高棉语」 | App Store / Google Play 检索 |
| 柬埔寨中文需求由中资就业驱动，有明确薪资溢价 | VnExpress / Khmer Times 报道 |
| HSK / HSKK 大纲（词表、语法点、音节表、口语题型）完全公开 | 教育部《国际中文教育中文水平等级标准》、汉考国际 |

**壁垒不在模型，在 Khmer-first 的教学语言层与沉淀的错误图谱。**

### 1.3 V1 语言范围

**V1 只做中文（HSK 1–3）。英文（CEFR / GSE A1–B1）复用同一套 schema，v2 接入。**

理由：中文赛道在柬埔寨无以高棉语授课的成熟竞品；英文赛道有 ELSA / Speak / Duolingo 且免费替代品强。架构必须语言无关（`language` 字段贯穿全表），但 V1 不实现英文内容包。

---

## 2. 双入口架构（同一引擎）

```
                   ┌─────────────────────────┐
                   │   Core Learning Engine   │
                   │  concept / attempt /     │
                   │  mastery / review / ASR  │
                   └───────────┬─────────────┘
                               │
              ┌────────────────┴────────────────┐
              │                                 │
    ┌─────────▼──────────┐          ┌───────────▼───────────┐
    │  Entry B (C 端)     │          │  Entry A (B 端)        │
    │  HSK 备考陪练       │          │  岗位中文培训          │
    │  Telegram Bot/MiniApp│         │  同引擎 + 岗位课包     │
    │  个人订阅           │          │  + 企业后台 + 月度报告 │
    └────────────────────┘          └───────────────────────┘
```

**关键设计约束：两个入口共用 `courses` 表，仅 `course_type` 不同（`exam` / `job`）。不得为 B 端另建一套学习逻辑。**

---

## 3. 学习闭环设计（对标 Speak）

### 3.1 Speak 的四段式结构与我们的映射

| Speak 原结构 | 我们的实现 | 说明 |
|---|---|---|
| Video Lesson（真人双语讲师视频 2–4 分钟） | **讲解卡，双形态**：默认音频（高棉语 TTS 讲解 + 中文示范），可升级为 AI 数字人视频 | 见第 7 节。v1 阶段全部用音频；视频按 S1–S3 阶段逐步替换，**架构现在就支持两种形态共存** |
| Speaking Drill（跟读→逐步遮词） | 同结构，**Azure 发音评测**打分 | 直接照搬，是留存核心 |
| Vocab Builder（句型替换） | 同结构，替换项**从 HSK 词表自动生成** | 大纲公开，可程序化生成 |
| AI Q&A（对话应用） | Basic 档=回合制对话任务；Pro 档=实时角色扮演。**任务形式与判分维度对齐 HSKK** | 见第 4 节与 5.1 |
| Smart Review（concept 掌握度 + 弱项优先） | 同结构，SM-2 变体 | 直接照搬 |

### 3.2 Concept 模型（整个系统的中枢）

**系统追踪的是 concept（语法点 / 句型），不是「课」。**

```
concept 示例：
  id: zh.hsk1.want_noun
  pattern: "我要 + [名词]"
  level: HSK1
  km_explanation: "<高棉语解释>"
  example_sentences: [...]
  common_l1_errors: [...]   # 高棉语母语者的典型错误，人工+数据积累
  hskk_task_types: [...]    # 对应的 HSKK 口语任务类型
```

每次开口（attempt）都会更新所涉 concept 的 `mastery_score`。复习队列按 mastery 升序 + 间隔重复到期时间排序。

**这是与「Coze 型 AI 外教」的根本分野：有状态、有进度、可判定。**

### 3.3 一节课的运行时流程

```
lesson_start
  → [1] 讲解卡（预生成媒体，0 API 调用）
        media_type = video 且网络允许 → 播视频
        否则 → 回退音频（回退是强制实现项，见 7.3）
  → [2] Drill: 逐句跟读
        录音 → Azure PronunciationAssessment(scripted, 参考文本已知)
        → 分数落 attempts 表 → 未达阈值则重试（最多 3 次）
  → [3] Vocab Builder: 句型替换，同上评分
  → [4] Q&A / Roleplay（任务形式对齐 HSKK）
        Basic: 回合制（见 4.1）
        Pro:   实时语音（见 4.2）
  → lesson_complete → 更新 concept_mastery → 写入 review_queue
```

---

## 4. 双档套餐设计

### 4.1 Basic 档：回合制

**载体**：Telegram Bot 原生消息（voice note 往返）

```
Bot 发出任务提示（文本 + 高棉语 TTS 音频）
  → 用户发 voice note
  → 后端: Azure STT + PronunciationAssessment(unscripted)
  → 后端: LLM 生成回应与纠错（文本，gpt-4.1-mini 级别）
  → Bot 回：评分卡 + 高棉语纠错说明 + 正确示范 TTS
```

关键：**参考文本已知时一律用 scripted 模式**（准确率显著更高）；只有自由回答用 unscripted。

### 4.2 Pro 档：实时语音

**载体**：Telegram Mini App（WebView + WebRTC）
**链路**：`Mini App ──WebRTC/WS──> 后端代理 ──> OpenAI Realtime (gpt-realtime-mini)`

**后端必须做代理，不得让前端直连 OpenAI**：
1. 计量（分钟数扣减）必须服务端权威
2. API key 不得下发
3. 需要在会话中注入 concept 上下文与系统提示（并保持提示稳定以命中 prompt cache）

**硬约束**：
- 每次会话开始前校验剩余分钟；余额不足直接拒绝
- 会话硬上限 15 分钟/次，超时服务端主动断开
- 系统提示必须**逐字稳定**（命中缓存音频输入价格降约 99%）
- 上下文超过 8 轮时做摘要压缩，防止 token 线性增长

### 4.3 套餐与定价

| | Free | Basic | Pro |
|---|---|---|---|
| 每日任务 | 3 个 | 不限 | 不限 |
| 回合制语音评分 | 每日 10 句 | 不限 | 不限 |
| 实时语音对话 | — | — | 60 分钟/月 |
| 数字人讲解视频 | — | S3 起开放 | S2 起开放 |
| Smart Review | 基础 | 完整 | 完整 + 自适应 |
| 学习报告 | — | 周报 | 周报 + 弱项专项 |
| 价格 | $0 | **$1.99/月 或 $18/年** | **$5.99/月 或 $54/年** |

定价锚点：Speak 全球 Premium ≈ $6.99/月、Premium Plus ≈ $13.74/月。柬埔寨人均 GDP 约为其目标市场的零头，必须本地化定价，年付折扣设深（约 25%）以改善现金流与留存。

**超量包**：实时语音 +30 分钟 = $1.99（一次性）。

---

## 5. 课程标准与内容来源

**原则：大纲与标准可以直接采用，出版社教材一律不可用（含改写）。** 本节的白名单是硬约束。

### 5.1 中文侧

| 来源 | 提供什么 | 用在哪一层 |
|---|---|---|
| 《国际中文教育中文水平等级标准》 | 三等九级；音节表 / 汉字表 / 词汇表 / 语法点 | concept seed 的主骨架 |
| HSK 考试大纲 | 题型、各级词表边界 | `validate.py` 的词表越界规则 |
| **HSKK 口语考试大纲** | **口语任务类型与评分维度** | **`qa` item 的任务形式与判定标准 —— mastery 判分的权威依据** |
| BCT 商务汉语考试大纲 | 商务与职场场景清单 | B 端岗位课包 seed |
| YCT 青少年中文 | 低龄分级 | 预留，V1 不实现 |

**HSKK 是 v1.0 的缺口**：做口语产品却只对齐笔试大纲，等于没有「这一轮算不算过了」的外部依据。实现时 `concepts.hskk_task_types` 必须填充。

### 5.2 英文侧（v2 用，seed 现在即可积累）

| 来源 | 提供什么 |
|---|---|
| **GSE（Global Scale of English）Teacher Toolkit** | 约 4000 条可检索的 can-do 学习目标，按技能与等级切分；粒度远细于 CEFR 描述，可直接映射为 concept |
| CEFR Companion Volume | 等级框架层 |
| Oxford 3000 / 5000 | 公开词表，用于词表越界校验 |
| IELTS 公开评分细则 | 口语判分维度 |

### 5.3 柬埔寨本地

| 来源 | 提供什么 | 额外价值 |
|---|---|---|
| **`oer.moeys.gov.kh`**（柬埔寨教育部开放教育资源，**CC-BY 3.0**） | 1–12 年级教案、练习、音视频，含英语科目 | **对齐国家课程 = 进入学校渠道的凭证**，本地说服力远高于自编课程 |
| MoEYS 课程框架文件 | 学段与等级对齐 | 与 GSE / CEFR 做映射表 |

### 5.4 语料

| 来源 | 提供什么 |
|---|---|
| **Tatoeba 英语–高棉语平行句对（CC-BY）** | seed 语料，降低 LLM 生成量与母语者校验量 |
| CC-CEDICT（CC-BY-SA） | 中文词典释义 |

### 5.5 版权边界（硬约束）

**可用**：国家标准、考试大纲与样卷、公开词表、公开评分细则、CC 授权素材（按其授权条款署名）、Tatoeba / CC-CEDICT。

**不可用**：《HSK标准教程》《发展汉语》《新实用汉语课本》等出版社教材。**禁止范围包括：直接复制、改写洗稿、以及作为 LLM 生成时的 few-shot 示例。** 最后一条最容易被忽略，实现时在 `generate.py` 的 prompt 构造处加注释说明。

**实现要求**：
- 每个 content pack 必须记录 `sources[]` 与 `licence` 字段，`import_pack.py` 校验非空，为空则拒绝入库
- CC-BY 素材的署名信息随 pack 一同入库，并在产品的「关于」页聚合展示

---

## 6. 内容生产管线（离线，与运行时严格分离）

### 6.1 管线

```
[1] seed/{zh-hsk3.0, zh-hskk, zh-bct, en-gse}/*.yaml
        │                    ← 人工录入 concept 骨架（来自第 5 节白名单来源）
[2] generate.py              ← LLM 批量生成（Batch API，非实时）
        │   产出：目标句 / 替换项 / 对话任务 / 高棉语解释 / 干扰项 / 拼音声调
        │
[3] validate.py              ← 自动校验（见 6.2），失败项回流重生成
        │
[4] review_export.py         ← 导出 10% 抽样到 CSV，母语者审核
        │
[5] build_pack.py            ← 固化为 content_pack_vN.json + 预生成媒体
        │   --with-video     ← 默认关闭；开启后为标记 anchor=true 的 item 生成数字人视频
        │
[6] import_pack.py           ← 入库 + 上传媒体至对象存储 + 校验 sources/licence 非空
```

### 6.2 自动校验规则（必须全部实现）

| 规则 | 说明 |
|---|---|
| 词表越界 | 句子中所有词必须在目标 HSK 等级及以下词表内 |
| 长度 | HSK1 ≤ 8 字，HSK2 ≤ 12 字，HSK3 ≤ 18 字 |
| 拼音与声调 | 用 `pypinyin` 生成并与 LLM 输出比对，不一致则拒绝 |
| 高棉语解释非空且非中文 | 用 Unicode 区段检测（U+1780–U+17FF） |
| concept 覆盖 | 每个 concept 至少 8 个目标句、12 个替换项、3 个对话任务 |
| HSKK 任务类型非空 | `hskk_task_types` 为空则拒绝 |
| 去重 | 句子级 MinHash 去重，相似度 > 0.9 拒绝 |
| 来源合规 | `sources[]` 与 `licence` 非空 |

### 6.3 媒体预生成

所有**固定文本**（讲解、示范、提示）在打包阶段生成并存对象存储，运行时只读 URL。

- 中文音色：zh-CN 标准女声，语速 0.9x
- 高棉语音色：M0 选型产出，记录到 `docs/tts-decision.md`
- 视频：仅当 `--with-video` 且 item 标记 `anchor=true` 时生成，见第 7 节

---

## 7. AI 数字人演进路线

### 7.1 定位

**数字人是课程交付形态的目标状态，不是营销附属品。** 但不能一步到位——约束是**迭代成本**，不是技术可行性。

技术前置条件已确认：HeyGen 支持高棉语语音与中文多变体；API 为纯按量计费，Avatar III 约 $1/分钟（720p/1080p），Avatar IV 约 $3–4/分钟。

**真正的问题**：音频改一个字，TTS 几秒重跑，成本≈0；视频改一个字，要重渲染，成本 $1–4/分钟。内容包在早期会反复改，全量视频化等于把改稿成本乘上单价。**所以阶段的分界线是「内容稳定度」，不是「用户数」。**

### 7.2 四阶段与进入条件

| 阶段 | 形态 | 用在哪 | 进入条件（全部满足才推进） | 成本量级 |
|---|---|---|---|---|
| **S1** 分发资产 | 数字人短视频 | TikTok / Facebook「每天一句中文」 | M2 上线 | 约 $30–60/月 |
| **S2** 课程锚点层 | 数字人讲解视频 | 每级 level intro + 10–15 个高频难点 concept | ① 内容包 v1 已冻结<br>② S1 已验证形象接受度（互动数据）<br>③ 客户端音频回退已实现 | 三级约 70 分钟 ≈ **$70–280** 一次性 |
| **S3** 课程主形态 | 全 concept 数字人讲解，替代音频讲解卡 | 所有 `explain` item | ① **内容包连续 30 天无实质修改**<br>② 付费用户 ≥ 1000<br>③ A/B 测试显示视频版完课率提升 ≥ 15% | 300–375 分钟 ≈ **$375–1,500** 一次性 + 增量 |
| **S4** 实时数字人 | Pro 档实时语音叠加形象 | 实时对话 | 单位成本降至纯语音的 3 倍以内 | **当前不可行**，见 7.5 |

**S3 的第 ① 条是硬门槛，优先级高于用户数。** 内容还在改却先视频化，是把可再生资产变成沉没成本。

**S2 与 S3 之间必须做 A/B**：同一批用户随机分配音频版与视频版，比完课率与 7 日留存。如果提升 < 15%，S3 不做——视频在教学上不一定优于音频，讲解内容本身的质量权重更高。这条写成验收标准，不靠主观判断。

### 7.3 架构必须现在就留的口子（v1.1 的实际代码影响）

即使 v1 全部用音频，以下三项**必须在 M2 前实现**，否则 S2/S3 要返工：

1. **`lesson_items` 增加 `media_type ['audio'|'video']` 与 `anchor bool`**
2. **新增 `media_assets` 表**：同一 `lesson_item_id` 可同时挂音频与视频版本，运行时按套餐 + 网络状况选择
3. **客户端强制实现音频回退**：视频加载失败、弱网、或用户开启省流量模式时自动降级到音频版。**柬埔寨移动网络下这不是可选项**，是可用性底线

### 7.4 形象合规（硬约束）

数字人形象必须在首次出现处标注为 **AI 助教**（i18n key：`avatar.ai_disclaimer`），不得表述或暗示为真人教师。

理由：语言学习产品的信任是留存的组成部分。被用户自行发现是 AI 形象，比一开始就说明伤害更大；且冒充真人教师在多数市场存在合规风险。

### 7.5 S4 为什么当前不可行

实时数字人（如 HeyGen Video Agent）约 $2.00/分钟，而实时语音（gpt-realtime-mini）约 $0.016–0.03/分钟——**相差约 60–100 倍**。Pro 档定价 $5.99/月含 60 分钟，走实时数字人单月成本就是 $120，比售价高 20 倍。

S4 的进入条件是成本问题，不是产品问题。定期复查即可，不必列入路线图的执行部分。

---

## 8. 技术栈（已定）

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | Python 3.12 + FastAPI | 语音/AI 生态最成熟；内容管线同语言 |
| 数据库 | PostgreSQL 16 | 关系清晰，`jsonb` 存 content pack 片段 |
| 缓存/队列 | Redis 7（含 RQ 做后台任务） | 轻量，单机够用 |
| Bot | python-telegram-bot v21（webhook 模式） | 成熟 |
| Mini App | React 18 + TypeScript + Vite + Telegram WebApp SDK | 实时语音必须走 WebView |
| 语音评测/ASR/TTS | Azure AI Speech | 同时支持 `zh-CN` 与 `en-US` 发音评测，音素级打分 |
| 实时语音 | OpenAI Realtime `gpt-realtime-mini` | 成本约为旗舰的 1/3 |
| 数字人视频 | HeyGen API（按量，无订阅） | 支持高棉语语音；`media/` 下做 provider 抽象，可换 |
| 文本 LLM | 统一 `llm_client` 抽象层，默认 gpt-4.1-mini 级 | 可替换，不锁死 |
| 支付 | ABA PayWay（主）+ Bakong KHQR（主）+ Telegram Stars（次） | 见 9.3 |
| 部署 | Docker Compose，单 VPS（新加坡区） | 延迟低，运维成本低 |

**Azure 发音评测能力确认**（实现时依赖这些字段）：
- 支持 `en-US`（IPA 音素、音节、韵律）与 `zh-CN`（SAPI 音素）
- 返回维度：AccuracyScore / FluencyScore / CompletenessScore（仅 scripted）/ ProsodyScore（**仅 en-US**）/ PronScore
- **中文无 ProsodyScore。声调正确性需自行从音素级结果推导，在 `scoring/zh_tone.py` 单独处理**（M0-1 验证会给出音素串是否直接带声调数字的结论，决定这里是「解析」还是「推导」）

---

## 9. 数据模型

### 9.1 核心表

```sql
-- 用户
users(id, telegram_id, phone, locale, created_at, last_active_at)
user_profiles(user_id, target_language, current_level, daily_goal_minutes, timezone,
              data_saver bool DEFAULT false)     -- 省流量模式，影响媒体选择

-- 内容
courses(id, course_type['exam'|'job'], language, level, title_km, title_zh, org_id NULL)
concepts(id, language, level, pattern, km_explanation, common_l1_errors jsonb,
         hskk_task_types text[], sort_order)
lessons(id, course_id, concept_ids[], sequence, title_km)
lesson_items(id, lesson_id, item_type['explain'|'drill'|'vocab'|'qa'],
             payload jsonb, media_type['audio'|'video'] DEFAULT 'audio',
             anchor bool DEFAULT false, sequence)
media_assets(id, lesson_item_id, kind['audio'|'video'], url, duration_ms,
             bytes, provider, generated_at, source_text_hash)
             -- 同一 item 可同时有 audio 与 video 两行；source_text_hash 用于
             -- 判断文本是否变更，变更则标记媒体过期需重生成
content_packs(id, version, language, level, checksum, sources jsonb,
              licence text, published_at)

-- 学习
attempts(id, user_id, lesson_item_id, concept_id, audio_url, asr_text,
         accuracy_score, fluency_score, completeness_score, pron_score,
         phoneme_detail jsonb, passed bool, created_at)
concept_mastery(user_id, concept_id, mastery_score, attempt_count,
                last_seen_at, next_due_at, ease_factor, interval_days)
lesson_progress(user_id, lesson_id, status, completed_at)
streaks(user_id, current_streak, longest_streak, last_day)
experiments(user_id, experiment_key, variant, assigned_at)   -- S2→S3 的 A/B 用

-- 商业
subscriptions(id, user_id, plan['free'|'basic'|'pro'], status, period_start, period_end,
              payment_provider, external_ref)
entitlements(user_id, realtime_minutes_remaining, daily_attempts_used, reset_at)
payments(id, user_id, provider, amount_usd, status, raw_payload jsonb, created_at)
realtime_sessions(id, user_id, started_at, ended_at, minutes_billed, cost_usd_est, transcript_url)
cost_ledger(id, occurred_at, user_id NULL, provider, unit, quantity, cost_usd_est, ref)

-- B 端
orgs(id, name, contact, plan, seats, created_at)
org_members(org_id, user_id, job_role, enrolled_at)
org_reports(id, org_id, period, metrics jsonb, pdf_url, generated_at)
```

### 9.2 Mastery 算法

```
初始 mastery = 0
每次 attempt:
  delta = (pron_score - 60) / 40        # 60 分为及格线，映射到 [-1.5, +1.0]
  mastery = clamp(mastery + delta * weight[item_type], 0, 100)
  weight = {drill: 0.6, vocab: 0.8, qa: 1.0}   # 越接近真实使用权重越高

间隔重复（SM-2 变体）:
  mastery >= 80 → interval *= ease_factor (初始 2.5)
  mastery <  60 → interval = 1 day, ease_factor -= 0.2 (下限 1.3)
  next_due_at = now + interval
```

### 9.3 支付说明（重要）

**Telegram Stars 不能作为主通道：** 移动端支付综合损耗约 32%（应用商店 30% + Fragment 换汇价差）；提现有 21 天锁定期，全链路 22–27 天；**不支持自动续订**；最低提现门槛 1000 Stars（约 $13）。

- 主通道：**ABA PayWay**（有官方开发者文档与沙箱）+ **Bakong KHQR**（柬国家银行官方集成文档）
- 次通道：Telegram Stars，仅用于小额一次性超量包，不用于订阅
- `payments/` 下做 provider 抽象层，三家各一个 adapter，统一 webhook 入库

---

## 10. API 规格（关键端点）

```
# 认证
POST /api/v1/auth/telegram        # 校验 Telegram initData 签名，签发 JWT

# 学习
GET  /api/v1/courses?language=zh&type=exam
GET  /api/v1/courses/{id}/lessons
GET  /api/v1/lessons/{id}          # 返回 lesson_items + 已解析的媒体 URL
                                   # 服务端按 plan + data_saver + 实验分组决定
                                   # 返回 video 还是 audio，客户端不做这个判断
POST /api/v1/attempts              # multipart: audio + lesson_item_id
     → 200 {pron_score, accuracy, fluency, completeness, phonemes[], passed, feedback_km}
POST /api/v1/lessons/{id}/complete
GET  /api/v1/review/queue          # 弱项优先 + 到期复习

# 实时语音（Pro）
POST /api/v1/realtime/session      # 校验额度 → 返回 ws_url + ephemeral_token
WS   /ws/realtime/{session_id}     # 后端代理至 OpenAI Realtime
POST /api/v1/realtime/session/{id}/end

# 商业
GET  /api/v1/me/entitlements
POST /api/v1/payments/checkout     # {provider, plan} → 返回支付链接或 QR
POST /api/v1/payments/webhook/{provider}

# B 端
GET  /api/v1/org/{id}/members
POST /api/v1/org/{id}/members      # 批量发放账号
GET  /api/v1/org/{id}/report?period=2026-10   # 返回 PDF URL

# 运维
GET  /api/v1/admin/costs           # 按 provider / 用户 / 日聚合，M2 前必须可用
```

---

## 11. 单位成本模型（硬约束）

### 11.1 假设

| 参数 | 值 | 依据 |
|---|---|---|
| Basic 用户月均录音时长 | 20 分钟 | 每天 10 句 × 4 秒 × 20 活跃日 ≈ 13 分钟，取 20 分钟留余量 |
| Azure Speech（含发音评测）单价 | 按 STT 标准计费 | 微软文档：发音评测与 STT 同价 |
| Realtime mini 单价 | 音频输入 $10/M tok，输出 $20/M tok | 约 $0.016/分钟基准 |
| Realtime 实际放大系数 | 2× | 上下文增长；已用稳定提示 + 8 轮摘要压制 |
| 数字人视频 | Avatar III 约 $1/分钟；Avatar IV 约 $3–4/分钟 | HeyGen API 按量 |

### 11.2 运营成本上限（每用户每月）

| 档位 | 月成本上限 | 定价 | 目标毛利 |
|---|---|---|---|
| Free | $0.15 | $0 | — |
| Basic | $0.45 | $1.99 | ≥ 75% |
| Pro | $2.30 | $5.99 | ≥ 60% |

### 11.3 内容生产成本（一次性，不计入每用户成本）

| 项 | 量 | 成本 |
|---|---|---|
| TTS 音频（HSK1–3 全量） | — | 可忽略 |
| S1 分发短视频 | 30–60 条/月 × ≤1 分钟 | $30–60/月 |
| S2 锚点视频 | 约 70 分钟 | $70–280 一次性 |
| S3 全量视频 | 300–375 分钟 | $375–1,500 一次性 |

**实现要求**：
- 每个外部 API 调用写入 `cost_ledger`，记录 provider / 单位 / 估算美元成本
- 后台任务每日汇总；任何用户单月成本超过档位上限 1.5 倍时告警并自动限流
- 视频生成成本单独归入 `cost_ledger` 的 `ref='content_production'`，不混入运营成本

### 11.4 成本拐点（记录在案，V1 不实现）

Basic 月活超过约 3,000 人时，自建 Whisper + 强制对齐（MFA）做发音评分的总成本将低于 Azure。届时 `scoring/` 需保持 provider 可替换。**V1 只需保证接口抽象，不做自建。**

---

## 12. B 端（Entry A）最小可售形态

**不做真人授课、不做排课、不做直播。**

企业购买的是：
1. 按席位发放的账号（复用 C 端全部学习功能）
2. **岗位课包**：酒店前台 / 工厂安全 / 餐厅点单 等，每包 50–80 个目标句式，seed 取自 BCT 场景清单 + 客户实际岗位用语
3. **月度进度报告**（自动生成 PDF）：出勤、开口时长、concept 掌握度分布、TOP5 弱项、环比

岗位课包走同一条内容管线，只是 seed 来源不同。报告生成是定时任务，每月 1 日为所有 active org 生成并邮件推送。

---

## 13. 里程碑与验收标准

| 里程碑 | 周 | 交付内容 | 验收标准 |
|---|---|---|---|
| **M0 技术验证** | 1 | Azure 中文发音评测打通；高棉语 TTS 选型；ABA PayWay 沙箱联通 | 三份验证报告落 `docs/`；发音评测对 good/bad 样本的平均分差 ≥15 且重叠 ≤20% |
| **M1 内容管线** | 2–3 | HSK1 完整内容包 | HSK1 全部 concept 生成、通过全部自动校验（含 HSKK 任务类型与来源合规）、母语者抽检合格率 ≥ 90%、音频全部预生成 |
| **M2 Basic 闭环上线** | 4–6 | Telegram Bot + 回合制学习闭环 + Free/Basic 付费墙 + ABA 收款 + **媒体双形态与音频回退** + 成本看板 | 真实用户可完成 HSK1 一个完整单元；支付可完成；`media_assets` 双形态可切换；`/admin/costs` 可用 |
| **M3 Pro 实时语音** | 7–8 | Mini App + Realtime 代理 + 分钟计量 + 超量包 + **S2 锚点视频** | 实时会话可用、分钟扣减准确、硬上限生效；S2 视频上线且弱网可回退音频 |
| **M4 B 端最小可售** | 9–10 | 岗位课包 ×1 + 企业后台 + 自动月报 | 可为一家企业发放 20 席位并生成一份月报 PDF |
| **S3 门槛评估** | M4 后 | A/B 实验报告 | 按 7.2 的三条进入条件判定，不满足则不进入 S3 |

按 15–20 小时/周投入估算。**M2 结束即可产生第一笔收入，不必等到 M4。**
S1 分发短视频与 M2 并行启动，不占用工程工时。

---

## 14. 非目标（明确不做）

| 不做 | 原因 |
|---|---|
| 真人教师、排课、直播 | 重运营，与定位冲突 |
| 独立 iOS / Android App | 柬埔寨下载摩擦高；Telegram 已覆盖 |
| 运行时 LLM 生成课程内容 | 不一致、无进度、成本不可控——同类产品失败主因 |
| **M1–M2 阶段生成任何视频资产** | 内容未冻结，视频化等于把改稿成本乘上单价 |
| **S4 实时数字人** | 成本约为纯语音的 60–100 倍，定期复查即可 |
| 社交功能（好友、排行榜、PK） | v2 再议，MVP 阶段是干扰项 |
| 英文内容包 | V1 只做中文，架构预留，GSE seed 可先积累 |
| 自建 ASR / 发音评分模型 | 见 11.4，未到成本拐点 |
| Telegram Stars 订阅 | 损耗 32% + 无自动续订 + 21 天锁定 |
| 手写课程大纲 | 直接采用第 5 节白名单来源 |
| 使用出版社教材 | 见 5.5，含改写与 few-shot 用途 |

---

## 15. 工程约定

### 15.1 目录结构

```
kruai/
├── backend/
│   ├── app/
│   │   ├── api/v1/          # FastAPI 路由
│   │   ├── core/            # 配置、鉴权、日志
│   │   ├── models/          # SQLAlchemy
│   │   ├── services/
│   │   │   ├── scoring/     # 发音评分（provider 抽象）
│   │   │   ├── realtime/    # OpenAI Realtime 代理
│   │   │   ├── media/       # 媒体解析与选择（audio/video + 回退）
│   │   │   ├── mastery/     # concept mastery + SM-2
│   │   │   ├── payments/    # aba / bakong / stars adapters
│   │   │   ├── experiments/ # A/B 分组
│   │   │   └── reports/     # B 端月报生成
│   │   └── workers/         # RQ 任务
│   └── tests/
├── bot/                      # python-telegram-bot
├── miniapp/                  # React + TS
├── pipeline/                 # 内容生成管线（独立 CLI）
│   ├── seed/{zh-hsk3.0, zh-hskk, zh-bct, en-gse}/
│   ├── generate.py validate.py build_pack.py import_pack.py
│   ├── video/                # 数字人视频生成（provider 抽象，默认不启用）
│   └── packs/
├── docs/
└── docker-compose.yml
```

### 15.2 强制要求

- 所有外部 API 调用走统一 client 层，带重试、超时、成本记账
- `scoring/`、`payments/`、`media/video`、`llm_client` 必须是 provider 可替换的抽象
- 每个 service 有单元测试；`attempts` 评分链路必须有集成测试（含固定音频样本）
- 环境变量走 `.env` + pydantic-settings，**不得硬编码任何 key**
- 数据库变更一律经 alembic migration
- 所有金额以美分整数存储，不用浮点
- 媒体选择逻辑**只在服务端**，客户端不做判断，只负责播放与回退

### 15.3 需要人工确认再动手的点

1. 高棉语 TTS 最终音色（M0 产出选型报告后确认）
2. ABA PayWay 商户资质与结算主体
3. 母语者审核人选与报酬方式
4. 岗位课包的首个场景（建议：酒店前台中文，与现有资源最近）
5. 数字人形象选型（S1 启动前确认，一旦确定不轻易更换——形象是品牌资产）

---

## 16. 决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| V1 语言 | 仅中文 HSK1–3 | 中文赛道无以高棉语授课的竞品；英文赛道竞争充分 |
| 课程来源 | 第 5 节白名单：等级标准 + HSK + **HSKK** + BCT + GSE + MoEYS OER + Tatoeba | 全部公开或 CC 授权；HSKK 补上了口语判分依据 |
| 教材使用 | 出版社教材一律不用，含改写与 few-shot | 版权风险 |
| 内容生成 | 离线批量 + 自动校验 + 10% 人工抽检 + 固化 | 运行时生成是同类产品失败主因 |
| 交互载体 | Telegram Bot（Basic）+ Mini App（Pro） | 覆盖与体验分层，与双档套餐天然对应 |
| 发音评测 | 买 Azure，不自建 | 支持中英双语音素级打分，未到自建拐点 |
| 实时语音 | gpt-realtime-mini + 后端代理 + 硬上限 | 成本可控且计量权威 |
| **数字人定位** | **课程交付形态的目标状态，分 S1–S4 推进；架构在 M2 前留好双形态口子** | 技术已可行（支持高棉语），约束是内容迭代成本，故以「内容稳定度」而非用户数作为阶段门槛 |
| **S3 进入条件** | **内容包 30 天无实质修改 + 付费用户 ≥1000 + A/B 完课率提升 ≥15%** | 三条缺一不可；第一条优先级最高 |
| 音频回退 | 强制实现项，M2 交付 | 柬埔寨移动网络下是可用性底线 |
| 形象合规 | 明确标注 AI 助教，不得冒充真人 | 信任是留存的组成部分 |
| 支付主通道 | ABA PayWay + Bakong KHQR | Telegram Stars 损耗 32%、无自动续订 |
| 双入口 | 同一引擎，`course_type` 区分 | 内容管线复用，销售多一个出口 |
| B 端形态 | 账号 + 岗位课包 + 自动月报 | 边际成本接近零，规避重运营 |
| 真人授课 | 否决 | 与 15–20h/周 的投入约束冲突 |

---

## 附录 A：参考资料

- 《国际中文教育中文水平等级标准》，中华人民共和国教育部
- HSK / HSKK / YCT / BCT 考试大纲与样卷，汉语考试服务网（中外语言交流合作中心）
- Global Scale of English Teacher Toolkit，Pearson
- 柬埔寨教育部开放教育资源平台 `oer.moeys.gov.kh`（CC-BY 3.0）
- Tatoeba 英语–高棉语平行语料（CC-BY）
- Azure AI Speech — Pronunciation Assessment 官方文档
- OpenAI Realtime API 定价（gpt-realtime / gpt-realtime-mini）
- HeyGen API 定价与语音语言支持列表
- ABA PayWay Developer Suite；Bakong KHQR Payment Integration（柬埔寨国家银行）
- DataReportal《Digital 2026: Cambodia》
- Speak 产品结构公开评测（四段式课程、concept mastery、Smart Review、双档定价）
