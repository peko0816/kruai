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

## D-009 `FakeLLM` 只支持 JSON Schema 的一个子集，超出范围时明确失败而非猜测

- 日期：2026-09-16
- 背景：ARCHITECTURE 3.2 要求 `FakeLLM`「按 `json_schema` 生成结构合法的最小
  占位内容」，但没说要支持 JSON Schema 的哪些构造。完整实现整个规范
  （`allOf` 组合、条件式 `if/then`、`patternProperties`、远程 `$ref`…）
  对一个测试替身来说是本末倒置。
- 选择：实现一个够用的子集，**超出范围时抛
  `UnsupportedSchemaError`，由 `complete()` 转成
  `ok=False` + `error_code="llm.schema_unsupported"`，并在消息里点名是哪个
  构造、在哪个路径**。
- 已支持：`type`（含类型联合数组）、`properties` / `required`、
  `items` / `minItems` / `maxItems`、`minLength` / `maxLength`、
  `minimum` / `exclusiveMinimum`、`const`、`enum`、`anyOf` / `oneOf`（取首支）、
  本地 `$ref`（`#/...`，含 `~0`/`~1` 转义）。
- 未支持（会明确失败）：远程 `$ref`、元组形式的 `items`、`allOf`、
  `if/then/else`、`patternProperties`、`additionalProperties` 作为 schema。
- 理由：base 契约写明「`json_schema` 非空时保证返回可解析，否则 `ok=False`」。
  对不认识的构造**静默产出一个不满足 schema 的值**，恰好是最难发现的那种
  违约——E3 会拿它继续跑，E4 校验时报出一堆看似内容问题的错误，
  真正的原因藏在两层之外。明确失败并点名构造，五秒就能定位。
- 对 E3 的约束：写 `generate.py` 的生成 schema 时，若用到上面「未支持」列表里的
  构造，全 fake 配置下管线会停在那一条并给出明确原因。届时要么改 schema，
  要么给这个 fake 补上那个构造——两条路都比静默产出错数据好。
- 备选与放弃原因：
  1. 遇到不认识的构造就返回 `{}` 或 `None` —— 静默违约，见上。
  2. 引入 `jsonschema` 的实例生成能力到**应用代码** —— 该库只做校验不做生成，
     且让运行时代码依赖一个纯测试用途的库不值当。
- 回退成本：低。补一个构造就是 `minimal_instance` 里加一个分支。
- 影响范围：`app/services/llm/fake.py`、E3（`generate.py` 的 schema 设计）。
- 保障：`tests/unit/test_llm_fake.py` 用 **`jsonschema` 这个独立校验器**
  验证产出确实满足 schema，而不是断言我们以为的形状——后者会和误解一起通过。
  已做变异验证：忽略 `minItems` 或 `minLength` 都会被校验器抓出
  "is too short"。

## D-010 支付 Fake 的 HMAC key 公开写在源码里，并由 registry 禁止其在 `ENV=prod` 下构建

- 日期：2026-09-16
- 背景：ARCHITECTURE 3.2 规定 `FakePaymentProvider`「用**固定 HMAC key** 验签」。
  但 R5 写的是「不得硬编码任何密钥」。这两条表面冲突，需要判断。
- 判断：这不是密钥。签名方案在这里有意义的前提，就是**测试能用它签出有效签名**——
  一个测试拿不到的 key，等于 `verify_callback` 的成功分支永远测不到。
  它是公开的测试夹具，和 D-001 里 compose 的 `kruai_dev` 同类。
  R5 真正要防的是**生产凭据进 git**，而这个 key 在生产中没有任何价值。
- 但由此产生一个真实且严重的风险：**若有人用 `PAYMENT_PROVIDERS=fake`
  部署到生产**，任何能读这个仓库的人都可以伪造一个签名有效的回调，
  给自己开通订阅。这不是「fake 在生产里没用」，是「fake 在生产里可被利用」——
  与另外三个 fake 的性质完全不同（评分/TTS/LLM 的 fake 在生产中只是产出垃圾，
  不能被用来获利）。
- 选择：
  1. key 公开写在 `fake.py` 顶部，命名为 `FAKE_HMAC_KEY`，注释说明它是
     夹具而非凭据，且真实 adapter 必须从 `Settings` 读自己的 key。
  2. 同时导出 `sign_payload()`，让测试与 D8a 的 webhook 集成测试能构造
     真实回调，而不是各自重新实现签名方案。
  3. **`registry.build_payment_provider()` 在 `settings.env == "prod"` 时
     拒绝构建 `fake` 与 `fake_manual`**，错误信息点明原因与修法。
