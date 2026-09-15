# CLAUDE.md — KruAI 项目纪律

> 本文件在每次会话开始时都会被读取。它的优先级**高于**你对"通常应该怎么做"的判断。
> 与 `docs/PRD.md` 冲突时，以 PRD 为准并在此处提出修订，不要自行取舍。

---

## 1. 这是什么项目

KruAI：以高棉语为教学语言的中文口语陪练系统。一套引擎，两个入口——C 端 HSK 备考（Telegram Bot / Mini App），B 端中资雇主的岗位中文培训。

完整规格见 `docs/PRD.md`（v1.1）。**动手前必读第 0、3、5、7、11、13、14 节。**

---

## 2. 七条红线

违反任何一条的 PR 一律不合并。不存在"这次特殊"。

| # | 红线 | 检查方式 |
|---|---|---|
| R1 | **运行时不得调用 LLM 生成课程内容**。课程是数据（content pack JSON），不是代码 | `app/api/` 与 `app/services/mastery/` 下不得出现 llm_client 调用 |
| R2 | **M0 未决的外部能力一律走 Provider 抽象 + Fake 实现**，不得直接写死某家厂商 SDK | 见第 4 节 |
| R3 | **阈值、权重、限额不得硬编码**，全部走配置 | 见 `docs/CONFIG_REFERENCE.md` |
| R4 | **用户支付金额用 `amount_minor` + `currency_minor_units`（最小货币单位整数）**；内部成本核算用 `_usd_cents`。禁止 float/Decimal，禁止把 KHR 当作有小数位 | 类型注解 + 单测 |
| R5 | **不得硬编码任何密钥**，全部经 pydantic-settings 从环境读取 | pre-commit 扫描 |
| R6 | **PRD 第 14 节「非目标」不得实现**，包括"顺手加一下" | PR 自检 |
| R7 | **PRD 第 5.5 节版权边界**：出版社教材不得进入管线，含改写与 LLM few-shot 示例 | content pack 的 `sources[]`/`licence` 非空校验 |

---

## 3. 当前阶段与允许触碰的范围

**当前阶段：M1（内容管线）与 M2（Basic 闭环）并行，M0 外部验证同步进行中。**

| 允许现在做 | 现在不要做 |
|---|---|
| 项目骨架、配置、日志、CI | Mini App（属 M3） |
| 全部数据模型 + alembic migration | 实时语音代理（属 M3） |
| Telegram 鉴权（initData 校验） | 数字人视频生成（属 S2，见 PRD 7.2） |
| concept / mastery / SM-2 引擎 | 任何 provider 的**真实实现**（M0 未出结论） |
| entitlements 与限额 | B 端企业后台（属 M4） |
| 订阅续费双路径（auto / manual） | |
| cost_ledger 与 `/admin/costs` | 社交 / 排行榜 / 好友 |
| media_assets 与媒体选择 + 音频回退 | |
| `pipeline/generate.py`、`validate.py`、seed schema | |
| Bot 会话状态机（用 Fake Provider 跑通） | |

**越出「允许」列之前，先问，不要自作主张扩范围。**

---

## 4. Provider 抽象纪律（这是并行开发能成立的前提）

M0 验证尚未完成，三个外部能力的选型未定。处理方式是**统一的**：

```
先定接口 → 立刻写 Fake 实现 → 全系统只依赖接口 → M0 出结论后只新增一个实现类
```

`interfaces/` 目录下的四个文件是**规范性的**，必须原样复制到下列路径，签名不得改动：

| 规范文件 | 目标路径 |
|---|---|
| `interfaces/scoring_base.py` | `backend/app/services/scoring/base.py` |
| `interfaces/tts_base.py` | `backend/app/services/tts/base.py` |
| `interfaces/payments_base.py` | `backend/app/services/payments/base.py` |
| `interfaces/llm_base.py` | `backend/app/services/llm/base.py` |

每个 `base.py` 旁边必须有：
- `fake.py` —— 确定性 Fake 实现（同样输入必得同样输出，禁止随机数）
- `registry.py` —— 按配置 `*_PROVIDER` 选择实现，默认 `fake`

**判断标准：把 `.env` 里所有 provider 设为 `fake`，整个系统必须能端到端跑通并通过全部测试。** 如果做不到，说明有地方绕过了抽象。

---

## 5. 工作方式

### 5.1 任务来源

按 `docs/BACKLOG.md` 的顺序推进。每个任务标注了依赖与是否被 M0 阻塞。**不要跳过顺序挑"有意思的"做。**

被标记 `[BLOCKED-M0]` 的任务：只实现接口与 Fake，真实实现留空并加 `# TODO(M0-x)` 注释。

### 5.2 提交粒度

一个 PR = 一个 BACKLOG 条目 = 一个可独立验收的单元。

