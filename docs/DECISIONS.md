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

## D-024 鉴权放 `core/security.py`，开户放 API 层，不新建 service 包

- 日期：2026-09-18
- 背景：D1 需要三样东西：验签、签发 JWT、把一个 Telegram 账号变成
  `users` 行。PRD 15.1 把 `core/` 写成「配置、鉴权、日志」，但没说开户归谁。
- 选择：
  - 纯密码学（initData 验签、JWT 签发与校验）→ `app/core/security.py`，不碰 IO，全单测；
  - 开户（`users` / `user_profiles` / `entitlements` 三行）→ `app/api/v1/auth.py`。
- 理由：
  - **不新建 `services/accounts/`**：G-C 的 `test_every_package_under_services_is_classified`
    要求 `services/` 下每个包非领域即适配。开户要连库，塞进领域层就是
    D-018 之后的**第三个**例外——例外多到第三个，规则本身就不成立了。
  - **API 层不写业务判断，但开户不是判断**。额度够不够、这一轮算不算过、
    该给视频还是音频，这些都是判断，仍然在领域层。
    「把已验签的身份落成一行」没有可判断的东西。
  - 将来 D6 的 Bot 也不需要自己开户：它走这个端点。
- 回退成本：低。若日后确实出现第二个开户入口，把
  `provision_account` 原样搬到 `services/accounts/` 并在分层测试里登记即可。
- 影响范围：`app/api/v1/auth.py`、`app/core/security.py`。

## D-025 JWT 只携带身份，1 小时过期，不做 refresh token

- 日期：2026-09-18
- 背景：token 里放什么、活多久，PRD 第 10 节只写了「签发 JWT」。
- 选择：claims 只有 `sub` / `iss` / `iat` / `exp`。TTL 走
  `JWT_ACCESS_TOKEN_TTL_SECONDS`，默认 3600。没有 refresh token。
- 理由：
  - **不放 plan**：套餐在 token 有效期内会变（付款、grace 到期、降级）。
    一个写着 `plan=pro` 的 token 是**任何撤销都够不到的权益**。
    每次请求从库里读，慢一点，但只有一个真相。
  - **不放 locale / quota**：同理，而且 `user_profiles` 本来就要读。
  - **不做 refresh token**：客户端手里一直有 initData，重新换发是一次
    HTTP 往返，没有交互成本。refresh token 要另配一套存储、撤销与轮换，
    为一个能免费重来的动作加一整套状态机不划算。
  - **`iss` 钉死为 `kruai` 且解码时校验**：共用密钥的另一个服务签出的 token
    不能在这里花掉。它不是配置项——改它等于让所有在用会话立刻失效。
- 回退成本：低。加 claim 是加法；TTL 已是配置项。
- 影响范围：`app/core/security.py`、D2 之后所有受保护端点。
- 保障：变异 F（去掉 `issuer=`）、变异 G（去掉 `require`，即允许无 `exp` 的
  永久 token）各转红一条。

## D-026 密钥缺失或过短时抛 `ValueError`（500），而不是拒绝登录（401）

- 日期：2026-09-18
- 背景：`ENV=dev` 的 `TELEGRAM_BOT_TOKEN` 与 `JWT_SECRET` 按 D-004 是空的
  （CI 必须能在无凭据下跑通）。那么空密钥下来了一个 initData，该怎么办？
- 选择：抛 `ValueError`，走 500。**不是** `AuthenticationFailed`。
  同时给 `JWT_SECRET` 加 32 字符下限（RFC 7518 3.2），
  `ENV=prod` 在启动期校验，`_signing_secret` 在签名点再校验一次。
- 理由：
  - **空密钥不是「验不过」，是「验不了」**。HMAC 用空 key 照样算出一个
    合法摘要——一个**任何人都能复现**的摘要。若返回 401，症状是
    「所有人都登不上」；若不拦，症状是「所有人都能伪造」。两者都必须
    区别于真实的伪造尝试，因为责任方不同：一个是运维，一个是攻击者。
  - **为什么不能放到启动自检**：dev 与 CI 的空密钥是**正确配置**，
    自检拦下就等于 G-B 不成立。所以只能在使用点拦。
  - **32 字符下限**：PyJWT 对短 key 只发 warning，而 warning 不是控制手段
    （本仓库 `filterwarnings=["error"]` 恰好让它在测试里现了形）。
    prod 启动期就拦，是为了不把它留到第一个用户登录时才炸。
- 回退成本：低。两处判断，常量在 `core/config.py`。
- 影响范围：`app/core/security.py`、`app/core/config.py`。
- 保障：变异 B（去掉空 token 判断）与变异 H（下限改成 1）各转红。

## D-027 `auth_date` 只卡过去一侧，不为时钟偏移设容差常量

- 日期：2026-09-18
- 背景：initData 的 `auth_date` 用来限制重放窗口。落在未来的时间戳怎么办，
  Telegram 文档没说，常见实现各不相同。
- 选择：只在 `now - auth_date > TELEGRAM_INIT_DATA_MAX_AGE_SECONDS` 时拒绝。
  未来时间一律放行，**不引入「允许超前 N 秒」的常量**。
- 理由：
  - 带着有效签名、且时间在未来的 initData，只可能来自 Telegram——
    别人造不出签名。那说明**他们的钟比我们快**，拒绝它等于把对方的
    时钟偏移变成我们的故障。
  - 引入容差就要回答「N 是多少」，而那个数字一旦写下就是 R3 说的硬编码阈值，
    却又没有任何线上数据能用来校准它。不引入这个问题更干净。
