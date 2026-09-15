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