提交信息格式：
```
<scope>: <做了什么>

<为什么这么做，如果不显然>

Refs: BACKLOG#<编号>
```

`scope` 取值：`core` `api` `bot` `pipeline` `scoring` `payments` `media` `mastery` `infra` `docs`

### 5.3 每个 PR 必过的检查

见 `docs/DEFINITION_OF_DONE.md`。简版：

- [ ] `make check` 全绿（lint + type + test）
- [ ] 新增逻辑有单测；涉及金额、计量、评分的必须有边界用例
- [ ] 没有新增硬编码阈值（对照 `docs/CONFIG_REFERENCE.md`）
- [ ] 数据库变更有 alembic migration，且 `upgrade`/`downgrade` 都验证过
- [ ] 没有触碰第 3 节「现在不要做」的范围

---

## 6. 决策与不确定性处理

**遇到 PRD 没写清楚的地方：**

1. 先查 `docs/ARCHITECTURE.md` 和 `docs/PRD.md` 是否真的没写
2. 若确实缺失，**选择最保守、最容易回退的方案**，实现它，并在 `docs/DECISIONS.md` 追加一条记录：
   ```
   ## D-<编号> <一句话结论>
   - 日期：
   - 背景：PRD 未定义 X
   - 选择：
   - 理由：
   - 回退成本：
   ```
3. 不要停下来等人。但**涉及以下四类必须停下来问**：
   - 花钱的（调用付费 API 的默认开关、并发上限）
   - 动钱的（定价、计量、退款逻辑）
   - 对外可见的（用户文案的语气、形象表述）
   - 不可逆的（数据删除、生产迁移）

**不要因为"看起来更好"就重构已通过验收的模块。** 想改先在 `docs/DECISIONS.md` 提案。

---

## 7. 语言与文案

- 代码标识符、注释、commit、日志：**全英文**
- 用户可见文案：**全部走 i18n**，不得内联字符串。语言键：`km`（高棉语，主）、`zh`、`en`
- i18n 文件缺 `km` 翻译时用占位符 `[[km:key]]`，不得回退到英文静默上线
- 数字人形象相关文案必须包含 `avatar.ai_disclaimer`（PRD 7.4），不得表述为真人教师

---

## 8. 成本纪律

每一次外部 API 调用都必须写入 `cost_ledger`（provider / unit / quantity / cost_usd_cents_est / ref）。

**没有记账的调用等于没有发生过——这类 PR 直接打回。**

PRD 第 11 节的档位成本上限是硬约束。写任何会产生调用的代码时，先回答："这行代码在最坏情况下每用户每月花多少钱？"答不上来就先别写。

---

## 9. 测试

- 纯逻辑（mastery、SM-2、entitlements、限额、金额计算）：**必须单测，覆盖边界**
- 评分链路：集成测试，用 `tests/fixtures/audio/` 下的固定样本 + FakeScorer
- 内容管线：`validate.py` 的每一条校验规则各一个正例一个反例
- 不写"为了覆盖率"的测试。测试要断言行为，不是断言实现细节

---

## 10. 你容易犯的错（针对本项目）

1. **把课程内容做成运行时生成**——这是同类产品最大的失败原因，也是 R1 存在的理由
2. **在 provider 抽象上开后门**——比如"这里直接调 Azure 更快"。不行
3. **把阈值写进业务逻辑**——60 分及格线是 M0 出结果后要校准的，现在写死等于埋雷
4. **假设订阅一定能自动续费**——柬埔寨多数本地渠道不支持代扣。`auto` 与 `manual` 两条路径都必须走通，由 `provider.supports_recurring` 分支
5. **把 KHR 当作有小数位**——KHR 没有"分"。金额一律 `amount_minor` + `currency_minor_units`
6. **提前做 M3 的东西**——实时语音的计量依赖 entitlements，顺序不能反
7. **扩大范围**——PRD 第 14 节那张表就是为了挡这个
8. **改 PRD 里已定的技术选型**——有意见写 `docs/DECISIONS.md` 提案，不要直接换

---

## 11. 文档地图

| 文件 | 看它找什么 |
|---|---|
| `docs/PRD.md` | 产品规格全文，唯一权威 |
| `docs/ARCHITECTURE.md` | 模块边界、数据流、provider 契约 |
| `docs/DATA_MODEL.sql` | 权威 DDL，建表以它为准 |
| `docs/CODING_STANDARDS.md` | 命名、错误处理、日志、目录结构 |
| `docs/CONFIG_REFERENCE.md` | 所有配置项；判断"这个能不能硬编码" |
| `docs/DEFINITION_OF_DONE.md` | PR 检查单、里程碑验收标准 |
| `docs/BACKLOG.md` | 做什么、什么顺序、哪些被 M0 阻塞 |
| `docs/DECISIONS.md` | 你自己追加的决策记录（初始为空） |