- 回退成本：低。一处比较。
- 影响范围：`app/core/security.py`。
- 保障：变异 E（窗口放大一千倍）转红三条。

## D-028 `signature` 字段留在 HMAC 校验串里

- 日期：2026-09-18
- 背景：Telegram 近年给 initData 加了 `signature`（Ed25519，供第三方验证）。
  「删掉 hash 和 signature」这句话出现在他们文档的**第三方验证**小节，
  很容易被读成对 HMAC 校验也成立。
- 选择：HMAC 校验串只排除 `hash`，其余字段（含 `signature`）全部保留，
  与 aiogram 等参考实现一致。
- 理由：照第三方那套删掉 `signature`，**每一个真实登录都会失败**，
  而本地用假数据自测时一切正常——因为造数据的人往往不加这个字段。
  这是最容易在上线当天才暴露的一类错误，所以单独写一条测试
  （`test_unknown_fields_stay_inside_the_signature`）盯住它。
- 回退成本：低。
- 影响范围：`app/core/security.py`。
- 保障：变异 D（把 `signature` 排除出校验串）转红。

## D-029 首次登录一次性开出三行；软删除账号拒绝登录而不是复活

- 日期：2026-09-18
- 背景：`entitlements.reset_at` 无数据库默认值，而 `quota.py` 把「没有
  entitlements 行」当作**开户失败**（D-018），不当作额度为零。那这行谁来建？
- 选择：首次登录在同一个事务里建 `users` + `user_profiles` + `entitlements`。
  三条语句都写成再来一次也不出错（`ON CONFLICT`）。
  `users.deleted_at` 非空时拒绝登录，并**回滚**（不留下 `last_active_at` 痕迹）。
- 理由：
  - **不做懒创建**：懒创建意味着每个读额度的地方都要先处理「行不存在」，
    那正是 D-018 想避免的分支。出生即完整。
  - **`reset_at` 用 `next_reset_at()` 算**，与每日重置任务同一个函数；
    `timezone` 从刚插入的 `user_profiles` 行 RETURNING 回来，
    而不是在 API 层再写一遍 `'Asia/Phnom_Penh'`——DDL 的默认值是唯一真相。
  - **locale 只在 insert 时写**：Telegram 的 `language_code` 是首次的合理猜测，
    不是长期指令。否则用户在应用内改了界面语言，下次登录就被悄悄改回去。
  - **软删除不复活**：`deleted_at` 是删除，不是休眠标记。重新登录就撤销删除，
    等于删除从来没生效过。
- 回退成本：低。
- 影响范围：`app/api/v1/auth.py`、D3（读 entitlements）、D8a（订阅开通）。
- 保障：变异 I（登录顺手把当日额度清零）与变异 J（允许软删账号登录）各转红。
- 附带发现：**第一轮变异测试十二条全部「转红」，而那个结果是假的。**
  跑测试的子进程继承了 macOS 的 `__PYVENV_LAUNCHER__`，venv 里的 pytest
  直接 `ModuleNotFoundError`——退出码非零，和「变异被抓住」一模一样。
  改成清理环境变量、并要求输出里必须出现 pytest 的统计行之后重跑，
  才是真的十二条全红。**一个坏掉的 runner 会把每一条变异都报成成功。**

## D-030 档位由 `subscriptions.status` 决定，读取端不按日期重新推导

- 日期：2026-09-18
- 背景：`users` 表没有 plan 列。D2 的媒体选择需要知道档位，而档位只能从
  `subscriptions` 推出来。一个 `status='active'` 但 `period_end` 已过去的行
  （续费任务还没跑到）该怎么算，PRD 未定义。
- 选择：`status IN ('active','grace')` 的行授予其 plan，多行取**最高档**；
  一行都没有就是 `free`。**不比较 `period_end` / `grace_until`。**
  放在 `services/entitlements/plan.py`。
- 理由：
  - **不让读取路径去猜状态机**。ARCHITECTURE 3.4 把
    active / grace / expired / cancelled 之间的迁移交给 D8b 的定时任务。
    读的时候再拿日期算一遍，等于状态机有两份实现，学习者的档位取决于
    是哪条代码路径在问。与 D-023「行一旦存在，行就是答案」同一条理由。
  - 代价说清楚：D8b 若不工作，过期用户会继续享有权益。这个风险属于 D8b 的测试，
    不属于这里——把它挪到这里只会让两边都不完整。
  - **grace 授予权益**：晚付一天不该把人从正在上的课里踢出去。
  - **多行取最高档**：期中升级会同时留下 basic 与 pro 两行。取错就是
    让刚付过钱的人降级，而且什么都不会报错。
- 为什么放 entitlements：这个包本来就是「这个人有什么权益」，
  且按 D-018 已经是领域层里允许碰数据库的那个。规则本身
  （`best_plan`）是纯函数，单测不需要数据库。
- 回退成本：低。
- 影响范围：`app/services/entitlements/plan.py`、D3、D5、D8a。
- 保障：变异 K（多行取第一个而非最高档）与变异 L（去掉 status 过滤）各转红。

## D-031 三个读端点一律要 token；org 课程在 C 端返回 404 而不是 403

- 日期：2026-09-18
- 背景：`courses.org_id` 为 B 端岗位课包而设（PRD 12），而成员关系要到 M4
  （BACKLOG G1）才有。在那之前，C 端的三个读端点该怎么处理这些行？
