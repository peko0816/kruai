# 决策记录

PRD 未定义、由实施方自行决定的事项记录在此。

**写入规则**（CLAUDE.md 第 6 节）：
- 只记录 PRD 与 ARCHITECTURE 确实没写的
- 选择最保守、最容易回退的方案
- 记完继续推进，不要停下来等人
- 但涉及**花钱 / 动钱 / 对外可见 / 不可逆**四类，必须停下来问，不要自行决策

---

## 格式

```
## D-001 <一句话结论>

- 日期：YYYY-MM-DD
- 背景：PRD 未定义 X；遇到的具体场景是 Y
- 选择：
- 备选与放弃原因：
- 回退成本：低 / 中 / 高，以及怎么回退
- 影响范围：涉及哪些模块
```

---

<!-- 从 D-001 开始追加，不要删除历史条目。推翻旧决策时新增一条并注明「取代 D-00X」。 -->

## D-001 docker-compose 的容器供给变量不进入 Settings，也不进入 CONFIG_REFERENCE

- 日期：2026-09-15
- 背景：PRD 与 ARCHITECTURE 未定义本地开发数据库凭据如何管理。A2 要起
  PostgreSQL 容器，官方镜像需要 `POSTGRES_USER` / `POSTGRES_PASSWORD` /
  `POSTGRES_DB`。但 `core/config.py` 的 `Settings` 用 `extra="forbid"`
  （CODING_STANDARDS 第 4 节），而 docker compose 会自动读取同目录的 `.env`——
  若把这些键写进 `.env`，应用启动会因为多出未声明的键而直接失败。
- 选择：把它们定位为**容器供给参数**而非应用配置。`docker-compose.yml` 内用
  `${POSTGRES_USER:-kruai}` 形式内联默认值，`.env.example` 不含这些键，
  `CONFIG_REFERENCE.md` 也不收录。应用侧只认 `DATABASE_URL` 一个入口。
  需要改端口或账密时，改 compose 文件或用 shell `export`，不要写进 `.env`。
- 备选与放弃原因：
  1. 把 `POSTGRES_*` 加进 `Settings` —— 应用根本不使用这三个值，
     徒增配置面，且让「配置项与 CONFIG_REFERENCE 一一对应」的验收变模糊。
  2. 给 compose 单独一个 env 文件（如 `deploy/dev-db.env`）—— 多一个文件、
     多一处需要与 `DATABASE_URL` 保持同步的地方，收益不抵成本。
  3. 凭据直接硬编码进 compose —— 与 R5 的字面要求冲突，虽然仅限本机 dev，
     但没必要踩线。
- 回退成本：低。若将来确需应用读取这些值，加进 `Settings` +
  `CONFIG_REFERENCE.md` + `.env.example` 三处即可，compose 的 `${VAR:-default}`
  写法天然兼容。
- 影响范围：`docker-compose.yml`、`.env.example`、A3 的 `core/config.py`。
- 遗留约束：compose 默认值与 `.env.example` 的 `DATABASE_URL` 必须手工保持一致，
  两个文件都写了交叉引用注释。

## D-002 对象存储在 M1 之前留空，Settings 中必须是可选项

- 日期：2026-09-15
- 背景：`CONFIG_REFERENCE` 第 1 节的 `OBJECT_STORAGE_ENDPOINT` 与
  `PUBLIC_MEDIA_BASE_URL` 标注为无默认值。BACKLOG A2 明确把 compose 的范围限定为
  PostgreSQL + Redis，未包含 MinIO 之类的对象存储。而 `TTS_PROVIDER=fake` 时
  管线不上传任何媒体，运行时也没有 URL 要解析。
- 选择：`.env.example` 中这两项留空，不为了填满而往 compose 里加 MinIO
  （那会越出 A2 的范围，违反 CLAUDE.md 第 3 节）。相应地，A3 实现 `Settings`
  时这两项必须是 `str = ""` 或 `str | None = None`，**不得**声明为必填，
  否则全 fake 配置下应用起不来，直接违反 G-B 验收。
- 备选与放弃原因：往 compose 加 MinIO —— 越范围；且 E6/E7 真正需要它时，
  对接的很可能是线上对象存储而非本地 MinIO，现在加等于提前押注。
