"""文本 LLM Provider 抽象。

规范性文件 —— 原样复制到 backend/app/services/llm/base.py，签名不得改动。

使用边界（红线 R1）：
  ✅ 运行时：Q&A 回合制的对话回应与纠错说明
  ✅ 离线：pipeline/generate.py 的内容批量生成
  ❌ 运行时生成课程内容（讲解、目标句、替换项、对话任务）—— 一律离线预生成

判断方法：如果这段文本会被多个用户看到同样一份，它就必须是离线生成并固化的。
只有针对单个用户当次输入的即时回应才允许运行时生成。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Purpose(str, Enum):
    """调用目的。registry 据此选择模型档位并做成本归因。"""
    #: 运行时：对话回应 + 纠错说明。要求低延迟、低成本。
    RUNTIME_DIALOGUE = "runtime_dialogue"
    #: 离线：内容批量生成。走 Batch API，不要求延迟。
    OFFLINE_GENERATION = "offline_generation"


@dataclass(frozen=True)
class Message:
    role: str          # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class CompletionResult:
    ok: bool
    text: str = ""
    #: 请求 json_schema 时的解析结果
    parsed: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    model: str = ""
    error_code: str | None = None
    error_message: str | None = None
    cost_usd_cents: int = 0


class LLMProvider(ABC):
    """文本生成 Provider。

    实现类必须保证：
      1. 不抛异常 —— 失败以 ok=False 返回
      2. system 提示逐字稳定时命中 prompt cache，并在 cached_input_tokens 中体现
      3. json_schema 非空时保证返回可解析，否则 ok=False
    """

    name: str

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        purpose: Purpose,
        max_output_tokens: int = 512,
        temperature: float = 0.3,
        json_schema: dict[str, Any] | None = None,
    ) -> CompletionResult:
        raise NotImplementedError

    @abstractmethod
    async def batch_complete(
        self,
        batches: list[list[Message]],
        *,
        purpose: Purpose = Purpose.OFFLINE_GENERATION,
        max_output_tokens: int = 2048,
        temperature: float = 0.7,
        json_schema: dict[str, Any] | None = None,
    ) -> list[CompletionResult]:
        """批量生成，供内容管线使用。

        实现类应尽可能走厂商的 Batch API（成本更低）。
        返回顺序必须与入参一一对应；失败项返回 ok=False 占位，不得丢弃。
        """
        raise NotImplementedError
