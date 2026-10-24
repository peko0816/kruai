# M0-1: 发音评测验证

回答一个问题：**能不能在"说得好的人"和"真实初学者"之间划出一条及格线。**

如果两组分数重叠，那条线放在哪里都没有意义——每一次学习者开口，分数都是噪声。
`SCORING_PASS_THRESHOLD` 的全部可信度建立在这个验证上。

## 你要准备的

一个文件夹，里面是录音 + 一份 `manifest.csv`：

```csv
file,reference_text,group
good-01.wav,我要水。,good
bad-01.wav,我要水。,bad
```

| 列 | 内容 |
|---|---|
| `file` | 录音文件名，**相对于 manifest 所在目录**（文件夹整个搬走也不会失效）|
| `reference_text` | 这条录音**本该说的**那句中文 |
| `group` | `good` 或 `bad` |

录音格式：**16 kHz、单声道、16-bit 的 wav**（或 ogg/opus），每条不超过 60 秒——这是 Azure 端点的要求。
手机录音通常是 44.1 / 48 kHz，脚本会在**发请求之前**拒掉并给出转换命令，不花钱：

```bash
ffmpeg -i 原始录音.m4a -ar 16000 -ac 1 -sample_fmt s16 good-01.wav
```

### 两组人怎么找

- **good 组**：中文说得好的人，约 20 句
- **bad 组**：**真实的柬埔寨初学者**，同样的 20 句

**bad 组不能找人假装。** 评测模型对"装出来的错"和真实 L1 口音的反应不一样，
用装的样本校准出来的及格线，上线后会系统性偏高或偏低——
而这件事要到有真实用户数据时才会发现（`docs/REAL_ENVIRONMENT.md` 第 3 项）。

## 怎么跑

```bash
make m0-scoring M0_MANIFEST=/path/to/manifest.csv
```

默认走 `SCORING_PROVIDER=fake`，不花钱，用来确认清单格式没问题。
接真实 provider 时**必须显式确认花钱**：

```bash
SCORING_PROVIDER=azure make m0-scoring \
    M0_MANIFEST=/path/to/manifest.csv ARGS="--confirm-spend"
```

计费运行前要先 `docker compose up -d` 并 `make migrate`：**每一次调用都写进 `cost_ledger`**
（CLAUDE.md 第 8 节），连不上账本就**一个请求都不发**，直接退出码 2。

## 它判什么

| 判据 | 要求 | 为什么 |
|---|---|---|
| 两组均值差 | ≥ 15 分 | 低于这个，及格线落在哪都是猜 |
| 重叠样本 | ≤ 20% | 均值差大但样本交错，等于没有可划线的位置 |
| 音素级数据非空 | 必须有 | E9 不论走哪条路都要用它 |
| 每条都打出分 | 必须 | 悄悄跳过失败的调用，会在一半调用挂掉的运行上报出漂亮结果 |

输出 `docs/m0-1-scoring.md`，并给出 `ZH_TONE_MODE` 的结论：

- **`parse`** —— provider 的音素串自带声调数字（`ni3`），E9 直接读
- **`derive`** —— 不带，E9 要从音素级准确率推导
- **`unknown`** —— 根本没返回音素，这本身就是结论

退出码 0 表示四条判据全过。

## 它不解决什么

**两组分得开，只说明"可以有一条线"，没说这条线画在哪。**

定 `SCORING_PASS_THRESHOLD` 等于在决定"一个说得够好的学习者，多久会被判一次不及格"。
而遗留约束 **L-7** 已经算过：这个数不能单独动——
`MASTERY_DELTA_BASE` 与三个 item_type 权重必须一起重标，
否则 `ease_factor` 会在掌握度还很低时就触底，间隔重复几乎不起作用。

---

# M0-2: 高棉语音色选型

回答：**Azure 的高棉语听起来像不像高棉语，以及两个声音各用在哪。**

原计划是三家对比。逐家查了官方声音列表之后（D-125）：

| 厂商 | 高棉语 TTS |
|---|---|
| **Azure** | ✅ `km-KH-SreymomNeural`（女声）、`km-KH-PisethNeural`（男声）|
| Google Cloud TTS | ❌ 语言表从 Kannada 直接跳到 Korean |
| ElevenLabs | ❌ 所有模型的语言表都从 Kazakh 跳到 Kirghiz |

所以比较的是**声音**：同一家厂商，指定不同的厂商音色，各合成一遍。
脚本的比较单位叫「候选」，写成 `厂商:声音`；只写厂商名也可以，比较厂商时照常用。

"盲"必须做出来而不是承诺出来：

