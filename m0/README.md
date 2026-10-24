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

录音格式 wav / mp3 / m4a 均可，后缀会被当作格式提示传给 provider。

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

回答：**三家 TTS 里，哪一家的高棉语听起来像高棉语。**

文档上比不出来——要紧的是高棉人听着自不自然，而三家都不会如实说自己。
所以这是一次**盲听测试**，而"盲"这件事必须做出来而不是承诺出来：

| 做法 | 为什么 |
|---|---|
| 文件名是摘要，不含厂商 | 叫 `azure-p01.mp3` 就不用做测试了 |
| 还原用的 key 写在**音频目录之外** | 这样"把文件夹发出去"不可能连 key 一起发 |
| 每一段的三个 clip **各自打乱** | 固定顺序到第三段就不盲了——听者会发现第一个永远是同一把嗓子，从此在给厂商打分而不是给音频打分 |
| 打乱是按段落种子的 | 两个人导出同一批素材得到同一份表，一个分数能追回它对应的那条 |

## 素材

```csv
id,khmer,source
p01,សួស្ដី តើអ្នកសុខសប្បាយទេ?,kh.moeys.oer
```

**不能用还没写完的课程文案**（`REAL_ENVIRONMENT.md` 第 5 项）。
用 MoEYS OER（oer.moeys.gov.kh，CC-BY 3.0，在 R7 白名单内）的公开段落，取 10–15 条。

`source` 不是装饰：R7 要求仓库里每一段第三方文本都能说出来处，盲听素材也是第三方文本。
缺 source 的行会被拒绝。

## 两步，中间隔几天

```bash
# 一、合成 + 出盲听表
make m0-tts M0_CORPUS=/path/to/corpus.csv \
    M0_VENDORS=azure,google,elevenlabs ARGS="--confirm-spend"

# 二、两份以上填好的表回来之后
make m0-tts-score SHEETS="r1.csv r2.csv"
```

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
