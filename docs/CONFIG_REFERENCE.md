# 配置清单

**这张表是判断"能不能硬编码"的唯一依据。表里有的一律走配置，新增阈值必须先加到这里。**

标记说明：`[M0]` = M0 验证出结论后需要重新校准；`[$]` = 直接影响成本，改动前先算账。

---

## 1. 运行环境

| 键 | 默认 | 说明 |
|---|---|---|
| `ENV` | `dev` | `dev` / `staging` / `prod` |
| `LOG_LEVEL` | `INFO` | |
| `DATABASE_URL` | — | PostgreSQL 连接串 |
| `REDIS_URL` | — | |
| `OBJECT_STORAGE_ENDPOINT` | — | 媒体文件存储 |
| `OBJECT_STORAGE_BUCKET` | `kruai-media` | |
| `PUBLIC_MEDIA_BASE_URL` | — | 媒体对外访问前缀（CDN） |

## 1b. 鉴权

| 键 | 默认 | 说明 |
|---|---|---|
| `TELEGRAM_INIT_DATA_MAX_AGE_SECONDS` | `86400` | Mini App initData 的有效期；超过即拒绝，限制重放窗口 |
| `JWT_ACCESS_TOKEN_TTL_SECONDS` | `3600` | 会话 token 有效期。客户端手里仍有 initData，过期后重新换发即可，无 refresh token |
| `ADMIN_TELEGRAM_IDS` | `` | 逗号分隔的 Telegram id，允许读 `/admin/costs`。**留空即无人可读**——运维端点默认关闭 |

> 签名算法（HS256）与签发者（`kruai`）不是配置项：改它们会让所有在用 token 立即失效，
> 那是一次带迁移方案的代码变更，不是一个可调参数。

## 1c. Bot

| 键 | 默认 | 说明 |
|---|---|---|
| `BOT_API_BASE_URL` | `http://localhost:8000` | Bot 进程访问 API 的地址。Bot 是独立进程，只走 HTTP，不 import service |
| `BOT_SESSION_TTL_SECONDS` | `86400` | 未完成的课在 Redis 里保留多久。一天：明天还能接着上，弃置的自己清掉 |

## 2. Provider 选择

| 键 | 默认 | 说明 |
|---|---|---|
| `SCORING_PROVIDER` | `fake` | `fake` / `azure` / `iflytek` |
| `SCORING_FALLBACK_PROVIDERS` | `` | 逗号分隔，按序回退 |
| `TTS_PROVIDER` | `fake` | `fake` / `azure` / `google` / `elevenlabs` |
| `LLM_PROVIDER` | `fake` | `fake` / `openai` / `anthropic` |
| `PAYMENT_PROVIDERS` | `fake` | 逗号分隔，可同时启用 `aba,bakong,stars` |
| `REALTIME_PROVIDER` | `fake` | M3 启用 |

> **全部设为 `fake` 时系统必须端到端跑通。** 这是 CI 的默认配置。

## 3. 评分与掌握度 `[M0]`

| 键 | 默认 | 说明 |
|---|---|---|
| `SCORING_PASS_THRESHOLD` | `60.0` | `[M0]` 及格线。M0-1 给出真实分布后必须重新校准 |
| `SCORING_MAX_RETRY` | `3` | 单条最多重试次数 |
| `SCORING_TIMEOUT_MS` | `8000` | 超时即降级，不扣额度 |
| `MASTERY_DELTA_BASE` | `40.0` | `delta = (score - pass_threshold) / base` |
| `MASTERY_WEIGHT_DRILL` | `0.6` | |
| `MASTERY_WEIGHT_VOCAB` | `0.8` | |
| `MASTERY_WEIGHT_QA` | `1.0` | |
| `MASTERY_HIGH` | `80.0` | 高于此值延长复习间隔 |
| `MASTERY_LOW` | `60.0` | 低于此值重置间隔 |
| `SM2_EASE_INITIAL` | `2.5` | |
| `SM2_EASE_MIN` | `1.3` | |
| `SM2_EASE_PENALTY` | `0.2` | |
| `ZH_TONE_MODE` | `auto` | `[M0]` `parse`（音素带声调数字）/ `derive` / `auto` |

## 4. 额度与限制

| 键 | 默认 | 说明 |
|---|---|---|
| `LIMIT_FREE_DAILY_TASKS` | `3` | |
| `LIMIT_FREE_DAILY_ATTEMPTS` | `10` | |
| `LIMIT_BASIC_DAILY_ATTEMPTS` | `0` | 0 = 不限 |
| `LIMIT_PRO_REALTIME_SECONDS_MONTHLY` | `3600` | 60 分钟 |
| `REALTIME_SESSION_MAX_SECONDS` | `900` | `[$]` 单次会话硬上限 15 分钟 |
| `REALTIME_CONTEXT_SUMMARIZE_AFTER_TURNS` | `8` | `[$]` 超过则摘要压缩 |
| `QUOTA_RESET_HOUR_LOCAL` | `0` | 按用户时区的重置时刻 |
| `REVIEW_ESTIMATED_SECONDS_PER_CONCEPT` | `60` | 复习队列切片用的单概念时长估算。目前是固定值，将来可按 item 数改算 |
| `ATTEMPT_MAX_AUDIO_BYTES` | `2097152` | `[$]` 单次上传录音上限（2 MiB）。超出直接拒绝，不进评测 |

## 5. 成本护栏 `[$]`