| 做法 | 为什么 |
|---|---|
| 文件名是摘要，不含厂商也不含声音名 | 叫 `sreymom-p01.mp3` 就不用做测试了 |
| 还原用的 key 写在**音频目录之外** | 这样"把文件夹发出去"不可能连 key 一起发 |
| 每一段的几个 clip **各自打乱** | 固定顺序到第三段就不盲了——听者会发现第一个永远是同一把嗓子 |
| 打乱是按段落种子的 | 两个人导出同一批素材得到同一份表，一个分数能追回它对应的那条 |

## 素材

**已备好：`m0/samples/khmer-corpus.csv`，15 句。**

取自 Tatoeba（`corpus.tatoeba`，R7 白名单内），每句单独核过许可——15 句全部 **CC BY 2.0 FR**。
`source` 列逐句写明句子编号、作者和原始链接，这是 CC BY 的署名义务，不是装饰。

挑句子时看的是**它能测出 TTS 的什么毛病**，而不只是内容中性：

| 测什么 | 句数 |
|---|---|
| 疑问句语调、连续多个问句 | 3 |
| 两个句子连读、长复句、转折 | 4 |
| 数字（数词串、年份阿拉伯数字） | 2 |
| 带引号的对话 | 1 |
| 专有名词 | 1 |
| 普通陈述句 | 4 |

刻意避开了宗教、死亡、暴力内容——评审要连续听几十遍。

### 一个已知的混淆，评审要知道

这些句子多数是从英文译成高棉语的，有些措辞可能带翻译腔。
**请评审给"嗓子"打分，不给"句子"打分**：句子本身别扭不该扣 `naturalness`。
每个声音合成的都是同一批句子，所以翻译腔会同等地影响所有候选，**相对比较仍然成立**；
但它可能整体压低绝对分数，所以 ≥4 这条线要结合 `comment` 看。

### 为什么不用 MoEYS OER

原计划是 MoEYS OER，实地查看后不可用：页面正文没有高棉文，唯一像样的高棉语读物
不署作者也不署出处。署错名会违反 CC BY 的核心义务（D-124）。

## 两步，中间隔几天

```bash
# 一、合成 + 出盲听表
make m0-tts \
    M0_CANDIDATES=azure:km-KH-SreymomNeural,azure:km-KH-PisethNeural \
    ARGS="--confirm-spend"

# 二、两份以上填好的表回来之后
make m0-tts-score SHEETS="r1.csv r2.csv"
```

第一步和 M0-1 一样要先起数据库（`docker compose up -d` + `make migrate`），理由相同：
每条合成都记账，账本不可写就不开始。

第一步产出 `m0/tts-blind/`（音频 + `scoring-sheet.csv`）和 `m0/tts-blind-key.csv`。
**只发前者。**

## 评分表

每条 clip 两个轴，各 1–5 分：

| 轴 | 问的是 |
|---|---|
| `naturalness` | 像不像人说的 |
| `intelligibility` | 听不听得清 |

**两个轴分开打，不合成一个总分。** 一把嗓子可以字字清楚却像机器，
这两种毛病对课程的后果不一样，合并打分会让其中一种蒙混过关。

另有 `fatal_error`（填 `yes`）：读错到不能用的程度。**它一票否决**——
再好听的嗓子，把词读错了也没法用。

## 验收

- 两个轴的均值都 ≥ 4
- **≥ 2 位母语者独立评分**（一个人的偏好不构成关于一门语言的证据）
- 零 `fatal_error`

产出 `docs/tts-decision.md`，含**逻辑音色 → 厂商音色**的映射表。
E8 读那张表；表没填，`TTS_PROVIDER` 就没有东西可以解析逻辑音色。

---

# Azure 验证实现（`m0/azure/`）

两个脚本里 `azure` 这个名字解析到 `m0/azure/`，**不在 `app/services/`**（D-126）：
CLAUDE.md 第 3 节在 M0 出结论前不允许真实 provider 实现，而 M0 要出结论就得调 Azure。
`fake` 照旧走应用自己的 registry，行为与运行时完全一致。

| | |
|---|---|
| 资源 | `ai-tutor-speech`，区域 `eastasia`，免费档 F0 |
| 走的接口 | REST（不是 Speech SDK）：TTS 用 `cognitiveservices/v1` + SSML；评测用短音频识别端点 + `Pronunciation-Assessment` 头 |
| 限流 | F0 每分钟约 20 次。遇到 429 按 `Retry-After` 等，最多试 3 次 |
| 成本 | F0 实际不收费；记账按标价估：TTS $15／百万字符（逐条向下取整）、评测按 $2／小时上限（逐条向上取整） |
| **没验证过的** | 评测返回的 JSON 结构只对着微软文档写过，两种文档写法都能读；读不懂时报 `scoring.malformed_response` 并把原文留在 `raw`。**第一次真实调用要有人盯着** |

M0 出结论后，留下的那个按 E8 / E9 搬进 `app/services/`，各加一行 registry；这个目录随之删除。