- 选择：
  - 三个端点全部走 bearer token，没有匿名读；
  - `org_id IS NULL` 是 C 端可见性的唯一条件，不满足的课程与其下的课时
    **一律 404**，与「不存在」同一个回答；
  - `/lessons/{id}` 与 courses 做 join 校验，不单独查 lesson。
- 理由：
  - **默认不泄漏**。少写一个 where 和「还没做成员校验」看起来一样，
    但前者现在就在漏数据。M4 要加的是放行逻辑，不是收紧逻辑。
  - **404 而非 403**：可区分的 403 会把 UUID 空间变成一份「哪些企业在这里有内容」
    的目录。M4 的跨 org 隔离验收要的就是这个性质。
  - **join 而不是单查**：课时的可见性继承自课程。只查 `lessons` 表，
    一个泄漏出去的 lesson id 就绕开了目录上的过滤。
  - **课程列表不公开**：目录本身不算机密，但匿名端点是一块要额外维护正确性的
    表面，而学习者从打开 Mini App 起就有 token。
- 回退成本：低。M4 时把 `org_id IS NULL` 换成「org_id 为空 **或** 调用者是成员」。
- 影响范围：`app/api/v1/courses.py`、`app/api/v1/lessons.py`、G1、G5。
- 保障：变异 A/B/C（三处分别去掉 org 过滤）、变异 D（未知课程返回空列表
  而不是 404）、变异 M（课时读取不再要 token）全部转红。

## D-032 媒体 URL 原样返回，不在 API 层拼 `PUBLIC_MEDIA_BASE_URL`

- 日期：2026-09-18
- 背景：`media_assets.url` 是完整 URL 还是对象存储的 key，取决于 E6/E7 怎么写，
  而那还没实现。`PUBLIC_MEDIA_BASE_URL` 目前为空（L-5）。
- 选择：D2 把 `media_assets.url` **原样**放进响应，不做任何前缀拼接。
  同时响应里带上 `decision`（为什么是这个形态）与 `client_timeout_ms`。
- 理由：
  - 现在发明一套拼接规则，等于替还没写的 E7 决定它该存什么。
    若 E7 存的是完整 URL，这段代码就是死的；若存的是 key，
    E7 的测试第一时间就会看到响应里是相对路径——发现成本很低，
    而猜错的成本是两处都要改。
  - **`decision` 给客户端看**：它是学习者关于自己账号的答案
    （「为什么我看到的是音频」）。没有它，一个支持问题就得靠重建账号状态来回答。
  - **`client_timeout_ms` 由服务端下发**：PRD 7.3 的 3 秒阈值要能不发版就调整。
- 回退成本：低，且有明确接手人（E6/E7）。
- 影响范围：`app/api/v1/lessons.py`、E6、E7。
- 保障：变异 J（主形态是音频时就不返回 video_url）转红——双 URL 是
  BACKLOG D2 的验收条件本身。

## D-033 档位阶梯（`PLAN_RANK`）上移到 `core/config.py`

- 日期：2026-09-18
- 背景：`media/resolve.py` 里有一份私有的 `_PLAN_RANK`。D-030 的 `best_plan`
  需要同一个顺序。
- 选择：把阶梯与 `plan_rank()` 移到 `core/config.py`（`Plan` 字面量旁边），
  两处都从那里引用。**这是对一个已通过验收的模块的改动**，
  按 CLAUDE.md 第 6 节在此备案。
- 理由：复制一份三个键的字典，就是第二处会被改错的地方；而阶梯是结构性的
  （档位本身就是这个顺序），不是配置项。改动是机械的：
  `media/resolve.py` 的 30 条既有测试**一行未改**，全绿——
  这正是「机械」的证据。
- 回退成本：低。
- 影响范围：`app/core/config.py`、`app/services/media/resolve.py`、
  `app/services/entitlements/plan.py`。
- 保障：变异 O（把 pro 排到 basic 之下）在三个测试文件里共转红 7 条。

## D-034 额度先扣，失败后补偿返还；返还是独立方法而不是负数扣减

- 日期：2026-09-18
- 背景：两条要求互相冲突。ARCHITECTURE 2.1 要求先校验额度再调评测
  （用完额度的人不能触发付费调用——每日上限正是 PRD 11.2 成本上限的抓手）；
  ARCHITECTURE 第 5 节要求 provider 失败时**不扣额度**。
  而 `quota.py` 的原子 UPDATE 无法同时满足两者：它把校验与扣减合成一条语句，
  没有「只校验不扣」的安全形态。
- 选择：**先原子扣减**，评测返回 `ok=False` 时再调
  `Entitlements.release_attempt()` 补偿返还。返还用 `GREATEST(used - n, 0)` 兜底。
- 理由：
  - **不能先评测后扣**：并发下十个请求都能通过前置校验、都发起付费调用，
    然后其中几个在扣减时被拒——钱花了，用户什么也没得到。
    这正是 `QuotaSnapshot` 文档里「永远不要拿它当闸门」的那个洞。
  - **中间窗口的失败方向是对的**：进程在扣减与返还之间死掉，学习者损失一次
    额度（免费档十次里的一次），而不是预算被击穿。
  - **为什么是独立方法而不是 `consume_attempt(-1)`**：`_require_positive`
    存在的理由就是挡住「负数扣减 = 静默返还」。让补偿走同一条路，
    等于亲手拆掉那道防线。独立方法只有一个调用点，且日志事件名不同
    （`entitlements.released`），运维能看出返还与扣减的比例。
