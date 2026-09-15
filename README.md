# KruAI

以高棉语为教学语言的中文口语陪练系统。一套引擎，两个入口——C 端 HSK 备考（Telegram Bot / Mini App），B 端中资雇主的岗位中文培训。

完整规格见 [`docs/PRD.md`](docs/PRD.md)（v1.1，唯一权威）。

---

## 快速开始

需要 [uv](https://docs.astral.sh/uv/)、Docker、Python 3.12（uv 会自动装）。

```bash
docker compose up -d          # PostgreSQL 16 + Redis 7
cp .env.example .env          # 72 个配置项，全部 provider 默认 fake
make sync                     # 装依赖
make migrate                  # 建表（21 张，对齐 docs/DATA_MODEL.sql）
make check                    # lint + format + mypy strict + pytest
```

`make check` 全绿即环境就绪。**不需要任何外部服务商的密钥**——这是下面那条设计前提的直接结果。

| 命令 | 作用 |
|---|---|
| `make check` | 提交前必须全绿：`ruff` + `ruff format` + `mypy --strict` + `pytest` |
| `make fmt` | 格式化（写入） |
| `make migrate` | `alembic upgrade head` |
| `make run` | 启动 API（API 骨架落地后可用） |
| `make sync` | 同步依赖 |

没起数据库时，schema 一致性的集成测试会跳过并提示；CI 里则会直接失败，不允许静默跳过。

---

## 设计前提：先 Fake，后真实

M0 外部验证（发音评测选型 / 高棉语 TTS 选型 / 支付联通）**尚未完成**，但系统 80% 的代码不依赖这三个结论。

解法是把三个外部能力全部收敛到 Provider 抽象后面，**先写确定性 Fake 实现**，让整条业务链路在没有任何真实供应商的情况下端到端跑通。M0 出结论后只新增实现类，不动调用方。

**验收这一设计是否成立的方法只有一条**：把 `.env` 里所有 `*_PROVIDER` 设为 `fake`，系统必须能完整跑通并通过全部测试。做不到就说明有地方绕过了抽象。CI 就是按这个配置跑的。

---

## 目录

```
backend/          FastAPI 后端
  app/core/         配置、日志、错误、金额
  app/models/       SQLAlchemy 模型（严格对齐 docs/DATA_MODEL.sql）
  app/services/     适配层（scoring/tts/payments/llm）+ 领域层
  alembic/          数据库迁移
  tests/            unit（纯逻辑）/ integration（需真实 PG）
bot/              Telegram Bot
pipeline/         内容生产管线（离线 CLI，不属于请求链路）
interfaces/       规范性 Provider 接口，须原样复制，签名不得改
docs/             规格与决策
```

---

## 文档

| 文件 | 看它找什么 |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | **项目纪律。七条红线、当前允许触碰的范围、工作方式** |
| [`docs/PRD.md`](docs/PRD.md) | 产品规格全文，唯一权威 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 模块边界、数据流、provider 契约 |
| [`docs/DATA_MODEL.sql`](docs/DATA_MODEL.sql) | 权威 DDL，建表以它为准 |
| [`docs/CODING_STANDARDS.md`](docs/CODING_STANDARDS.md) | 命名、类型、金额、错误处理、日志、测试 |
| [`docs/CONFIG_REFERENCE.md`](docs/CONFIG_REFERENCE.md) | 全部配置项；判断「这个能不能硬编码」的唯一依据 |
| [`docs/DEFINITION_OF_DONE.md`](docs/DEFINITION_OF_DONE.md) | PR 检查单、里程碑验收标准 |
| [`docs/BACKLOG.md`](docs/BACKLOG.md) | 做什么、什么顺序、哪些被 M0 阻塞 |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | PRD 未定义、由实施方自行决策的记录 |

---

## 贡献约定

- 一个 PR = 一个 [`BACKLOG`](docs/BACKLOG.md) 条目，提交信息末尾带 `Refs: BACKLOG#<编号>`
- 开 PR 前对照 [`docs/DEFINITION_OF_DONE.md`](docs/DEFINITION_OF_DONE.md) 的检查单逐条自检
- 金额一律 `amount_minor` + `currency_minor_units` 整数；内部成本用 `_usd_cents`。**KHR 没有小数位**，出现 float 即为 bug
- 新增阈值/权重/限额必须先加进 `CONFIG_REFERENCE.md`，否则 PR 打回
- 数据库变更必须有 alembic migration，且 `downgrade` 真的跑通过

---

## 并行推进的另一条线

M0 验证套件独立交付，三个脚本不依赖本工程。其中 **M0-1 需要先找中文母语者录 20 条音频**，是唯一阻塞在他人身上的事项，应尽早安排。
