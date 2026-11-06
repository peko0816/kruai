# KruAI 部署手册（试运营）

一台服务器、Docker Compose（PRD 第 8 节）。设计取舍见 `docs/DECISIONS.md` D-144。

试运营部署在 DigitalOcean 多伦多（tor1）的一台 droplet 上，**和另一个项目共用**（D-145）。动手之前先读第六节。

## 它长什么样

```
            Telegram
               ▲  Bot 主动连出去（轮询），服务器不需要开任何端口
               │
┌──────────────┼──────────── 服务器（只开 SSH）────────────────────┐
│            bot ──────► api ──────► postgres                      │
│             │           │                                         │
│             │           └────────► redis（Bot 会话也存这里）      │
│             └─► media（课程音频，Bot 取到后上传给 Telegram）      │
└───────────────────────────────────────────────────────────────────┘
```

- **不需要域名，也不需要 HTTPS 证书。** Telegram 不会来访问我们的服务器：Bot 自己去取音频，再上传给 Telegram（D-143）。
- **什么时候需要域名：** 以后接支付回调、把 Bot 改成 webhook 模式、或者做 Mini App（M3）时。到时再加一个对外入口。
- **评分走 Azure，其他外部服务都不用。** 课程是预先生成好的数据，运行时不调用 LLM，也不调用 TTS（红线 R1）。
- **支付关闭**（`PAYMENTS_ENABLED=false`）。名单里的测试者按 Basic 档对待，月成本上限 500 美分（D-131、D-136）。

## 一、要准备的东西

| 项目 | 说明 |
|---|---|
| 服务器 | 用现有的 DigitalOcean tor1 droplet（D-145）。需要 Docker 和 `docker compose` 插件（v2）。KruAI 实测约占 175MB 内存，学员多起来估计 300–400MB；镜像和数据约 1GB 硬盘 |
| Telegram Bot token | 在 Telegram 里找 @BotFather，发 `/newbot`，按提示起名字，拿到 token |
| Azure 评分密钥 | 用现有的 `ai-tutor-speech`（eastasia）。密钥只填进服务器上的 `deploy/kruai.env`，不要发在聊天里 |
| 测试者的 Telegram 数字 id | 每人在 Telegram 里给 @userinfobot 发任意消息，它会回复一串数字 |
| 你自己的 Telegram 数字 id | 同上。填进 `ADMIN_TELEGRAM_IDS`，用来查成本 |

## 二、第一次部署

以下命令，标注「服务器」的在服务器上执行，标注「本机」的在你的 Mac 上执行。

**1. 取代码（服务器）**

```bash
git clone https://github.com/peko0816/kruai.git
cd kruai
```

仓库改成私有之后，这一步要先在服务器上配好 GitHub 的访问权限（部署密钥）。

**2. 生成配置（服务器）**

```bash
./deploy/init-env.sh
```

这一步会生成两个文件，只有你自己能读：
- `deploy/kruai.env`：应用配置，JWT 密钥已自动生成；
- `deploy/postgres.env`：数据库密码，已自动生成。

生成的密钥不会显示在屏幕上。然后编辑 `deploy/kruai.env`，填四项：

```bash
nano deploy/kruai.env
```

- `TELEGRAM_BOT_TOKEN=` BotFather 给的 token
- `AZURE_SPEECH_KEY=` Azure 评分密钥
- `TRIAL_TELEGRAM_IDS=` 测试者的数字 id，多人用英文逗号隔开，例如 `111111,222222`
- `ADMIN_TELEGRAM_IDS=` 你自己的数字 id

**3. 把课包传到服务器（本机，在项目目录下执行）**

课包有 20MB 左右，不在 git 里。把下面的 `用户名@服务器地址` 换成你的：

```bash
ssh 用户名@服务器地址 'mkdir -p ~/kruai/deploy/data/packs'
```

```bash
scp pipeline/packs/zh-HSK1.v2.pack.json 用户名@服务器地址:~/kruai/deploy/data/packs/
```

```bash
scp -r pipeline/packs/zh-HSK1-v2-media 用户名@服务器地址:~/kruai/deploy/data/packs/
```

**4. 让程序能写入课包目录（服务器）**

程序在容器里以 10001 号用户运行，导入时要往 `deploy/data/packs/published` 写音频：

```bash
sudo chown -R 10001:10001 deploy/data
```

**5. 构建、建表、导入课包、启动（服务器）**

```bash
docker compose -f deploy/compose.yml build
```

```bash
docker compose -f deploy/compose.yml run --rm migrate
```

```bash
docker compose -f deploy/compose.yml run --rm import /data/packs/zh-HSK1.v2.pack.json --media-dir /data/packs/zh-HSK1-v2-media
```

导入成功时会显示 `imported zh HSK1 v2: 48 new concept(s) ... 1112 clip(s)`。

```bash
docker compose -f deploy/compose.yml up -d
```

**6. 确认都在运行（服务器）**

```bash
docker compose -f deploy/compose.yml ps
```