- 回退成本：低。
- 影响范围：`app/services/entitlements/quota.py`、`app/api/v1/attempts.py`。
- 保障：变异 A（删掉 release 调用）转红 2 条；另有「连续三次失败后额度仍为 0」。
- 附带发现：**返还的 `GREATEST(..., 0)` 下限第一遍没有被任何测试覆盖**（变异 O 存活）。
  它不是防御性装饰：扣减 → 评测卡住 → 学习者本地午夜到了、每日重置把计数器清零
  → 失败返回、执行返还，计数器就变成 **-1**，学习者白得一次额度直到下次重置，
  而 `daily_attempts_used` 列上没有 CHECK 约束，不会有任何东西报错。
  已补三条测试（返还到零、重复返还、非正数返还被拒）。

## D-035 评测失败映射为 503，且失败调用仍然记账（成本 0）

- 日期：2026-09-18
- 背景：`scoring/base.py` 规定 provider **不抛异常**，失败以
  `ok=False + error_code` 返回，由调用方决定降级。API 层就是那个调用方，
  它该回什么，PRD 未定义。
- 选择：抛 `ScoringUnavailable`（`scoring.unavailable`，HTTP 503）。
  同时**仍向 `cost_ledger` 写一行**，`unit='calls'`、`cost_usd_cents_est=0`。
- 理由：
  - **503 而不是 200 + `ok:false`**：Bot 与 Mini App 用状态码分支比解 body 便宜，
    且 `code` 字段本来就是 i18n 契约（CODING_STANDARDS 5.1）。
    这不违反「provider 不抛异常」——抛的是 API 层，是它做出的降级决定。
  - **失败也要有账**：`external_call` 的上下文管理器要求必须 `record()`，
    否则抛 `UnledgeredCallError`。更重要的是：**打出去的调用就是发生过的调用**。
    今天 FakeScorer 失败报 0 成本，真实 provider 未必；而且失败率只有在
    `/admin/costs` 里看得见才有人看。零成本行不影响求和，只增加计数。
  - **「失败」以 `ok` 为准，不以「有没有分数」为准**。见下。
- 回退成本：低。
- 影响范围：`app/api/v1/attempts.py`、D5 的成本看板。
- 保障：变异 D（不记账）转红 18 条、变异 E（只记有成本的调用）转红 3 条。
- 附带发现：**判断失败的两个条件，第一遍只有一个被测到**（变异 C 存活）。
  `not result.ok or result.pron_score is None` 里删掉前半段，
  25 条集成测试全绿——因为 FakeScorer 失败时两者总是同时成立。
  真实 provider 完全可能在 `ok=False` 的同时带回一个部分分数，
  那时被删掉的那半段正是唯一拦住「拿一个供应商自己都不认的分数去扣额度、
  写 attempts、推 mastery」的东西。已抽成 `usable_score()` 并单测：
  `ok=False` 且带 88 分 → 仍然是失败。**这是这一条最值钱的一次变异。**

## D-036 脚本化 item 的参考文本键定为 `payload.target_text`；缺失时抛 `ValueError`

- 日期：2026-09-18
- 背景：`lesson_items.payload` 是 JSONB，其 schema 属于 E1（seed schema），
  而 E1 还没做。D3 现在就需要从里面取出「学习者要说的那句话」。
- 选择：键名定为 `target_text`，`drill` / `vocab` 走 SCRIPTED 并从这里取参考文本；
  `qa` 走 UNSCRIPTED 不取。**取不到就抛 `ValueError`（500）**，不降级。
- 理由：
  - 总要定一个名字，早定早对齐。**E4 的校验规则里必须加上这一条**，
    这条决策就是给 E 阶段的接口约定。
  - **为什么不降级成 UNSCRIPTED**：那会让一个坏掉的内容包看起来在正常工作，
    分数还照给——只是不再对照任何参考文本。宁可 500：
    这是我们的缺陷（包不该被导入），不是学习者的。
  - 为什么不是 `AppError`：它不是业务规则不满足，是不变量被破坏
    （CODING_STANDARDS 5.1 第三类）。
- 回退成本：低，但**要和 E1/E4 对齐**；改键名时两边一起改。
- 影响范围：`app/api/v1/attempts.py`、E1、E3、E4。

## D-037 每日额度重置在请求路径上惰性执行

- 日期：2026-09-18
- 背景：C5 实现了 `QuotaReset.reset_if_due()`，但没有任何东西调用它。
  谁来触发重置，PRD 未定义。
- 选择：在 `POST /attempts` 进入时调用一次（扣减之前）。
  不引入定时任务。
- 理由：
  - **依赖 worker 的重置就是可能不发生的重置**。RQ 没起、任务队列堵了、
    部署漏了一个进程——症状都是「学习者今天没有额度」，而且没有报错。
  - `reset_if_due` 的守卫 UPDATE（`reset_at <= now`）本来就写成可以每次请求都调，
    幂等且并发安全，这是 C5 的设计意图。
  - 代价：不消费额度的用户，其计数器在下次开口前不会归零。
    而不开口的人没有额度问题，所以这个滞后没有症状。
- 回退成本：低。将来若真需要定时任务（例如给「今日剩余」看板用），
  两者可以共存，因为同一条守卫语句保证不会重复重置。
- 影响范围：`app/api/v1/attempts.py`、D5。

## D-038 不保存学习者录音；上传体积走 `ATTEMPT_MAX_AUDIO_BYTES`

- 日期：2026-09-18
- 背景：`attempts.audio_url` 列存在，但没人规定要不要往里写。
- 选择：**留空**。录音评测完即丢，不落对象存储。
  新增 `ATTEMPT_MAX_AUDIO_BYTES`（默认 2 MiB），分块读取，超限直接 413，
  不进评测、不扣额度、不记账。