- 为什么守卫放在 B4 而不是留给 B5：B5 是通用的「启动期能力自检」，
  检查的是 provider 能否覆盖所需能力。这一条不是能力问题，是**这一个适配器
  特有的安全属性**，写在它自己的 registry 里，位置与风险一致。
- 备选与放弃原因：
  1. key 从 `Settings` 读 —— 要新增一个 `CONFIG_REFERENCE` 条目（R3），
     且 `.env.example` 里必须填一个非空值否则 fake 不可用，反而更像是在
     鼓励把它当真凭据管理。
  2. 只写注释不加守卫 —— 注释挡不住误配置，而这个误配置的后果是免费订阅。
- 回退成本：低。放宽只需删掉守卫的三行。
- 影响范围：`app/services/payments/{fake,registry}.py`、D8a（webhook 测试
  可直接用 `sign_payload`）、部署配置。
- 保障：已做变异验证——去掉守卫会让 3 个测试转红；验签失败仍解出数据会让
  「拒绝的回调不得携带可用数据」转红。

## D-011 「所需语言/音色」由 PRD 1.3 的 V1 范围声明，不做成配置项

- 日期：2026-09-16
- 背景：B5 要求「检查所配 provider 是否覆盖**所需语言**」，验收是
  「故意配错 → 启动报错并指明缺哪个语言」。但「所需」是谁定的？
  `CONFIG_REFERENCE` 里没有语言清单。
- 选择：在 `selfcheck.py` 里以模块常量声明，不新增配置项。
  - `REQUIRED_LANGUAGES = (Language.ZH_CN,)`
  - `REQUIRED_VOICES = (KM_NARRATOR, KM_FEEDBACK, ZH_MODEL)`
- 理由：R3 要求进配置的是**阈值、权重、限额**——那些会因为线上数据而改变。
  而「V1 只做中文」是 PRD 1.3 定死的产品范围，不是运维可调的旋钮：
  **没有英文内容包，任何部署都无法服务英文**，给它一个配置开关等于暗示
  它可调，而调了也没用。同理 `EN_MODEL` 音色在 v2 之前没有意义。
  v2 接入英文时，这两行与英文内容包一起改，是同一次变更的两个部分。
- 备选与放弃原因：
  1. 新增 `REQUIRED_LANGUAGES` 配置项 —— 需走 CONFIG_REFERENCE 全流程，
     且新增一个 V1 内不会有人改、改了也无效的键。
  2. 要求覆盖 `Language` 枚举的全部成员 —— 会让所有只配中文的正常部署失败。
  3. 从数据库里已导入的 content pack 推导 —— 启动期查库、耦合过重，
     且空库会推导出「什么都不需要」，检查形同虚设。
- 回退成本：低。若将来确需按部署调整，加配置项并让常量作为默认值即可。
- 影响范围：`app/services/selfcheck.py`、v2 英文接入。

## D-012 引入 `ProviderConfigurationError`（继承 `ValueError`），供自检精确捕获

- 日期：2026-09-16
- 背景：B1–B4 的四个 registry 都抛 `ValueError`（按 CODING_STANDARDS 5.1
  第三类「编程/部署错误抛原生异常」）。B5 要捕获并**聚合**这些错误，
  但 `except ValueError` 会同时吞掉 provider 工厂内部的真实 bug——
  于是一个代码缺陷会被报成「部署配置有误」，把值班的人送去错误的文件。
- 选择：新增 `app/services/provider_errors.py`，定义
  `ProviderConfigurationError(ValueError)`，四个 registry 的 9 处抛出点全部改用它。
- 为什么继承 `ValueError` 而不是 `Exception`：现有 15 个测试写的是
  `pytest.raises(ValueError)`，继承使它们**一字不改仍然正确**；
  同时语义上它确实是「值不合法」。自检则捕获精确类型。
- 为什么不放进 `core/errors.py`：那个文件的 docstring 已写明只收
  `AppError` 子类（业务规则未满足），并解释了为何不收 ConfigurationError。
  provider 配置错误是第三类，不是业务规则。
