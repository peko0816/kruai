# KruAI 开发启动包

交给 Claude Code 的完整上下文。**它会据此自主建仓并开始开发。**

## 怎么用

1. 新建一个空目录（比如 `kruai/`）
2. 把本包所有内容复制进去
3. 在该目录启动 Claude Code，第一句话说：

> 读 CLAUDE.md 和 docs/ 下的全部文档，然后按 docs/BACKLOG.md 从 A1 开始做。
> 每完成一个条目停下来汇报，对照 docs/DEFINITION_OF_DONE.md 的检查单自检。

Claude Code 会自己创建目录结构、写代码、建迁移。

## 包含什么

```
├── CLAUDE.md                  ★ 项目宪法。每次会话必读，优先级高于通用最佳实践
├── README.md                    本文件
├── .env.example                 完整配置示例
├── docs/
│   ├── PRD.md                 ★ 产品规格 v1.1，唯一权威
│   ├── ARCHITECTURE.md          分层、数据流、provider 契约、目录结构
│   ├── DATA_MODEL.sql         ★ 权威 DDL，建表以它为准
│   ├── CODING_STANDARDS.md      工具链、类型、金额、错误、日志、测试、命名
│   ├── CONFIG_REFERENCE.md    ★ 配置清单。判断「能不能硬编码」的唯一依据
│   ├── DEFINITION_OF_DONE.md    PR 检查单 + 各里程碑验收标准
│   ├── BACKLOG.md             ★ 有序任务清单，标注 M0 阻塞项与关卡
│   └── DECISIONS.md             实施方自行决策的记录（初始为空）
└── interfaces/                ★ 规范性接口，须原样复制到目标路径，签名不得改
    ├── scoring_base.py          → backend/app/services/scoring/base.py
    ├── tts_base.py              → backend/app/services/tts/base.py
    ├── payments_base.py         → backend/app/services/payments/base.py
    └── llm_base.py              → backend/app/services/llm/base.py
```

## 一句话说明设计意图

M0 外部验证（发音评测选型 / 高棉语 TTS 选型 / 支付联通）**尚未完成**，但系统 80% 的代码不依赖这三个结论。

解法是把三个外部能力全部收敛到 Provider 抽象后面，**先写 Fake 实现**，让整条业务链路在没有任何真实供应商的情况下端到端跑通。M0 出结论后只新增实现类，不动调用方。

**验收这一设计是否成立的方法只有一条**：把 `.env` 里所有 `*_PROVIDER` 设为 `fake`，系统必须能完整跑通并通过全部测试。做不到就说明有地方绕过了抽象。

## 并行推进的另一条线

M0 验证套件是独立交付的（`KruAI-M0-verification.zip`），三个脚本不依赖本工程。其中 **M0-1 需要先找中文母语者录 20 条音频**，是唯一阻塞在他人身上的事项，应尽早安排。