- 理由：
  - 开始保留一个人的声音是一项要**先决定**的事，不是顺手实现的默认值。
    对象存储本来也还没接（D-002），列留着，需要时再填。
  - **体积上限是成本闸门**：真实评测按音频时长计费，且整段要读进内存。
    没有上限时，一次上传就能同时打穿内存与预算。
    默认值对一条几十 KB 的 Telegram 语音来说极宽松。
- 回退成本：低。
- 影响范围：`app/api/v1/attempts.py`、`core/config.py`、E9。

## D-039 完课时为「没开过口」的 concept 补建 mastery 行，已有行一律不动

- 日期：2026-09-19
- 背景：PRD 3.3 的结尾是「lesson_complete → 更新 concept_mastery → 写入 review_queue」。
  但 mastery 那一半 D3 已经在做了——每次开口都会更新。那完课还剩什么？
- 选择：为这节课 `concept_ids` 里**还没有 mastery 行**的 concept 各建一行：
  mastery 0、attempt_count 0、ease = `SM2_EASE_INITIAL`、**明天到期**。
  已有行（学习者练过的）一个字段都不改。
- 理由：
  - **剩下的正是「点完但没练」的部分**。D3 只在 attempt 落地时建行，
    所以一节课里跳过的 item、纯讲解的 concept，永远不会有行，
    也就**永远不会进复习队列**——课显示「已完成」，而其中一部分从未被练过，
    且没有任何地方会提醒。
  - **不碰已有行**：它们的排期正在生效中，重算等于把学习者的进度抹平。
    与 D-023 同一条：行一旦存在，行就是答案。
  - **为什么是明天而不是立刻到期**：刚上完课的 concept 立刻出现在复习队列里，
    就是间隔重复被关掉。`INITIAL_INTERVAL_DAYS` 本来就是 1 天。
- 回退成本：低。
- 影响范围：`app/api/v1/lessons.py`、D6。
- 保障：变异 A（不补建）转红 6 条、变异 B（改成覆盖已有行）转红 2 条、
  变异 D（建成立刻到期）转红 2 条、变异 E（用 DDL 默认 ease）与
  变异 F（attempt_count 建成 1）各转红 1 条。

## D-040 完课幂等，保留第一次的 `completed_at`

- 日期：2026-09-19
- 背景：学习者会重看已完成的课。第二次调用 complete 该怎么办，PRD 未定义。
- 选择：`ON CONFLICT DO UPDATE ... WHERE status <> 'completed'`。
  第二次调用什么也不改，返回的是第一次写下的时间，并带
  `first_completion: false`。
- 理由：重看一节课不等于「重新完成」了它。刷新时间戳会悄悄改写
  「这个人是什么时候学到这里的」——而那正是 B 端月报（G4）与留存分析要读的东西。
- 回退成本：低。
- 影响范围：`app/api/v1/lessons.py`、G4。
- 保障：变异 C（去掉 WHERE 条件）转红。

## D-041 复习时长用 `IntEnum` 而不是 `Literal`，默认 10 分钟

- 日期：2026-09-19
- 背景：`REVIEW_DURATION_CHOICES = (5, 10, 15, 25)` 是 C3 定的常量。
  查询参数怎么校验、不传时给多少，PRD 未定义。
- 选择：查询参数类型是 `IntEnum`；不传时 10 分钟。
- 理由：
  - **`Literal[5, 10, 15, 25]` 看起来等价，实际全错**：查询串到达时是
    `"10"`（字符串），pydantic 对 Literal 只做匹配不做转换，于是**每一个请求
    都被拒**——包括合法值。而默认值本身是 int，所以不带参数的那条路径是通的。
    第一版就是这么写的，31 条测试里只有用到 `?minutes=` 的那些转红。
    **这类 bug 只在真实客户端第一次传参时才暴露。**
  - **不读 `user_profiles.daily_goal_minutes`**：那是每日目标，取值不受
    这四个选项约束（默认 10 恰好合法而已）。拿它当默认，等于用户改一下目标
    就让这个端点开始报 422。
  - 枚举重述了 `REVIEW_DURATION_CHOICES`（两种写法都没法从元组动态构造），
    所以补了一条测试钉住两者相等。
- 回退成本：低。
- 影响范围：`app/api/v1/review.py`、F1（Mini App 的时长选择器）。
- 保障：变异 I（忽略传入时长）转红 6 条；另有「四个合法值都通、五个非法值都 422」。

## D-042 每概念复习时长是配置里的固定值

- 日期：2026-09-19
- 背景：`build_review_queue` 需要每个 concept 的预计耗时来切片，
  而 C3 明确把它设计成**入参**：真实估算取决于该 concept 挂了多少 drill/vocab item，
  那是 `lesson_items` 的事，领域层不该去 join。
- 选择：新增 `REVIEW_ESTIMATED_SECONDS_PER_CONCEPT`（默认 60），API 层传给它。
- 理由：C3 的 docstring 写明「今天会是一个固定值」。按 item 数实算需要
  多一个 join，而且仍然要一个「每个 item 多少秒」的常数——同样是要标定的数字，
  却多了一份复杂度。改成实算时只改这一个调用点，领域模块不动。