- 为什么现在才抽：B1 时只有一处，按「三行重复胜过过早抽象」保持了 `ValueError`。
  到 B5 才出现**功能性需求**（聚合器必须区分两类异常），此时抽取有明确理由。
- 回退成本：低。该类只有一个定义点，改回 `ValueError` 是机械替换。
- 影响范围：四个 `registry.py`、`selfcheck.py`。
- 保障：已做变异验证——把捕获放宽成 `except Exception` 会让
  `test_a_genuine_bug_in_a_factory_is_not_reported_as_misconfiguration` 转红。

## D-013 `cost_ledger` 写入使用独立 session，不加入调用方事务

- 日期：2026-09-16
- 背景：ARCHITECTURE 2.1 的 attempt 链路是
  `额度校验 → scoring → cost_ledger → attempts 落库 → mastery → 复习队列`。
  最自然的实现是让记账加入同一个事务。PRD 与 ARCHITECTURE 都没说该不该这样。
- 选择：`CostLedger._persist()` **自己开一个 session 并 commit**，
  与调用方的业务事务完全无关。
- 理由：**钱已经花掉了，与业务事务提没提交无关。**
  若共用事务，业务回滚会连带删掉记账行——而回滚恰恰发生在出问题的请求上，
  也就是最需要留痕的那些。这会把 CLAUDE.md 第 8 节的规则反过来实现：
  「没有记账的调用等于没有发生过」变成「出错的调用自动变成没发生过」。
  ARCHITECTURE 第 5 节说 scoring 失败时「不扣额度、不写 attempts」——
  但没说不记成本，因为超时往往在请求已发出之后，钱照付。
- 附带的同类决定：`external_call` 用 `try/finally`，**异常传播时仍然落盘
  已记录的成本**。调用已经发生，之后抛出的异常是另一个问题，不该抹掉花钱的证据。
- 代价：每次外部调用多一次数据库往返。相对于外部调用本身（100ms 起）可忽略。
- 备选与放弃原因：
  1. 共用调用方 session —— 见上，会丢掉最该留的记录。
  2. 先写记账再提交、后续business 另开事务 —— 等于把顺序耦合塞给每个调用方，
     一旦有人写反就静默失效。
- 回退成本：低。`_persist` 是唯一写入点，改为接收外部 session 即可。
- 影响范围：`app/services/cost_ledger.py`、D3（attempt 链路）、D5（成本看板）。
- 保障：两个集成测试直接验证——业务事务回滚后记账行仍在、
  记账后抛异常仍然落盘。变异验证：去掉 `commit` 或去掉 `finally` 都会转红。

## D-014 `unit` 与 `ref` 用 `Literal` 而非自由字符串

- 日期：2026-09-16
- 背景：`DATA_MODEL.sql` 把 `cost_ledger.unit` 与 `ref` 定义为 `TEXT`，
  并在注释里列出取值（`seconds / tokens / calls / minutes`、
  `attempt / realtime / content_production`）。数据库层不约束。
- 选择：Python 侧声明为 `Literal[...]`，与 DDL 注释逐字一致。
- 理由：`/admin/costs`（D5）要按这两列聚合。一个拼错的 `"call"`（少个 s）
  会安静地产生一个永远聚合不进任何分组的孤儿行——查账时看到的总额偏低，
  而没有任何地方报错。类型检查把这类错误提前到写代码时。
  不在数据库加 CHECK 约束，是因为新增取值（比如 v2 的新计量单位）
  不应该需要一次 migration。
- 回退成本：低。改回 `str` 即可。
- 影响范围：`app/services/cost_ledger.py`、D5。

## D-015 SM-2 中间地带（`MASTERY_LOW` ≤ mastery < `MASTERY_HIGH`）保持原状

- 日期：2026-09-16
- 背景：PRD 9.2 的间隔重复只写了两条规则：
  `mastery >= 80 → interval *= ease_factor`、
  `mastery < 60 → interval = 1 day, ease_factor -= 0.2`。
  **60 到 80 之间两条都不触发**，PRD 没说该怎么办。
- 选择：`interval` 与 `ease_factor` **都不变**，`next_due_at = now + 当前 interval`，
  即 concept 按原有节奏再来一次。返回的 `band` 标记为 `"held"`。
- 理由：这是对规格最字面的读法——没有规则触发，就什么都不改。
  学习者既没有表现出掌握、也没有表现出遗忘，维持当前间距是唯一不引入
  新判断的做法。