- 回退成本：低。E7（`import_pack.py`）需要真实存储时，补 compose 服务
  或填线上 endpoint 即可，不涉及代码结构变更。
- 影响范围：`.env.example`、A3 的 `core/config.py`、E6/E7。

## D-003 `core/money.py` 不定义 `Money` 类，改用 `MoneyLike` Protocol

- 日期：2026-09-15
- 背景：BACKLOG D8a 写的是「`Money` 值对象与 `core/money.py`」，CODING_STANDARDS
  第 3 节要求「格式化只在展示层，统一走 `core/money.py`」。但
  `interfaces/payments_base.py` 是**规范性文件**，它自己定义了 `Money`
  数据类，且 CLAUDE.md 第 4 节要求 B4 把它**原样复制**到
  `services/payments/base.py`、签名不得改动。若 `core/money.py` 也定义一个
  `Money`，代码库会出现两个结构相同但互不兼容的类型——跨层 `isinstance`
  判断会失败，且没人说得清该 import 哪一个。
- 选择：`core/money.py` **不定义 `Money`**。它只持有币种元数据
  （`MINOR_UNITS` / `CURRENCY_SYMBOLS`）、校验与格式化函数，全部面向一个
  `MoneyLike` Protocol 编程。`payments.base.Money` 结构上天然满足该 Protocol，
  无需任何 import。`Money` 这个类由 B4 从规范文件带入，唯一。
- 备选与放弃原因：
  1. `core/money.py` 定义 `Money`，B4 的 base.py 改为 import 它 ——
     违反「原样复制，签名不得改动」。
  2. 两处各定义一个，接受重复 —— 两个 frozen dataclass 结构相同但类型不同，
     是真实的 bug 温床。
  3. `core/money.py` 直接 import `services/payments/base.py` ——
     基础设施层反向依赖适配层，违反 ARCHITECTURE 第 1 节的分层规则。
- 回退成本：低。若将来确定要一个集中的 `Money`，把 Protocol 换成具体类、
  改 import 即可，所有函数签名不变。
- 影响范围：`core/money.py`、B4 的 `services/payments/base.py`、D8a。
- 保障：`tests/unit/test_money.py::test_normative_money_satisfies_moneylike`
  直接加载 `interfaces/payments_base.py` 并用真实的 `Money` 跑格式化，
  这个前提一旦不成立，在 A3 就红，不会拖到 B4 才发现。

## D-004 密钥声明为必填（无默认值），并在 `ENV=prod` 时校验签名密钥非空

- 日期：2026-09-15
- 背景：CONFIG_REFERENCE 第 9 节写明密钥「**永远不要有默认值**」，但没说
  缺失时应当如何表现。若声明成 `str = ""`，删掉 `.env` 里的一行不会有任何
  症状，系统会带着空密钥静默运行——空的 `JWT_SECRET` 意味着任何人都能伪造
  token。
- 选择：两层。
  1. 11 个密钥全部声明为**必填**（无默认值）。`.env` 里缺这一行 → 启动失败。
     这是对「永远不要有默认值」最字面的实现。
  2. 必填只能挡住「整行缺失」，挡不住 `JWT_SECRET=`（空值）。因此追加一条
     `ENV=prod` 时的校验：`JWT_SECRET` 与 `TELEGRAM_BOT_TOKEN` 必须非空白。
     dev/CI 全 fake 时允许为空，否则本地开发需要真凭证，与 G-B 验收冲突。
- 为什么只校验这两个：其余密钥是否必需取决于启用了哪些 provider
  （`AZURE_SPEECH_KEY` 只在 `SCORING_PROVIDER=azure` 时才需要）。
  那是 B5「registry 启动期能力自检」的职责，在此重复判断会形成两处真相。
- 备选与放弃原因：给密钥 `= ""` 默认值 —— 缺失与空值无法区分，
  且与 CONFIG_REFERENCE 的字面要求冲突。
- 回退成本：低。放宽只需给字段加默认值。
- 影响范围：`core/config.py`、`.env.example`、A5 的 CI 配置
  （CI 需要一份含全部 11 个键的 `.env`）。