- 回退成本：低。
- 影响范围：`app/api/v1/review.py`、`core/config.py`。
- 保障：变异 J（写死 60）转红——测试里配了 120 与 900 两个值。
- 附带发现：`_load_candidates` 第一版的 docstring 说 inner join 是为了挡住
  「concept 已从内容包删除、mastery 行还在」的情况。**为了证明它而写的测试
  根本构造不出那个状态**：`concept_mastery.concept_id` 是外键且没有 ON DELETE，
  数据库直接拒绝删除。docstring 已改成实情，测试改成钉住这条外键。
  一句自信的解释，如果没人去验证，就会指导后面所有相关改动——
  这和 C5 那次 DST docstring 写错是同一类。

## D-043 一个「任务」= 完成一节课

- 日期：2026-09-19
- 背景：`LIMIT_FREE_DAILY_TASKS`（Free 每日 3 个任务）从 A3 起就是配置项，
  但没有任何消费者，因为 PRD 里「任务」出现了两种读法：
  4.3 的套餐表把它与「每日 10 句评分」并列，读起来像**一节课**；
  4.1 的正文「Bot 发出任务提示 → 用户发 voice note」读起来像**一道口语题**。
  这是计量口径，按 CLAUDE.md 第 6 节属于必须问人的一类。
- 选择：**一个任务 = 完成一节课**，在 `POST /lessons/{id}/complete` 扣减，
  重复完课不重复扣（完课本身已是幂等的，见 D-040）。
  由项目所有者于 2026-09-19 拍板。
- 理由：只有这个口径能让 PRD 4.3 表里的两个数字自洽。
  3 节课 × 每节若干句 ≈ 10 句评分；若按题算，Free 用户 3 道题就用完，
  10 句的额度永远用不到，两个限额互相矛盾。
- 实现位置：**BACKLOG D8a**（付费墙，「Free/Basic 限额生效」）。
  D4 已经把完课端点建好且幂等，接入是在那里加一次 `consume_task`
  与一条 402 的分支。
- 回退成本：低。口径变了就换扣减点，`consume_task` 的实现本身不用动。
- 影响范围：`app/api/v1/lessons.py`、`services/entitlements/quota.py`、D8a。

## D-044 `/admin/costs` 的准入是配置白名单，默认空；拒绝返回 403 而不是 404

- 日期：2026-09-19
- 背景：`users` 表没有角色列，PRD 也没说运维端点怎么鉴权。
- 选择：新增 `ADMIN_TELEGRAM_IDS`（逗号分隔，**默认空**）。
  调用者的 telegram_id 不在表里就 403。
- 理由：
  - **默认空 = 无人可读**。一个「先开着，等谁想起来再关」的运维端点，就是开着的。
    空配置下连运维自己也读不了，这是正确的失败方向。
  - **用 telegram_id 而不是我方 uuid**：运维在部署存在之前就知道自己的
    Telegram id，而 uuid 是每个环境各生成一次的。
  - **每次请求查库拿 telegram_id，不把管理员身份放进 token**：否则一个已经
    从白名单里移除的人，手上的 token 还能用到过期（D-025 同一条理由）。
  - **403 而不是 404**：D-031 给内容端点选 404 是为了不让 UUID 空间变成目录；
    这里路径是固定的、OpenAPI 里就有，藏起来什么也买不到，
    而「你已登录但没权限」和「没这个东西」是两件事。
  - 不建角色表：M4 的 org 成员体系会带来真正的授权模型，
    现在为一个端点建一张表，届时要么迁移要么并存。
- 回退成本：低。
- 影响范围：`app/api/v1/admin.py`、`core/config.py`、M4。
- 保障：变异 A（去掉准入检查）转红 3 条、变异 B（空白名单当作放行）与
  变异 C（有账号就放行）各转红。

## D-045 读额度时顺带执行每日重置

- 日期：2026-09-19
- 背景：D-037 把重置挂在开口路径上。但学习者在本地午夜之后先打开的是
  「我还剩多少」，不是录音。
- 选择：`GET /me/entitlements` 也调一次 `reset_if_due`。
- 理由：不调的话，刚过本地午夜打开应用的人看到的是昨天用尽的计数器，
  于是他会相信自己还被锁着——直到他不信邪去录了一条。
  `reset_if_due` 的守卫 UPDATE 幂等且并发安全，读路径上调它代价是一条语句。
  GET 带副作用不理想，但这里的副作用是**纠正**，且与 D-037 用的是同一条规则、
  同一个函数——另写一份「虚拟重置」的展示逻辑才是真的错（那就有两份规则了）。
- 回退成本：低。
- 影响范围：`app/api/v1/me.py`。
- 保障：变异 K（重置永远不触发）转红。

## D-046 「不限量」对外是 `null`，不是 `0`

- 日期：2026-09-19
- 背景：内部 0 是「无上限」的哨兵值（CONFIG_REFERENCE 第 4 节，
  `LIMIT_BASIC_DAILY_ATTEMPTS=0` 表示不限）。
- 选择：`limit` 与 `remaining` 在不限量时输出 `null`；
  有上限时 `remaining` 用 `max(limit - used, 0)` 夹住。
- 理由：
  - 把 0 原样发出去，等于对每一个付费订阅者显示「今日剩余 0 次」。
    哨兵值是内部约定，越过 API 边界就不再有人记得它的含义。
  - **夹住负数**：`consume_attempt` 的守卫保证学习者花不超上限，
    但挡不住**上限本身被调低**——配置一改，已经用掉 99 次的人会算出 -89。
    界面没法画这个数。
- 回退成本：低。
- 影响范围：`app/api/v1/me.py`、D8a 的付费墙 UI。
- 保障：变异 I（不限量输出 0）与变异 J（不夹负数）各转红。

## D-047 成本聚合永远按 `unit` 再分一层；窗口末日包含全天

