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