五个服务（postgres、redis、api、bot、media）都应该是 `Up`，api 后面带 `(healthy)`。

## 三、冒烟测试（第一次上线后马上做）

这几项是本机测不了、只能在真实 Telegram 里验证的（D-141、D-142、D-143）。用一个已经填进 `TRIAL_TELEGRAM_IDS` 的账号做：

| # | 操作 | 应该看到 | 验证的是 |
|---|---|---|---|
| 1 | 给 Bot 发 `/start` | 高棉语欢迎词和命令列表 | Bot 连上了 Telegram 和 API |
| 2 | 发 `/status` | 档位显示 `basic` | 测试者名单生效 |
| 3 | 发 `/learn` | 讲解和跟读句子，**音频是语音气泡**，点了能播放 | 音频上传（D-143） |
| 4 | 按住麦克风，把跟读句子读一遍发出去 | 几秒内返回分数 | Azure 能接受 Telegram 的 OGG 语音（D-141） |
| 5 | 在服务器上执行下面的命令 | 最新一行 `millicents` 大约等于语音秒数 × 56 | 按真实时长计费（D-142） |
| 6 | 读得很差地再发一次 | 分数明显更低，读得不好的字用【】标出 | 评分有区分度 |

第 5 项的命令（服务器）：

```bash
docker compose -f deploy/compose.yml exec postgres psql -U kruai -c "select occurred_at, provider, ref, cost_usd_millicents_est as millicents from cost_ledger order by id desc limit 5;"
```

例如一条 5 秒的语音，应记 278 左右。如果每条都是 3334，说明时长没读出来，按 60 秒计了费。

**如果出问题：**
- **第 3 项，音频显示为播放条而不是语音气泡：** 能播放就不影响使用，这是预留的退路。记下来告诉我。
- **第 4 项，提示评分不可用：** 看日志（下一节）里的 `scoring.` 开头的错误码，把那一行发给我。不要把 `deploy/kruai.env` 的内容发给我。

## 四、日常运维（都在服务器上执行）

**看日志**（`bot` 可换成 `api`、`media` 等）：

```bash
docker compose -f deploy/compose.yml logs --tail 100 bot
```

日志里的 Bot token 会自动遮掉，显示为 `[redacted]`。

**更新到最新代码：**

```bash
git pull
```

```bash
docker compose -f deploy/compose.yml build
```

```bash
docker compose -f deploy/compose.yml run --rm migrate
```

```bash
docker compose -f deploy/compose.yml up -d
```

**备份数据库**（生成一个带日期的文件，建议每天一次，并复制到服务器以外的地方）：

```bash
docker compose -f deploy/compose.yml exec -T postgres pg_dump -U kruai kruai | gzip > kruai-$(date +%F).sql.gz
```

**全部停止：**

```bash
docker compose -f deploy/compose.yml down
```

**不要加 `-v`。** `down -v` 会删掉数据库里的全部学员数据。

## 五、安全要点

- 服务器防火墙只开 SSH。所有服务都不对外监听，这一点在 `compose.yml` 里已经保证了。
- `deploy/kruai.env` 和 `deploy/postgres.env` 只有你能读，它们不在 git 里，也不会打进镜像。
- 换 Bot token 或 Azure 密钥：改 `deploy/kruai.env`，再执行 `docker compose -f deploy/compose.yml up -d`。

## 六、与另一个项目共用服务器（D-145）

这台 droplet 上还跑着另一个项目。KruAI 的部署已经做到互不干扰：

- **不占端口。** KruAI 不映射任何主机端口，不会和另一个项目抢 80、443、5432 这些端口。
- **名字分开。** KruAI 的容器、网络、数据卷都以 `kruai` 开头，用 `docker ps` 可以一眼分清。
- **内存有上限。** 每个服务都设了上限，全部加起来最多 1.7GB，实际占用约 175MB。即使 KruAI 出了问题，也挤不掉另一个项目。

**维护时必须遵守：**

- **只用带 `-f deploy/compose.yml` 的命令。** 这样的命令只作用于 KruAI，第四节里的命令都是这样写的。
- **绝对不要运行下面这三条命令：**
  - `docker system prune --volumes`
  - `docker volume prune`
  - `docker system prune -a`

  另一个项目的容器只要处于停止状态，它的数据卷和镜像就会被这几条命令当作"没人用"而删掉，删了无法恢复。
- **要清理磁盘，只用下面这一条。** 它只删除没有标签的悬空镜像，不碰任何数据卷：

```bash
docker image prune
```

- **重启服务器会同时影响两个项目。** KruAI 的服务会自动重新启动，另一个项目是否会自动启动，取决于它自己的设置。

**部署前先看一眼服务器的现状（服务器）：**

```bash
free -h; df -h /; nproc; lsb_release -ds; docker --version; docker compose version; docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
```

把输出发给我，我来确认内存、硬盘和 Docker 版本是否够用。这条命令只读不改，输出里也不含任何密钥。