- 日期：2026-09-19
- 背景：PRD 第 10 节只写了「按 provider / 用户 / 日聚合」。
- 选择：三种分组由 `group_by` 参数选；**无论哪种，都额外按 `unit` 分组**。
  时间窗口 `since`/`until` 是**含端点的日期**，SQL 里对末日用
  `< 次日零点` 实现。默认窗口 30 天。
- 理由：
  - **不按 unit 分会把秒和 token 加在一起**。`cost_usd_cents_est` 那一列仍然
    完全正确，紧挨着它的 quantity 却是个没有意义的数——
    这种错最难被发现，因为报表看起来是对的。
  - **末日含全天**：`until=今天` 的人要的是今天，包括 23:30 那一笔。
    按 `<= 当日零点` 实现会静默丢掉一整天，而且只在有人对账时才发现。
  - **窗口按 UTC 而不是学习者本地日**：这是运维视角的花销，一张账单覆盖
    好几个时区的人；按谁的本地日都不对。
- 回退成本：低。
- 影响范围：`app/api/v1/admin.py`、G3/G4（企业后台与月报也要按窗口聚合）。
- 保障：变异 D（不按 unit 分组）转红 10 条、变异 F（末日砍在零点）转红 9 条、
  变异 E（不卡下界）与变异 G（不合法窗口照常返回）各转红。
- 附带发现：这一轮十四条变异里**有三条第一遍存活**，其中一条是我自己写坏的：
  它只在语句后面加了个注释，**什么语义都没改**——正是 DoD 1.1 第二条陷阱
  （「变异要改语义，不要改语法」）的反面。一个不改变语义的变异存活下来，
  证明的只是它自己没用。换成「读到的是查询结果的第一行而不是本人那行」
  之后当即转红。另外两条是真的：
  「不限量时不夹负数」没有任何测试覆盖（上限被调低这件事会发生），
  以及「空白名单当作放行」——它与后一层检查结果相同，
  差别只在日志里的原因，于是补了一条断言日志的测试：
  「没人配过」和「你不在名单里」对运维是两种完全不同的修法。

## D-048 Bot 用自己持有的 bot token 签一份 initData 去换 JWT

- 日期：2026-09-19
- 背景：D1 的鉴权入口只认 initData，而那是 Mini App 才有的东西——
  聊天里的 Bot 永远收不到 initData。Bot 怎么代表一个聊天用户调 API，PRD 未定义。
- 选择：Bot 进程用它本来就持有的 `TELEGRAM_BOT_TOKEN`，
  按 Telegram 的规则给「刚刚给我发消息的这个 user」签一份 initData，
  再走同一个 `POST /auth/telegram`。Token 按学习者缓存到过期前 60 秒。
- 理由：
  - **不引入第二套鉴权**。另一条路是「服务令牌 + telegram_id」，
    那意味着一个新密钥、一个新端点、一条新的必须同样正确的代码路径。
    而 Bot 持有的恰好就是 Telegram 用来签名的那把钥匙——
    它能签，正说明它就是那个 Bot。
  - **安全性没有变化**：拿到 bot token 的人本来就能冒充任何用户
    （他可以直接以 Bot 的身份行事）。这条路没有放大任何东西。
  - 服务端一个字都不用改，`verify_init_data` 照常校验签名与新鲜度。
    集成测试里 Bot 签、服务端验，两侧的实现互为对方的守卫。
- 回退成本：低。若将来要区分「Bot 代用户」与「用户本人」，
  在 initData 里加一个字段即可（它在签名链里）。
- 影响范围：`bot/api_client.py`、`app/core/security.py`（未改动，只被验证）。
- 保障：变异 M（改用别的密钥签名）转红 9 条。

## D-049 Bot 先跑 polling，不跑 webhook

- 日期：2026-09-19
- 背景：PRD 第 8 节写的是 `python-telegram-bot v21（webhook 模式）`。
- 选择：`bot/main.py` 用 `run_polling()`。
- 理由：webhook 需要一个公网 HTTPS 端点，而这个项目目前没有部署环境。
  两者差一个调用（`run_webhook` 多接 URL 与 secret token），
  且完全局限在这一个文件里——换过去是部署工作，不是代码重构。
  现在为一个跑不起来的模式写配置项与分支，等于凭空造出一条没人走过的路径。
- 回退成本：低，且范围明确（一个函数调用 + 两个配置项）。
- 影响范围：`bot/main.py`。**部署时必须改回 webhook**，PRD 的选型没有变。

## D-050 Bot 的用户可见文案全部是 i18n key；km / zh 先留占位符

- 日期：2026-09-19
- 背景：Bot 几乎全部是文案，而 i18n 框架是 D7。更要紧的是
  CLAUDE.md 第 6 节把「用户文案的语气」列为**必须停下来问人**的一类。
- 选择：
  - `bot/flow.py` 与 `bot/handlers.py` 里**没有一个句子**，只有 key；
    渲染发生在最后一步（`_render`）；
  - `locales/en.json` 写了**功能性占位措辞**，让 Bot 在开发期能用；
  - `locales/km.json` 与 `zh.json` 全部是 `[[km:key]]` / `[[zh:key]]` 占位符；
  - 缺失翻译**渲染为占位符，不回落到英文**。
- 理由：
  - 静默回落会给高棉语学习者发一个英文 Bot，而日志里什么都没有。
    占位符在屏幕上很难看，正是它的用处——难看到不可能被漏掉。
  - **英文那份也不是最终文案**：我写的是尽量中性的功能句，
    `locales/README.md` 里写明了它没有经过产品声音的确认。
    高棉语必须由母语者写（PRD 15.3 第 3 条）。
  - D7 要做的是**检查**（CI 里 km 不得有占位符），那条检查在翻译写好之前
    本来就该是红的——它是闸门，不是噪音。
