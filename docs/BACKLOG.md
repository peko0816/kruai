# 任务清单

**按编号顺序推进。一个条目 = 一个 PR。** 不要跳过顺序挑"有意思的"做。

标记：
- `[BLOCKED-M0]` —— 只实现接口与 Fake，真实实现留 `# TODO(M0-x)`
- `[GATE]` —— 里程碑关卡，前面所有条目验收通过才能进入

---

## 阶段 A：地基（M0 完全不阻塞，可全速）

| # | 任务 | 依赖 | 验收 |
|---|---|---|---|
| A1 | 建仓与骨架：目录结构（ARCHITECTURE 第 6 节）、`pyproject.toml`、ruff/mypy/pytest 配置、`Makefile`（check/test/lint/fmt/migrate/run） | — | `make check` 在空项目上全绿 |
| A2 | `docker-compose.yml`：PostgreSQL 16 + Redis 7；`.env.example` 按 CONFIG_REFERENCE 生成 | A1 | `docker compose up` 起得来，健康检查通过 |
| A3 | `core/`：config（pydantic-settings，`extra="forbid"`）、structlog 日志、`errors.py`（AppError 体系）、`money.py` | A1 | 配置项与 CONFIG_REFERENCE 一一对应；`.env` 多一个键即启动失败 |
| A4 | 全量 SQLAlchemy 模型 + alembic 初始 migration，严格对齐 `DATA_MODEL.sql` | A2 A3 | `upgrade` 后表结构与 DDL diff 为空；`downgrade` 跑通 |
| A5 | CI：GitHub Actions 跑 `make check`，全 provider=fake | A1 | PR 触发，全绿 |

`[GATE]` **G-A**：`make check` 全绿 + 数据库可建可回滚。

---

## 阶段 B：Provider 抽象与 Fake（并行开发的前提）

| # | 任务 | 依赖 | 验收 |
|---|---|---|---|
| B1 | `services/scoring/`：`base.py`（**原样复制 interfaces/scoring_base.py**）+ `fake.py` + `registry.py` | A3 | FakeScorer 确定性；同输入同输出；`__BAD__` 触发低分 |
| B2 | `services/tts/`：同上结构，`FakeTTS` 生成指定时长静音 mp3 | A3 | 可被 pipeline 调用 |
| B3 | `services/llm/`：同上结构，`FakeLLM` 按 json_schema 产出结构合法占位 | A3 | `batch_complete` 返回顺序与入参一一对应 |
| B4 | `services/payments/`：同上结构，`FakePaymentProvider` 用固定 HMAC key 验签 | A3 | `verify_callback` 无副作用；`create_checkout` 幂等 |
| B5 | registry 启动期能力自检：所配 provider 是否覆盖所需语言，不通过则启动失败 | B1–B4 | 故意配错 → 启动报错并指明缺哪个语言 |
| B6 | `cost_ledger` 记账装饰器/上下文管理器；`COST_LEDGER_REQUIRED=true` 时未记账的外部调用抛异常 | A4 B1 | 写一个不记账的调用 → 测试捕获到异常 |

`[GATE]` **G-B**：全部 provider=fake，系统可启动，能力自检通过。

---

## 阶段 C：领域层（系统中枢，纯逻辑，重点单测）

| # | 任务 | 依赖 | 验收 |
|---|---|---|---|
| C1 | `services/mastery/`：delta 计算 + clamp + 按 item_type 加权（PRD 9.2），全部参数来自配置 | A3 A4 | 单测覆盖：满分、零分、负 delta、上下边界、权重切换 |
| C2 | `services/mastery/` SM-2 变体：ease_factor 升降、interval 计算、`next_due_at` | C1 | 单测覆盖：连续通过、连续失败、ease 触底 1.3 |
| C3 | 复习队列：按 mastery 升序 + 到期时间排序，支持 5/10/15/25 分钟时长切片 | C2 | 单测：给定 mastery 分布，队列顺序符合预期 |
| C4 | `services/entitlements/`：额度校验与扣减。**必须原子更新**（`UPDATE ... WHERE remaining >= n`），禁止读-改-写 | A4 | 并发测试：10 并发扣同一额度，总扣减数正确，无超扣 |
| C5 | 额度重置：按 `user_profiles.timezone` 的本地 `QUOTA_RESET_HOUR_LOCAL` 重置 | C4 | 单测覆盖跨时区与夏令时边界 |
| C6 | `services/media/`：`resolve(item, user)` → 主 URL + fallback URL。输入 media_type / plan / data_saver / 实验分组 | A4 | 单测：video 但用户 data_saver → 返回 audio 为主；无 video 资产 → audio |
| C7 | `services/experiments/`：稳定分组（`hash(user_id + key)`），写入 experiments 表 | A4 | 同一用户多次分组结果一致 |