| 键 | 默认 | 说明 |
|---|---|---|
| `COST_ALERT_MULTIPLIER` | `1.5` | 超过档位上限此倍数时告警并限流 |
| `COST_CAP_FREE_USD_CENTS_MONTHLY` | `15` | |
| `COST_CAP_BASIC_USD_CENTS_MONTHLY` | `45` | |
| `COST_CAP_PRO_USD_CENTS_MONTHLY` | `230` | |
| `COST_LEDGER_REQUIRED` | `true` | 为 `true` 时未记账的外部调用直接抛异常（dev/CI 必须为 true） |

## 5b. 订阅与续费

两条路径都要支持，由 `provider.supports_recurring` 决定走哪条。

| 键 | 默认 | 说明 |
|---|---|---|
| `SUBSCRIPTION_DEFAULT_RENEWAL_MODE` | `manual` | 渠道不支持代扣时的兜底模式 |
| `SUBSCRIPTION_REMINDER_DAYS_BEFORE` | `3` | manual 模式：到期前几天推送续费提醒 |
| `SUBSCRIPTION_GRACE_DAYS` | `3` | 到期后的宽限期，期间 `status='grace'` 仍可用 |
| `SUBSCRIPTION_AUTO_CHARGE_RETRY_DAYS` | `1,3` | auto 模式：扣款失败的重试间隔（天，逗号分隔） |
| `SUPPORTED_CURRENCIES` | `USD` | 逗号分隔，如 `USD,KHR` |
| `DEFAULT_CURRENCY` | `USD` | |
| `PRICE_BASIC_MONTHLY` | `USD:199` | PRD 4.3。`币种:最小单位整数`，可写多币种如 `USD:199,KHR:8000` |
| `PRICE_BASIC_YEARLY` | `USD:1800` | |
| `PRICE_PRO_MONTHLY` | `USD:599` | |
| `PRICE_PRO_YEARLY` | `USD:5400` | |
| `PAYMENT_RECONCILE_AFTER_MINUTES` | `10` | 回调丢失兜底：pending 超过此时长就主动查单 |
| `PAYMENT_ABANDON_AFTER_HOURS` | `24` | 仍为 pending 就判定为放弃，置 failed |

> 价格是**最小货币单位的整数**：199 = $1.99，8000 = ៛8000。KHR 没有小数位，
> 所以这里永远不会出现「分」。`SUPPORTED_CURRENCIES` 里列了却没定价的币种，
> 启动自检会拒绝启动——收得了钱却报不出价，是一种没人会立刻发现的错。

## 6. 媒体与回退

| 键 | 默认 | 说明 |
|---|---|---|
| `MEDIA_VIDEO_ENABLED` | `false` | S2 前保持 false |
| `MEDIA_VIDEO_MIN_PLAN` | `pro` | 哪些档位可见视频 |
| `MEDIA_CLIENT_VIDEO_TIMEOUT_MS` | `3000` | 客户端切音频的超时阈值 |
| `MEDIA_DEFAULT_DATA_SAVER` | `false` | 新用户默认是否省流量 |

## 7. 内容管线

| 键 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_WITH_VIDEO` | `false` | `[$]` S2 前保持 false |
| `PIPELINE_REVIEW_SAMPLE_RATE` | `0.10` | 母语者抽检比例 |
| `PIPELINE_MIN_SENTENCES_PER_CONCEPT` | `8` | |
| `PIPELINE_MIN_SUBSTITUTIONS_PER_CONCEPT` | `12` | |
| `PIPELINE_MIN_DIALOGUES_PER_CONCEPT` | `3` | |
| `PIPELINE_DEDUP_THRESHOLD` | `0.9` | MinHash 相似度上限 |
| `PIPELINE_MAX_CHARS_HSK1` | `8` | |
| `PIPELINE_MAX_CHARS_HSK2` | `12` | |
| `PIPELINE_MAX_CHARS_HSK3` | `18` | |
| `TTS_SPEAKING_RATE_ZH` | `0.9` | |
| `TTS_SPEAKING_RATE_KM` | `1.0` | |

## 8. 实验（A/B）

| 键 | 默认 | 说明 |
|---|---|---|
| `EXPERIMENT_EXPLAIN_MEDIA_ENABLED` | `false` | S2→S3 的视频 vs 音频对比 |
| `EXPERIMENT_EXPLAIN_MEDIA_SPLIT` | `0.5` | 分到 **video**（实验组）的比例。`0` = 无人，`1` = 全部；见 D-021 |

## 9. 密钥（**永远不要有默认值**）

```
TELEGRAM_BOT_TOKEN=
AZURE_SPEECH_KEY=
AZURE_SPEECH_REGION=
GOOGLE_APPLICATION_CREDENTIALS=
ELEVENLABS_API_KEY=
OPENAI_API_KEY=
PAYWAY_MERCHANT_ID=
PAYWAY_API_KEY=
PAYWAY_BASE_URL=          # 沙箱 https://checkout-sandbox.payway.com.kh；生产另行配置
BAKONG_TOKEN=
JWT_SECRET=
```

---

## 新增配置项的流程

1. 加到本文件对应小节，写清默认值与说明
2. 加到 `core/config.py` 的 `Settings`
3. 加到 `.env.example`（密钥留空，其余给默认值）
4. 若带 `[$]` 标记，在 PR 描述里写清最坏情况的成本影响

漏掉第 1 步的 PR 直接打回。
