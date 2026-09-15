"""发音评测 Provider 抽象。

规范性文件 —— 原样复制到 backend/app/services/scoring/base.py，签名不得改动。

背景：M0-1 验证尚未完成，最终选型（Azure / 科大讯飞 ISE / 自建 MFA）未定。
所有调用方只依赖本文件的抽象，不得直接引用任何厂商 SDK。

中文与英文的差异必须在 Provider 内部吸收，调用方看到的结构一致：
  - 英文（en-US）有 prosody_score，中文（zh-CN）没有
  - 中文的声调正确性由 tone_score 表达，其来源由 M0-1 结论决定
    （音素串直接带声调数字 → 解析；否则 → 从音素级结果推导）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Language(str, Enum):
    ZH_CN = "zh-CN"
    EN_US = "en-US"


class AssessMode(str, Enum):
    #: 已知参考文本（跟读、句型替换）。准确率显著更高，能给 completeness。
    SCRIPTED = "scripted"
    #: 自由回答（Q&A、roleplay）。无参考文本。
    UNSCRIPTED = "unscripted"


@dataclass(frozen=True)
class PhonemeScore:
    phoneme: str
    accuracy: float                       # 0-100
    offset_ms: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class WordScore:
    word: str
    accuracy: float                       # 0-100
    error_type: str | None = None         # None | "Omission" | "Insertion" | "Mispronunciation"
    phonemes: tuple[PhonemeScore, ...] = ()


@dataclass(frozen=True)
class PronunciationResult:
    """一次开口的评测结果。

    调用方只应读取本结构，不得解析 raw。raw 仅用于排障与离线分析。
    """
    ok: bool
    asr_text: str = ""

    # 各维度分数，0-100。不适用的维度为 None，调用方必须处理 None。
    pron_score: float | None = None        # 综合分，mastery 计算的输入
    accuracy_score: float | None = None
    fluency_score: float | None = None
    completeness_score: float | None = None   # 仅 SCRIPTED
    prosody_score: float | None = None        # 仅 en-US
    tone_score: float | None = None           # 仅 zh-CN；来源见模块 docstring

    words: tuple[WordScore, ...] = ()

    #: 供应商原始响应，落 attempts.phoneme_detail，不得进入业务判断
    raw: dict[str, Any] = field(default_factory=dict)

    #: ok 为 False 时的机器可读原因
    error_code: str | None = None
    error_message: str | None = None

    #: 本次调用的估算成本（USD 美分整数）。调用方据此写 cost_ledger。
    cost_usd_cents: int = 0


class PronunciationScorer(ABC):
    """发音评测 Provider。

    实现类必须保证：
      1. 不抛异常 —— 所有失败以 ok=False + error_code 返回
      2. 幂等 —— 同一音频同一参数多次调用结果一致
      3. 自报成本 —— cost_usd_cents 必填，未知时保守高估
    """

    #: provider 标识，写入 cost_ledger.provider
    name: str

    @abstractmethod
    async def assess(
        self,
        audio: bytes,
        *,
        language: Language,
        mode: AssessMode,
        reference_text: str | None = None,
        audio_format: str = "wav",
    ) -> PronunciationResult:
        """评测一段音频。

        Args:
            audio: 音频字节。实现类负责必要的格式转换。
            language: 目标语言。
            mode: SCRIPTED 时 reference_text 必填。
            reference_text: 参考文本；mode 为 UNSCRIPTED 时必须为 None。
            audio_format: 输入格式提示（wav / ogg / m4a）。

        Returns:
            PronunciationResult，失败时 ok=False，不抛异常。
        """
        raise NotImplementedError

    @abstractmethod
    def supports(self, language: Language) -> bool:
        """该 provider 是否支持此语言。registry 据此在多 provider 间路由。"""
        raise NotImplementedError