- 附带：7 个真凭证字段用 `SecretStr`，使 `model_dump()` 与 `repr()`
  无法泄露它们；`AZURE_SPEECH_REGION` / `PAYWAY_BASE_URL` /
  `PAYWAY_MERCHANT_ID` / `GOOGLE_APPLICATION_CREDENTIALS` 是标识符、URL
  与文件路径，不是凭证，保持 `str`。

## D-005 初始 migration 创建 pgcrypto，但 downgrade 不删除它

- 日期：2026-09-15
- 背景：`DATA_MODEL.sql` 以 `CREATE EXTENSION IF NOT EXISTS "pgcrypto"` 开头，
  21 张表里所有 UUID 主键都用 `gen_random_uuid()` 作默认值，没有这个扩展
  一条都插不进去。autogenerate 不会生成扩展语句，必须手工补。
  但 downgrade 是否应该对称地 `DROP EXTENSION`，PRD 与 CODING_STANDARDS 都没写。
- 选择：upgrade 创建，**downgrade 不删除**，并在 migration 里注明原因。
- 理由：`IF NOT EXISTS` 的语义决定了我们无法区分「扩展是本次 migration 建的」
  还是「它本来就在」。删掉一个别人依赖的扩展，比留下一个没人用的扩展后果严重
  得多。这符合 CLAUDE.md 第 6 节「选最保守、最容易回退的方案」。
- 代价：downgrade 后数据库会残留一个未使用的扩展。无功能影响，
  下次 upgrade 时 `IF NOT EXISTS` 直接跳过。
- 回退成本：低。需要对称时在 downgrade 末尾加一行
  `DROP EXTENSION IF EXISTS pgcrypto`（不要加 CASCADE）。
- 影响范围：`alembic/versions/79333c6c4815_*.py`。
- 保障：`test_pgcrypto_is_created_by_the_migration` 确认 upgrade 后
  `gen_random_uuid()` 可用；`test_downgrade_removes_everything_and_upgrade_restores_it`
  确认 downgrade→upgrade 往返后 schema 指纹不变。

## D-006 模型暂不声明任何 `relationship()`

- 日期：2026-09-15
- 背景：ARCHITECTURE 与 DATA_MODEL 只规定表结构，未规定 ORM 关系。
  惯常做法是建表时顺手把 `relationship()` 都配上。
- 选择：**一个都不配**。等到某个查询真正需要时，在那里加，并显式指定
  加载策略（`selectinload` / `joinedload`）。
- 理由：关系不影响 schema（验收标准与它无关），但在 async session 下
  懒加载会抛 `MissingGreenlet`——这是 SQLAlchemy 异步用法最常见的事故。
  提前配一堆没人用的关系，等于提前埋下一批只在运行时才炸的地雷，
  而且每一条都需要单独想清楚加载策略才算配对。没有需求就没有正确答案。
- 备选与放弃原因：全量配上 `lazy="raise"` —— 能挡住意外懒加载，
  但仍是在为假想需求写代码，且 21 张表的关系图要维护。
- 回退成本：低。加关系是纯增量操作，不涉及 migration。
- 影响范围：`app/models/*`，以及 D 阶段写查询时的取数方式。

## D-007 四个规范性 `base.py` 从 ruff 豁免，改用逐字节比对测试守住

- 日期：2026-09-16
- 背景：CLAUDE.md 第 4 节要求 `interfaces/*.py` **原样复制**到
  `services/*/base.py`、**签名不得改动**；第 5.3 节要求 `make check` 全绿。
  B1 复制第一个文件后，两条规则直接冲突：ruff 对
  `scoring/base.py` 报了 41 个错误。
- 错误的性质（这决定了该让谁让步）：
  - **39 个 RUF002/RUF003**：中文全角标点 `，（）；`。该规则是为拉丁文本的
    同形字攻击设计的，对中文散文属于误报——这些标点在中文里就是正确写法。
  - **2 个 UP042**：建议把 `class Language(str, Enum)` 改为 `StrEnum`。
    这是**行为变更**，不只是风格：实测
    `str(Language.ZH_CN)` 现为 `'Language.ZH_CN'`，改用 `StrEnum` 会变成
    `'zh-CN'`，会静默改变所有日志与字符串插值的输出。