- 备选与放弃原因：
  1. 中间地带也重置 `interval` —— 等于把「没掌握好」当成「失败」，
     会让 60–79 分的学习者永远停在每日复习，与 `MASTERY_LOW` 这条线的存在
     自相矛盾（既然划了线，线上和线下就该不同）。
  2. 按 mastery 在区间内的位置做插值 —— PRD 没有任何依据支持某个插值公式，
     而一个编出来的公式会在 M0 校准时无从判断对错。
  3. 中间地带不重新调度（不更新 `next_due_at`）—— 那样 concept 会永远停留在
     已过期状态，每次复习队列都把它排在最前，实际效果是每日复习。
- 回退成本：低。三个分支集中在 `schedule_review` 一个 if/elif/else 里。
- 影响范围：`app/services/mastery/sm2.py`、C3 的复习队列排序。
- 保障：`test_the_middle_band_holds_everything_where_it_is` 与边界参数化测试
  覆盖 79.9 / 60.0 两侧。变异验证：把中间地带改成重置会转红。

## D-016 `interval` 增长用向上取整（`ceil`），而非 `floor` 或 `round`

- 日期：2026-09-16
- 背景：`interval *= ease_factor` 产出小数，而 `concept_mastery.interval_days`
  是 `INTEGER`。PRD 未指定取整方式。
- 选择：`math.ceil`。
- 理由：这不是风格问题，另外两种取整都有一个**静默失效**的陷阱。
  `SM2_EASE_MIN` 是 1.3，而 `floor(1 × 1.3)` 与 `round(1 × 1.3)` **都等于 1**：

  ```
  floor  [1, 1, 1, 1, 1, 1]   ← ease 触底后永远卡在每日复习
  round  [1, 1, 1, 1, 1, 1]   ← 同上
  ceil   [1, 2, 3, 4, 6, 8]
  ```

  也就是说：一个 ease 已衰减到下限的学习者，**即使每次都通过，也要天天
  复习同一个 concept，永远不会拉开**。没有任何报错，调度就是不工作了。
  向上取整是三者中唯一保证「通过就一定拉开」的。
- 代价：间隔比向下取整长一些（ease=2.5 时 `1,3,8,20,50,125`
  对比 `1,2,5,12,30,75`）。前几次差 1–3 天，之后相对于以周计的间隔可忽略。
- 备选与放弃原因：
  1. `floor` / `round` —— 见上，最需要帮助的学习者反而被卡死。
  2. `max(floor(interval × ease), interval + 1)` —— 能保证增长，但表达的是
     同一个意图而多一层包装，`ceil` 已经蕴含它。
- 回退成本：低。`_grow()` 一个函数一行。
- 影响范围：`app/services/mastery/sm2.py`。
- 保障：`test_a_concept_at_minimum_ease_still_spaces_out` 与
  `test_every_extension_moves_by_at_least_a_day` 直接断言这条性质。
  变异验证：换成 `floor` 或 `round` 各有 4 个测试转红。

## D-017 复习队列只收已到期的 concept，且时长预算是目标而非硬上限

- 日期：2026-09-16
- 背景：PRD 3.2 说「复习队列按 mastery 升序 + 间隔重复到期时间排序」，
  第 10 节的端点注释是「弱项优先 + 到期复习」。但两件事没写清楚：
  未到期的 concept 要不要进队列？以及单个 concept 比整个时段还长时怎么办？
- 选择一：**只收已到期的**（`next_due_at <= now`，`NULL` 视为已到期）。
  - 理由：若把全部 concept 按 mastery 排序，五分钟前刚做过的 concept
    会因为 mastery 还很低而排在最前——**这等于把间隔重复关掉了**，
    而队列看上去依然完全合理。排期决定「什么时候」，队列只在排期已经
    放行的范围内决定「先做哪个」。
  - `next_due_at IS NULL` 视为已到期并排在最前：那意味着「做过但从未排期」，
    是最被忽视的状态，应该浮出来而不是永久隐形。
- 选择二：**时长预算是目标，不是硬上限**。若排在最前的 concept 本身就比
  整个时段长，仍然返回它（`over_budget` 标记为真）。
  - 理由：一个有二十个逾期 concept 的学习者，因为每个要六分钟而他选了五分钟，
    就被告知「没有需要复习的」——这比一次略长的练习糟糕得多。
