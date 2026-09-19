# 代码规范

不重复通用最佳实践，只写**本项目容易出错的地方**和**必须统一的约定**。

---

## 1. 工具链（不可替换）

| 项 | 选择 | 配置位置 |
|---|---|---|
| Python | 3.12 | `pyproject.toml` |
| 依赖管理 | uv（或 pip-tools，二选一后不再变） | `pyproject.toml` |
| Lint + Format | ruff（含 isort 规则） | `pyproject.toml [tool.ruff]` |
| 类型检查 | mypy，`strict = true` | `pyproject.toml [tool.mypy]` |
| 测试 | pytest + pytest-asyncio + pytest-xdist | `pyproject.toml` |
| 迁移 | alembic | `backend/alembic/` |
| 前端（M3） | TypeScript strict + ESLint + Prettier | `miniapp/` |

`make check` = `ruff check` + `ruff format --check` + `mypy` + `pytest`。**这条命令必须全绿才能提交。**

测试默认并行 4 个 worker（`--dist loadfile`，同一文件进同一 worker），约 70 秒跑完。
集成测试**共用一个临时库**，每个测试进入前 TRUNCATE 全部表来隔离（D-076）。
调试某个失败时用 `make test PYTEST_WORKERS=1` 退回单进程。
**新增数据表不需要改测试**——重置的表清单是从数据库读回来的，
`tests/integration/test_isolation.py` 会校验它与实际存在的表完全一致。

---

## 2. 类型

- 全量类型注解，`mypy --strict` 通过。不允许 `# type: ignore` 除非附一行原因注释
- 领域对象用 `@dataclass(frozen=True)`，不用裸 dict 在层间传递
- API 出入参用 pydantic v2 模型，**与 SQLAlchemy 模型分离**，不得直接暴露 ORM 对象
- `Any` 只允许出现在 provider 的 `raw` 字段和 `jsonb` 列

---

## 3. 金额与计量（最容易出 bug 的地方）

柬埔寨是双币种经济：**USD 有 2 位小数，KHR 有 0 位小数**。所以不能用 "cents" 语义。

```python
# ✅ 用户支付金额：最小货币单位 + 位数
Money(amount_minor=199,  currency="USD", currency_minor_units=2)   # $1.99
Money(amount_minor=8000, currency="KHR", currency_minor_units=0)   # ៛8000

# ✅ 内部成本核算：永远是 USD，语义明确
cost_usd_cents: int = 3

# ❌ 禁止
amount: float = 1.99
amount: Decimal = Decimal("1.99")
amount_cents: int = 8000          # KHR 没有"分"，这个命名本身就是 bug
```

- 用户支付金额列：`amount_minor INTEGER` + `currency` + `currency_minor_units`
- 内部成本列：`INTEGER`，字段名以 `_usd_cents` 结尾
- **格式化只在展示层**，统一走 `core/money.py`，禁止在别处做 10**n 换算
- 实时语音分钟数同理：内部用**秒**的整数（字段名 `_seconds`），只在展示时换算成分钟
- 任何涉及金额、额度、分钟数的函数**必须有边界单测**：0、负数、超上限、并发扣减

---

## 4. 配置

```python
# core/config.py
class Settings(BaseSettings):
    scoring_provider: str = "fake"
    scoring_pass_threshold: float = 60.0
    ...
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")
```

- `extra="forbid"`：`.env` 里有未声明的键直接启动失败，避免拼写错误静默生效
- **新增任何阈值/权重/限额，必须同时更新 `CONFIG_REFERENCE.md`**，否则 PR 打回
- 判断"能不能硬编码"的标准：**这个数字有没有可能因为线上数据而改变？** 会改的都是配置

---

## 5. 错误处理

### 5.1 三类错误，处理方式不同

| 类型 | 做法 |
|---|---|
| 外部依赖失败（provider） | **不抛异常**，返回 `ok=False` + `error_code`。调用方决定降级策略 |
| 业务规则不满足（额度不足、未订阅） | 抛 `AppError` 子类，API 层统一转 HTTP 状态码 |
| 编程错误（不变量被破坏） | 抛原生异常，让它崩，不要吞 |

```python
# core/errors.py
class AppError(Exception):
    code: str          # 机器可读，前端据此做 i18n
    http_status: int

class InsufficientQuota(AppError): code="quota.insufficient"; http_status=402
class NotSubscribed(AppError):    code="sub.required";        http_status=402
```