- 选择：在 `pyproject.toml` 的 `[tool.ruff] extend-exclude` 中**逐个列出**
  这四个文件路径，同时新增
  `tests/unit/test_provider_interfaces_are_verbatim.py`，对每个已复制的文件
  做 SHA-256 逐字节比对。
- 为什么这样不是开后门：这四个文件**不是我们的代码**，是规格产物，用我们的
  风格规则去格式化它们没有意义；而逐字节比对是比任何 lint 规则都强的约束——
  改动一个字符就红。豁免与守卫是一笔交易的两面，不能只做一半。
  mypy 仍然检查这些文件（它们是整个类型面的来源），未被豁免。
- 为什么逐个列而不用 `app/services/*/base.py` 通配：领域层
  （`mastery/` `media/` 等）将来若出现自己的 `base.py`，那是我们的代码，
  必须继续被 lint。
- 备选与放弃原因：
  1. 按 ruff 的建议改文件 —— 违反「签名不得改动」，且 UP042 那条会真的
     改变运行时行为。
  2. 全局关掉 RUF002/RUF003/UP042 —— 会让项目自己的中文注释与
     `str, Enum` 用法也失去检查。
  3. 用 `# noqa` 注释逐行标注 —— 那本身就是对文件的改动，同样违反原样复制。
- 回退成本：低。若将来决定让这些文件服从项目风格，删掉 exclude 条目、
  同步修改 `interfaces/` 源文件、重新复制即可——但那需要先推翻 CLAUDE.md
  第 4 节。
- 影响范围：`backend/pyproject.toml`、B1–B4 的四个 `base.py`。

## D-008 两个 Fake 的成本口径不同：FakeScorer 每次至少 1 美分，FakeTTS 短文本报 0

- 日期：2026-09-16
- 背景：`cost_usd_cents` 是整数，而两个 provider 的真实单价相差几个数量级。
  这导致同一个"合成成本"的做法在两处会得出相反的结论，若不写下来，
  B6 实现 `cost_ledger` 时会觉得其中一个是 bug。
- 事实依据：
  - **评分**：Azure 发音评测按 STT 计价，一次 4 秒录音约 0.1 美分，
    而调用频次是**每用户每次开口**。PRD 11.2 给 Basic 档定的月成本上限是
    45 美分，正是由这类调用累积而成。
  - **TTS**：约 $16/百万字符，一句话不到 0.002 美分；调用频次是
    **每内容条目一次，且只在打包时**。PRD 11.3 明确把这项列为「可忽略」。
- 选择：
  - `FakeScorer` 每次成功评测报 **1 美分**（保守取整向上），
    依据是 base 契约那句「未知时保守高估」。
  - `FakeTTS` 用整除 `字符数 // 625` 报告，短文本自然得 **0**，
    长文本才累积出非零值。
- 理由：这不是不一致，是两条不同的真实曲线。若 TTS 也每次向上取整到 1 美分，
  一个 10000 条目的内容包会显示花了 $100，与 PRD 11.3 的「可忽略」直接矛盾，
  而 `/admin/costs`（D5）会把这个假数字展示给人看。反过来若评分报 0，
  B6 的记账链路与 PRD 11.2 的成本护栏在全 fake 配置下就无法测试——
  而全 fake 是 CI 唯一跑的配置。
- 备选与放弃原因：两者统一报 0 —— 成本护栏失去测试路径；
  两者统一报 1 美分 —— 内容打包成本虚高千倍。
- 回退成本：低。两个常量都在各自 `fake.py` 顶部，各一行。
- 影响范围：B6（`cost_ledger` 记账与护栏）、D5（`/admin/costs`）。
- 注意：**这两个数字是测试替身的行为参数，不是业务阈值**，因此不进
  `CONFIG_REFERENCE`。判据是 CODING_STANDARDS 第 4 节那句「这个数字有没有
  可能因为线上数据而改变」——不会，真实 provider 落地后会报自己的真实成本，
  这两个常量随 fake 一起退场。
