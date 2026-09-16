"""TTS Provider 抽象。

规范性文件 —— 原样复制到 backend/app/services/tts/base.py，签名不得改动。

背景：M0-2 高棉语音色选型未完成。Azure 可能没有 km-KH 音色，届时需切换到
Google Cloud TTS 或 ElevenLabs。调用方不得感知这一差异。

重要：TTS 只在**内容打包阶段**使用（pipeline/build_pack.py），运行时不调用。
运行时只读对象存储里已生成好的 URL。任何在请求链路里调用本接口的代码都是错的。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Voice(str, Enum):
    """逻辑音色标识。具体映射到哪个厂商音色由 provider 内部决定，
    映射表记录在 docs/tts-decision.md（M0-2 产出）。"""
    KM_NARRATOR = "km_narrator"      # 高棉语讲解旁白（主）
    KM_FEEDBACK = "km_feedback"      # 高棉语反馈短句（可与 narrator 同源）
    ZH_MODEL = "zh_model"            # 中文示范朗读
    EN_MODEL = "en_model"            # 英文示范朗读（v2）


@dataclass(frozen=True)
class SynthesisResult:
    ok: bool
    audio: bytes = b""
    audio_format: str = "mp3"
    duration_ms: int = 0
    #: 实际使用的厂商音色名，写入 media_assets.provider 便于追溯
    resolved_voice: str = ""
    error_code: str | None = None
    error_message: str | None = None
    cost_usd_cents: int = 0


class TTSProvider(ABC):
    """文本转语音 Provider。

    实现类必须保证：
      1. 不抛异常 —— 失败以 ok=False 返回
      2. 确定性 —— 同文本同音色多次合成，内容一致（允许字节级差异）
      3. 支持 speaking_rate，教学场景默认放慢
    """

    name: str

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        voice: Voice,
        speaking_rate: float = 1.0,
        audio_format: str = "mp3",
    ) -> SynthesisResult:
        """合成一段音频。

        Args:
            text: 待合成文本。高棉语文本为 Unicode U+1780–U+17FF 区段。
            voice: 逻辑音色。
            speaking_rate: 语速倍率。中文示范默认 0.9。
            audio_format: 输出格式。
        """
        raise NotImplementedError

    @abstractmethod
    def available_voices(self) -> dict[Voice, str]:
        """返回该 provider 能提供的逻辑音色 → 厂商音色名映射。
        不支持的逻辑音色不出现在返回值里，registry 据此路由。
        """
        raise NotImplementedError