`[GATE]` **G-C**：领域层单测覆盖充分，无一处 import 适配层具体实现。

---

## 阶段 D：API 与 Bot（M2 闭环）

| # | 任务 | 依赖 | 验收 |
|---|---|---|---|
| D1 | Telegram `initData` 验签 + JWT 签发：`POST /api/v1/auth/telegram` | A3 | 伪造 initData 被拒；有效 initData 签出 JWT |
| D2 | 课程与课时读取：`GET /courses`、`/courses/{id}/lessons`、`/lessons/{id}`（含媒体解析，返回 video_url + audio_url） | C6 | 响应含双 URL；服务端已做选择，客户端无需判断 |
| D3 | `POST /api/v1/attempts` 全链路：额度校验 → scoring → cost_ledger → 落库 → mastery → 复习队列 | B1 B6 C1-C4 | 集成测试用 FakeScorer + 固定音频跑通；失败时不扣额度不写 attempts |
| D4 | `POST /lessons/{id}/complete` + `GET /review/queue` | C3 | 完课后 mastery 与队列正确更新 |
| D5 | `GET /me/entitlements` + `GET /admin/costs`（按 provider/用户/日聚合） | B6 C4 | 成本看板能看到 D3 产生的记账 |
| D6 | Bot 会话状态机：讲解 → drill → vocab → Q&A → 完课；voice note 往返 | D2 D3 | 用 Fake Provider 在真实 Telegram 里走完一个单元 |
| D7 | i18n 框架 + `locales/{km,zh,en}.json`；CI 检查 km 无占位符 | A3 | 内联字符串检查通过 |
| D8a | 付费墙 + 下单收单：Free/Basic 限额生效；`POST /payments/checkout` + `POST /payments/webhook/{provider}`；`Money` 值对象与 `core/money.py` | B4 C4 | FakeProvider 下可完成一笔"支付"并开通订阅；验签失败拒绝开通；KHR（0 位小数）与 USD（2 位）都有单测 |
| D8b | **订阅续费双路径**（ARCHITECTURE 3.4）：`renewal_mode` 状态机、grace 期、auto 扣款定时任务 + 重试、manual 提醒定时任务 + 防重复 | D8a | 两条路径各有集成测试；FakeProvider 可分别模拟 `supports_recurring` 为真/假；grace 到期正确降级 |
| D9 | `[BLOCKED-M0]` ABA PayWay 真实 adapter（含 `HASH_FIELD_ORDER` 核对） | D8a + M0-3 | 真实小额支付 + 回调验签通过 |
| D11 | 订阅升级（Basic → Pro）。**先决条件：产品所有者决定已付天数怎么处理**（立即取消不退差额 / 按剩余天数折算 / 到期再换档）。落地时同时把 `subscriptions` 的币种与周期从「回读最近一笔付款」改成两个真实列（见 D-060），否则一人两订阅时会算错续费条款 | D8a D8b + 产品决策 | 升级后旧订阅不再续费；新档位立即生效；折算金额（若做）有整数边界单测；`/payments/checkout` 的 `payment.already_subscribed` 分支被升级流程取代 |
| D10 | 成本护栏（PRD 11.3）：按用户按月累计 `cost_ledger`，超过档位上限 `COST_ALERT_MULTIPLIER` 倍时告警并自动限流。**比较必须显式取整**（L-1：`45 × 1.5 = 67.5`，67 算不算超标要人来定，不能让浮点序关系替你决定） | D5 D8a | 单测覆盖取整边界（恰好 67、恰好 68）；被限流的用户拿到 **429 `cost.cap_reached`**（不是 402——他已经付过钱了，续费解决不了成本超限，见 D-071）且不产生付费调用、不扣额度；告警可在日志与 `/admin/costs` 里看到 |

`[GATE]` **G-D / M2**：对照 `DEFINITION_OF_DONE.md` 的 M2 清单逐条验收。