- 回退成本：低。
- 影响范围：`locales/`、`bot/i18n.py`、D7。
- 保障：变异 N（缺失翻译回落英文）转红；另有「三个 locale 的 key 集合必须相等」。
- 附带发现：两条变异第一遍存活，两条都是测试写得不够狠：
  **「重试次数带进下一步」**——原来的断言在一个从 0 开始的 session 上做，
  怎么改都成立；真实症状是「一句话卡了三次的学习者，接下来整节课都只有一次机会」，
  不报错、不可见。**「resume 越界」**——守卫去掉后仍然正常结束，
  差别只在它会先说一句「继续第 8 步，共 1 步」。断言改成
  「越界时一句话都不说」之后才钉住。

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
| L-3 | `REALTIME_PROVIDER` **未纳入启动自检**。目前没有 realtime registry，没有东西可以校验它，配错了今天既无症状也无后果。该适配器落地时要在 `selfcheck.py` 补一个 `_realtime_problems()`，否则一个错值会一路走到第一次 Pro 实时会话。 | BACKLOG F2（M3 实时语音代理） | `services/selfcheck.py` 的 `_llm_problems` 上方 |
| L-4 | `docker-compose.yml` 把两个数据存储绑在 `0.0.0.0`，且 **Redis 完全没有密码**。仓库转 private 只解决 PostgreSQL 那一半（凭据不再公开），Redis 的暴露面与仓库可见性无关——同局域网内任何人都能直连。修法是绑回 loopback：`ports: ["127.0.0.1:6379:6379"]`。已与项目所有者确认**暂缓**。 | C 阶段 Redis 开始承载真实数据时 | 见 D-001；`docker-compose.yml` |
| L-5 | `OBJECT_STORAGE_ENDPOINT` 与 `PUBLIC_MEDIA_BASE_URL` 仍为空，**`Settings` 中必须保持可选**。声明为必填会让全 fake 配置启动失败，直接违反 G-B 验收。 | BACKLOG E6 / E7（真实对象存储） | 见 D-002；`core/config.py` |
| L-7 | **按当前默认参数，`ease_factor` 必然在 mastery 还很低的时候就触底，复习间隔长期停在 1 天。** 算一遍：drill 权重 0.6、`MASTERY_DELTA_BASE=40`、及格线 60，则满分一次只加 0.6 分，要爬到 `MASTERY_LOW`(60) 需要约 100 次；而在那之前每一次都落在「reset」带里，每次扣 0.2 ease，**6 次后就到 `SM2_EASE_MIN`(1.3)**。也就是说间隔重复在默认配置下几乎不生效。算法实现没错（PRD 9.2 原样如此，见 D-015/D-016），错的是参数标定。**M0-1 拿到真实分数分布后，必须连同 `MASTERY_DELTA_BASE` 与三个权重一起重新标定**，不要只调 `SCORING_PASS_THRESHOLD`。 | M0-1 结论落地时；或第一次有人问「为什么所有概念天天都要复习」 | `services/mastery/sm2.py` 的 `schedule_review`；`core/config.py` 的 `mastery_delta_base` |

| L-8 | **`LIMIT_FREE_DAILY_TASKS`（Free 每日 3 个任务）仍然没有任何消费者**，所以 Free 档的这条限额目前等于不存在。口径已不再是障碍：D-043 已定「一个任务 = 完成一节课」，剩下的只是接线——在 `POST /lessons/{id}/complete` 里扣一次 `consume_task`，额度不足返回 402。 | BACKLOG D8a（付费墙）。在那之前 Free 用户不受任务数限制，只受每日 10 句评分限制 | `services/entitlements/quota.py` 的 `consume_task` |
| L-9 | **`streaks` 表没有任何写入方。** 它只在 PRD 第 9 节的表清单里出现过一次，没有任何 BACKLOG 条目、没有行为规格。完课是它最自然的写入点，但那属于扩范围（CLAUDE.md R6），所以 D4 没做。**要么补规格要么删表**——一张永远为空的表，会让后面每个读它的人先花时间确认它是不是坏了。项目所有者于 2026-09-19 确认**暂时留着不动**，不必再问一次。 | 有人要做连续打卡 / 留存激励时；或 M4 月报需要活跃度指标时 | `models/learning.py` 的 `Streak` |

| L-10 | **PRD 11.3 的成本护栏（「超过档位上限 1.5 倍时告警并自动限流」）已认领：BACKLOG D10，排在 D8a 之后**（项目所有者 2026-09-19 决定；限流要决定降到哪一档，取决于订阅链路先跑通）。在它落地之前—— D5 把 `/admin/costs` 做出来了，但它只呈现花销，不比较上限、不告警、不限流。而 PRD 第 11 节把单位成本上限写成**硬约束**，M2 验收清单里也有「单用户月成本估算未超 `COST_CAP_BASIC_USD_CENTS_MONTHLY`」这一条——目前这条只能靠人去看看板。实现时必须同时处理 L-1（浮点比较要显式取整）。 | BACKLOG D10（D8a 之后，M2 验收之前） | `app/api/v1/admin.py` 的模块 docstring；`core/config.py` 的 `cost_alert_multiplier` |

**处理完一条就把它从这张表里删掉**，并在对应的代码注释里说明已解决——留着一条已经不成立的约束，比没有这张表更糟。
