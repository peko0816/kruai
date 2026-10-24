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