- 选择三：排序的最后一级用 `concept_id` 兜底。
  - 理由：没有它，mastery 与到期时间都相同的两个 concept 会按输入顺序返回，
    学习者刷新一次列表就重排一次。这不报错，也不像 bug。
- 备选与放弃原因：
  1. 收录全部 concept 按 mastery 排序 —— 见上，等于关掉间隔重复。
  2. 严格不超预算 —— 长 concept 的用户会看到空队列，功能静默失效。
  3. 按「最佳装填」重排以塞满时段 —— 会让较强的短 concept 插到较弱的长
     concept 前面，直接违反「弱项优先」。
- 回退成本：低。三处都集中在 `review.py` 的两个函数里。
- 影响范围：`app/services/mastery/review.py`、D4 的 `GET /review/queue`。
- 保障：六个变异（降序、去掉兜底排序、不过滤未到期、硬上限、到期判定用 `<`、
  未排期排最后）各自都会让测试转红。
- 附带说明：**每个 concept 的预计时长是调用方传入的，不在这里计算。**
  复习一个 concept 要多久取决于它挂了多少 drill/vocab item，那在
  `lesson_items` 里，不是领域层该 join 的。D4 接入时先给一个固定值即可，
  将来换成真实估算不需要改这个模块。

## D-018 `entitlements` 是唯一触碰数据库的领域层服务

- 日期：2026-09-17
- 背景：ARCHITECTURE 第 1 节把 `services/entitlements` 归入**领域层**，
  并规定该层「纯逻辑，不碰 IO，可完全单测」。但 BACKLOG C4 与
  CODING_STANDARDS 第 8 节同时规定额度扣减**必须原子更新**
  （`UPDATE ... WHERE remaining >= n`），禁止读-改-写。
- 冲突：这两条无法同时满足。原子性的全部含义就是**判断与写入是同一条 SQL
  语句**；把判断抽成纯函数、写入放到别处，恰好就是那条规则要防的读-改-写。
- 选择：**让 IO 规则让步**。`Entitlements` 持有 session factory，
  每个扣减是一条带 `WHERE` 守卫的 `UPDATE ... RETURNING`，
  受影响行数为 0 即拒绝。模块 docstring 与本条记录说明原因。
- 为什么是这个方向：分层规则的目的是「可完全单测」，而这里恰恰**不能**
  用单测证明正确性——读-改-写的实现能通过任何单线程测试，只在并发下静默
  超扣。实测：把实现换成读-改-写后，10 个并发扣减只记录了 **3 次**
  （丢失更新），而限额为 10 时 **20 个并发请求全部通过**。
  换言之，遵守 IO 规则会让唯一能发现这个 bug 的测试无法存在。
- 备选与放弃原因：
  1. 纯函数判断 + 仓储层写入 —— 就是被禁止的读-改-写，见上。
  2. 用 `SELECT ... FOR UPDATE` 显式加锁 —— CODING_STANDARDS 允许这种写法，
     但它需要调用方自己管理事务边界，一旦有人忘记就退化成读-改-写且无提示；
     `UPDATE ... WHERE` 把正确性焊死在语句里，没有用错的方式。
- 回退成本：低。若将来确需纯逻辑版本，可保留 `Entitlements` 作为适配层、
  另抽一个只做限额换算的纯函数模块——但扣减本身不能离开数据库。
- 影响范围：`app/services/entitlements/`、D3（attempt 链路）、
  D5（`GET /me/entitlements`）、D8a（付费墙）。
- 保障：五个变异全部被测试抓住，其中「换成读-改-写」是决定性的一个。
- 附带纪律：`QuotaSnapshot` 明确标注**只可用于展示**。
  读它再据此判断，就是这个模块存在的理由所要消灭的读-改-写——
  只有 `consume_*` 方法是权威的。

## D-019 额度重置在 DST 边界上依赖 Python 的 `fold=0` 默认语义，并用测试钉死

- 日期：2026-09-17
- 背景：C5 要求按 `user_profiles.timezone` 的本地 `QUOTA_RESET_HOUR_LOCAL`
  重置。默认重置时刻是**本地午夜**，而本地午夜在两种情况下不是一个普通时刻：
  - **不存在**：`America/Santiago`（2026-09-06）、`Asia/Beirut`（2025-03-30）
    等时区在午夜切换夏令时，时钟从 23:59 直接跳到 01:00，00:00 从未发生。
  - **出现两次**：`America/Havana`（2026-11-01）午夜回拨，00:00 出现两遍。