> 注：D8b（续费双路径）相比"只做单一路径"约增加 3–5 天工作量，已计入 M2 的 4–6 周区间的上沿。
>
> 注：D11（升级）当前**被产品决策阻塞**，不是被代码阻塞。在它落地之前，
> `POST /payments/checkout` 对已有生效订阅的人返回 `payment.already_subscribed`
> 并拒绝下单（D-065）——拒绝不涉及任何金额计算，所以不会引入需要回退的取整逻辑。
>
> 注：D10 排在 D8a 之后，因为限流要决定「降到哪一档」，而那取决于订阅链路已经跑通。
> 已完成：护栏在 `services/entitlements/cost_guard.py`，取整口径见 D-070，
> 返回码与顺序见 D-071，月份口径见 D-072；L-1 与 L-10 两条遗留约束就此消除。

---

## 阶段 E：内容管线（与 D 并行）

| # | 任务 | 依赖 | 验收 |
|---|---|---|---|
| E1 | seed YAML schema 定义 + 校验器；`pipeline/seed/zh-hsk3.0/` 目录结构 | A1 | 非法 seed 被拒并指出具体字段 |
| E2 | HSK1 concept 骨架录入（来自公开大纲，含 `hskk_task_types`） | E1 | HSK1 全部 concept 有 seed |
| E3 | `generate.py`：调 `llm.batch_complete`，产出目标句/替换项/对话任务/高棉语解释/拼音声调 | B3 E1 | FakeLLM 下产出结构合法 |
| E4 | `validate.py`：8 条规则全实现（词表越界、长度、拼音声调、高棉语区段、concept 覆盖、hskk 非空、MinHash 去重、来源合规） | E3 | 每条规则一正例一反例，全部覆盖 |
| E5 | `review_export.py`：10% 抽样导 CSV | E4 | 抽样比例可配置，输出可直接给母语者 |
| E6 | `build_pack.py`：固化 JSON + 调 TTS 预生成音频；`--with-video` 开关（默认关，S2 前不实现视频分支） | B2 E4 | FakeTTS 下产出完整 pack |
| E7 | `import_pack.py`：入库 + 上传对象存储 + 校验 sources/licence 非空 | A4 E6 | 空库导入 + 一致性校验通过；sources 为空则拒绝 |
| E8 | `[BLOCKED-M0]` 真实 TTS adapter（按 M0-2 结论） | E6 + M0-2 | 高棉语音频可生成并通过母语者复听 |
| E9 | `[BLOCKED-M0]` 真实 scoring adapter（按 M0-1 结论）+ `scoring/zh_tone.py` | B1 + M0-1 | 按 `ZH_TONE_MODE` 结论实现解析或推导；真人样本区分度达标 |

`[GATE]` **G-E / M1**：对照 M1 验收清单。

---

## 阶段 F：M3 实时语音（G-D 通过后才开始）

| # | 任务 |
|---|---|
| F1 | Mini App 骨架（React + TS + Telegram WebApp SDK） |
| F2 | 实时会话后端代理（WS），系统提示逐字稳定，8 轮后摘要压缩 |
| F3 | 秒级计量与原子扣减；单次 15 分钟服务端硬断开 |
| F4 | 超量包购买与即时生效 |
| F5 | 媒体视频形态接入 + 客户端 3 秒超时回退音频 |
| F6 | S2 锚点视频生成（`pipeline/video/`，provider 抽象） |

## 阶段 G：M4 B 端（G-D 通过后可与 F 并行）

| # | 任务 |
|---|---|
| G1 | orgs / org_members 与席位发放 |
| G2 | 岗位课包 seed（酒店前台中文，seed 源自 BCT 场景清单） |
| G3 | 企业后台只读视图 |
| G4 | 月报定时任务 + PDF 生成 + 邮件推送 |
| G5 | 跨 org 数据隔离测试 |

---

## M0 结论落地清单（三份报告齐备后立即执行）

- [ ] 按 M0-1 校准 `SCORING_PASS_THRESHOLD`，设定 `ZH_TONE_MODE`
- [ ] 按 M0-2 写 `docs/tts-decision.md`，实现 E8
- [ ] 按 M0-3 核定 PayWay `HASH_FIELD_ORDER` 与回调字段，实现 D9
- [ ] 三个 provider 从 `fake` 切到真实，CI 保留 fake 配置不变