### 5.2 禁止

- `except Exception: pass`
- 把外部失败包装成业务异常再抛（丢失降级机会）
- 在 service 层返回 HTTP 状态码

---

## 6. 日志

用 structlog，**结构化，不要拼字符串**。

```python
log.info("attempt.scored", user_id=uid, concept_id=cid,
         pron_score=r.pron_score, provider=scorer.name, cost_usd_cents=r.cost_usd_cents)
```

- 事件名用 `模块.动作` 的点分命名
- **禁止记录**：音频内容、用户手机号、支付原始报文中的敏感字段、任何 API key
- 外部调用必须记 `provider` / `latency_ms` / `cost_usd_cents` 三个字段
- 错误日志必须带 `error_code`，不只是 message

---

## 7. 异步

- IO 一律 `async`；CPU 密集（MinHash 去重、音频转换）放 `run_in_executor` 或 RQ worker
- **不要在 async 函数里做同步 IO**（`requests`、同步 DB 驱动）——这会阻塞整个事件循环
- provider 的 `verify_callback` 是唯一的同步方法（纯计算，无 IO），保持同步

---

## 8. 数据库

- 建表以 `DATA_MODEL.sql` 为准。SQLAlchemy 模型必须与之一致
- 每次结构变更都要 alembic migration，且 `downgrade` 必须真的能回滚（写完跑一遍验证）
- 外键一律显式声明；软删除用 `deleted_at`，不用 `is_deleted`
- 时间列一律 `TIMESTAMPTZ`，存 UTC，展示时按 `user_profiles.timezone` 转换
- 涉及额度扣减的更新必须用 `SELECT ... FOR UPDATE` 或原子 `UPDATE ... WHERE remaining >= n`，**不得读-改-写**

---

## 9. 测试

```
tests/
├── unit/          # 纯逻辑，无 IO，毫秒级
├── integration/   # 起 PG + Redis（testcontainers 或 compose），走真实 SQL
└── fixtures/
    ├── audio/     # 固定音频样本
    └── packs/     # 固定 content pack
```

- **必须单测**：mastery、SM-2、entitlements、限额、金额换算、validate.py 的每条规则
- **必须集成测**：attempts 完整链路（用 FakeScorer）、支付回调验签、媒体解析与回退
- 测试用 `pytest.mark.parametrize` 覆盖边界，不要写一堆相似的测试函数
- 断言**行为**不断言实现：`assert result.passed is False` 而不是 `assert mock.call_count == 1`

---

## 10. 命名

| 对象 | 约定 | 例 |
|---|---|---|
| 模块/文件 | `snake_case` | `zh_tone.py` |
| 类 | `PascalCase` | `PronunciationScorer` |
| 函数/变量 | `snake_case` | `apply_mastery_delta` |
| 常量 | `UPPER_SNAKE` | `MAX_RETRY` |
| 配置项 | `UPPER_SNAKE`，按域加前缀 | `SCORING_PASS_THRESHOLD` |
| DB 表 | 复数 `snake_case` | `lesson_items` |
| DB 列 | 单数 `snake_case`，带单位后缀 | `duration_ms` `amount_minor` `cost_usd_cents` |
| API 路径 | 复数名词，kebab 不用 | `/api/v1/lesson-items` ❌ → `/api/v1/lessons/{id}/items` ✅ |
| i18n key | `域.子域.键` | `avatar.ai_disclaimer` |

---

## 11. i18n

- 用户可见字符串**零内联**。全部 `t("key")`
- 语言：`km`（主）/ `zh` / `en`。文件 `locales/{lang}.json`
- 缺 `km` 翻译时占位 `[[km:key]]`，CI 检查：**上线前 km 不得有占位符**
- 高棉语文本处理注意：Unicode U+1780–U+17FF，**无词间空格**，不要用空格切词做长度判断

---

## 12. 前端（M3 开始适用）

- TypeScript `strict`，禁用 `any`
- 不在客户端做业务判断（见 ARCHITECTURE.md 第 1 节）
- 媒体播放必须实现超时回退：video 3 秒内未开始播放即切 audio
- Telegram WebApp SDK 的 `initData` 必须原样上送服务端验签，**不得在前端解析后信任**