- 需要的语义：
  - 不存在时 → 在时钟**跨过**名义时刻后的第一个真实瞬间重置（否则额度当天不返还）
  - 出现两次时 → 取**第一次**（早重置好过晚重置，学习者不该白等一小时）
- 实测结论：**Python 的默认 `fold=0` 在两种情况下都恰好给出上述语义。**
  - 不存在时 `fold=0` 用切换前的偏移解析，得到的瞬间正是间隙之后（本地 01:00）
  - 出现两次时 `fold=0` 是较早的那次
- 选择：**依赖这个默认，代码里不出现 `fold` 字样**，但在
  `tests/unit/test_entitlements_reset.py` 里用**真实时区与真实日期**
  把两种行为都钉死。
- 为什么必须钉死：正因为代码里看不到 `fold`，后人完全可能"顺手"加上
  `fold=1` 处理（那看起来像是在"更正确地处理 DST"），而行为会静默改变——
  重复的午夜会晚一小时重置，不存在的午夜会提前到间隙之前。
  变异验证：把 `_local_reset_on` 改成 `fold=1`，5 个测试转红。
- 为什么用真实时区而非构造的假时区：这里依赖的是 **Python/tzdata 的行为**，
  用编造的时区只会测到测试自己。
- 回退成本：低。若将来需要显式处理，在 `_local_reset_on` 一处改即可，
  测试会立刻告诉你行为变了。
- 影响范围：`app/services/entitlements/reset.py`、D3、D5。
- 附带更正：本模块 docstring 最初写的是「给 datetime 加 timedelta 会在
  DST 日错一小时」——**这句话是错的**，变异测试暴露了它。对 `zoneinfo`
  的 aware datetime 加 timedelta 是**墙钟加法**且会重新解析偏移，
  与按日历日重建等价。真正的陷阱是**先转成 UTC 再加 24 小时**，
  已改正并由 `America/New_York`（02:00 切换）的 23h/25h 测试覆盖。

## D-020 分组用 SHA-256 而不是内置 `hash()`，桶宽 48 位

- 日期：2026-09-17
- 背景：BACKLOG C7 把分组写成 `hash(user_id + key)`，验收是
  「同一用户多次分组结果一致」。
- 冲突：**Python 内置 `hash()` 恰好不满足这条验收。** PEP 456 对 str/bytes
  的哈希按进程随机加盐，实测同一输入在三个进程里分别落到 747 / 208 / 399：

  ```
  $ for i in 1 2 3; do PYTHONHASHSEED=$i python3 -c \
      "print(hash('explain_media:3f2504e0-...') % 1000)"; done
  747
  208
  399
  ```

  生产跑多个 uvicorn worker。同一学习者这次看到视频、下次看到音频，
  完课数记在最后应答的那个 worker 的臂上，**不报错、不留日志**，
  而 PRD 7.2 要拿这组数字决定一笔 $375-1,500 的投入。
- 选择：`hashlib.sha256(f"{key}:{user_id}")`，取前 **6 字节**（48 位）除以 2^48。
  - **key 进摘要**：只哈希 user_id 会让同一批人永远落在每个实验的实验组，
    两个同时在跑的 A/B 会互相测量。
  - **key 在前、uuid 在后**：UUID 是定长 36 字符的固定字母表，
    所以分界点与 key 的内容无关，不需要转义。
  - **48 位而不是 64 位**：float64 只有 53 位尾数，`(2**64-1)/2**64`
    会舍入成**恰好 1.0**，持有该摘要的用户会掉出一个号称 100% 的放量。
    48 位下每个分数都精确，最大值 0.9999999999999964。
- 备选与放弃原因：
  1. 内置 `hash()` —— 见上，直接不满足验收。
  2. `PYTHONHASHSEED` 固定成常数 —— 把正确性押在部署环境变量上，
     少设一次就静默退化，且 CPython 不承诺跨版本稳定。
  3. MD5/CRC32 —— 更快，但这里一次调用的成本本来就可以忽略，没有理由
     选一个更弱的。
- 回退成本：**高，且随时间上升**。换哈希会把所有**尚未分组**的用户重新洗牌；
  实验跑到一半换，两臂就混了。已分组的用户不受影响（见 D-023）。
  三个 golden 向量钉死了当前取值，改动会让测试转红而不是让数字悄悄漂移。
- 影响范围：`app/services/experiments/bucket.py`。
- 保障：稳定性测试在三个不同 `PYTHONHASHSEED` 的子进程里跑真实函数；
  另有一个测试证明内置 `hash()` 通不过同一检查。

## D-021 `EXPERIMENT_*_SPLIT` 是实验组的比例，比较取严格小于

- 日期：2026-09-17
- 背景：CONFIG_REFERENCE 第 8 节只给了 `EXPERIMENT_EXPLAIN_MEDIA_SPLIT = 0.5`，
  没说这 0.5 是哪一臂的比例。0.5 时无所谓，任何其他取值都有所谓。
- 选择：**实验组（video）的比例**。
- 理由：配错的方向要便宜。视频是待验证且**花钱**的那一臂
  （PRD 7.2：$375-1,500 一次性）。若 split 表示音频比例，
  `0.0` 就意味着全员视频——一个手滑把最贵的路径全量放开。
  按当前定义，`0.0` 意味着没有人进视频，与 `ENABLED=false` 的默认方向一致。
- 附带选择：比较用 `fraction < split`，不是 `<=`。这才让两个端点名副其实：
  `0.0` 时确实无人进实验组（否则摘要恰为 0 的用户会进一个号称 0% 的放量），
  `1.0` 时确实全员进（由 48 位桶宽保证，见 D-020）。
- 回退成本：低，但**只在实验开跑前**。已写入的分组不会因为定义改变而移动
  （见 D-023），所以跑到一半改这个定义会得到一张两种语义混在一起的表。
- 影响范围：`app/services/experiments/bucket.py`、`docs/CONFIG_REFERENCE.md`。
- 保障：`<=` 变异由「分数恰好等于 split 的学习者属于对照组」一条捕获——
  两万人的分布测试抓不到它，因为没有人的分数恰好等于 split，
  所以测试把 split 设成某个已知学习者自己的分数。

## D-022 实验关闭时返回 `None` 且不写任何行

- 日期：2026-09-17
- 背景：`EXPERIMENT_EXPLAIN_MEDIA_ENABLED` 默认 `false`（S2 之后才开）。
  关闭时 `assign()` 该返回什么、要不要落库，PRD 未定义。
- 选择：返回 `None`，**既不读也不写**；已有的行保留不动。
- 理由：
  - **返回 `None` 而不是对照组**：C6 的 `media.resolve()` 把 `None` 读作
    「A/B 没在跑，只按资格判定」。给它 `audio` 会让付费用户拿不到视频，
    而没有任何地方记录原因。
  - **不写行**：实验关着时写下的分组，是没有任何人被实际暴露过的分组。
    事后它和真实分组**无法区分**，只会给某一臂的分母灌水。
  - **关掉就是真的关掉**：如果关闭时仍返回已存行，一个被分到音频的 Pro 用户
    会在实验显示「已关闭」的情况下继续拿不到视频，且无从排查。
- 回退成本：低。集中在 `choose_variant` 的第一个分支。
- 影响范围：`app/services/experiments/bucket.py`、D2。

## D-023 `experiments` 也持有 session factory，但只有写入那一半

- 日期：2026-09-17
- 背景：ARCHITECTURE 第 1 节把 `services/experiments` 列为**领域层**
  （「纯逻辑，不碰 IO」），而 BACKLOG C7 要求写入 experiments 表。
  与 D-018 是同一处冲突。
- 选择：**比 D-018 更窄的例外**。分组规则全部在 `bucket.py`，纯函数、
  无 session、无时钟、完全可单测；只有「把结果写下来」在 `assign.py`。
  D-018 里判断与写入必须是同一条 SQL，什么都拆不出来；这里拆得干净，
  所以只让能让的那一半让步。
- 为什么表不是缓存：**已存的行优先于任何重算。**
  分组是确定性的，看起来重算即可。但把
  `EXPERIMENT_EXPLAIN_MEDIA_SPLIT` 从 0.5 改到 0.7，重算式实现会把
  五分之一的学习者从音频悄悄搬到视频，**并带着他们此前的学习记录**。
  此后完课率是「两臂都待过的人」的平均值，PRD 7.2 的 15% 门槛
  就是拿一个不存在的样本去比。换哈希、改 bug、手工订正，同理。
  所以一旦分了组，行就是答案。
- 并发：`INSERT ... ON CONFLICT DO NOTHING`，不加锁升级。
  每个竞争者算出的 variant 相同，输掉的一方没有损失——
  这正是 quota 那条读-改-写禁令**不**适用于此的原因。
  输的一方回读赢家的行（即使部署中途两边 split 不同，也以先落库的为准）。
- 回退成本：低。
- 影响范围：`app/services/experiments/assign.py`。
- 保障：八个变异全部转红，包括「忽略已存行改为重算」与「去掉 ON CONFLICT」。
- 附带发现：**`asyncio.gather` 十个并发 `assign()` 实际上没有重叠。**
  加探针后发现十次里有九次在前置 SELECT 就读到了已提交的行，
  也就是说冲突分支根本没被走到——去掉 `ON CONFLICT` 的变异一开始
  逃过了测试。改成由一个未提交的竞争行**强制**造出冲突，才真正钉住这条路径。
  并发测试写了不等于测到了。

---

# 遗留约束

**这些不是决策，是已知的、尚未处理的约束。** 每一条都在未来某个具体任务上生效，
现在没有症状，但到那时若没人记得，就会变成 bug。

列在这里是为了让它们跟着仓库走——不依赖任何人的记忆，也不依赖某一次对话。
每条在代码里对应位置另有一份注释，两处都放是因为：只看文档的人看不到代码注释，
只改代码的人不会翻文档。

| # | 约束 | 何时生效 | 代码内注释位置 |
|---|---|---|---|
| L-1 | `COST_ALERT_MULTIPLIER` 是唯一接触金额的浮点数。它是比率不是金额，不违反 R4，但它驱动的比较不精确：`45 × 1.5 = 67.5`，67 到底算不算超标需要显式决定。**在调用处显式取整，不要让浮点序关系替你决定。** | 实现 PRD 11.3 的成本护栏（「超过档位上限 1.5 倍时告警并自动限流」）时 | `core/config.py` 的 `cost_alert_multiplier` 字段 |
| L-2 | CI 里**没有 Redis service**。目前没有任何测试连它，所以无所谓。第一个连 Redis 的测试出现时必须在 workflow 里补上，否则它会静默跳过或失败——而「静默跳过的测试」正是这个 workflow 已经专门防范的失败形态。 | 第一个依赖 Redis 的测试（RQ 任务、缓存） | `.github/workflows/ci.yml` 的 `services:` 段 |
| L-3 | `REALTIME_PROVIDER` **未纳入启动自检**。目前没有 realtime registry，没有东西可以校验它，配错了今天既无症状也无后果。该适配器落地时要在 `selfcheck.py` 补一个 `_realtime_problems()`，否则一个错值会一路走到第一次 Pro 实时会话。 | BACKLOG F2（M3 实时语音代理） | `services/selfcheck.py` 的 `_llm_problems` 上方 |
| L-4 | `docker-compose.yml` 把两个数据存储绑在 `0.0.0.0`，且 **Redis 完全没有密码**。仓库转 private 只解决 PostgreSQL 那一半（凭据不再公开），Redis 的暴露面与仓库可见性无关——同局域网内任何人都能直连。修法是绑回 loopback：`ports: ["127.0.0.1:6379:6379"]`。已与项目所有者确认**暂缓**。 | C 阶段 Redis 开始承载真实数据时 | 见 D-001；`docker-compose.yml` |
| L-5 | `OBJECT_STORAGE_ENDPOINT` 与 `PUBLIC_MEDIA_BASE_URL` 仍为空，**`Settings` 中必须保持可选**。声明为必填会让全 fake 配置启动失败，直接违反 G-B 验收。 | BACKLOG E6 / E7（真实对象存储） | 见 D-002；`core/config.py` |
| L-6 | `concept_mastery.ease_factor` 在 DDL 里默认 **2.5**，而 `SM2_EASE_INITIAL` 是配置项，当前也是 2.5。**两者会漂移**——改了配置，靠数据库默认值插入的新行仍然是 2.5。写入方必须显式带上 `initial_ease_factor(settings)`，不要依赖 DDL 默认值。 | D3 写入 `concept_mastery` 时 | `services/mastery/sm2.py` 的 `initial_ease_factor` |

**处理完一条就把它从这张表里删掉**，并在对应的代码注释里说明已解决——留着一条已经不成立的约束，比没有这张表更糟。
