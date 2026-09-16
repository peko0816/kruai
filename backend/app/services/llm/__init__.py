"""Text generation.

Red line R1 governs what may call this. Runtime use is limited to replies and
correction notes aimed at one learner's own input; anything many learners would
see identically is course content and must be generated offline and frozen into
a pack. base.py's module docstring states the test: if the same text reaches
more than one user, it does not belong here.

Exports the registry and base types only, never a concrete implementation
(ARCHITECTURE section 3).
"""

from app.services.llm.base import CompletionResult, LLMProvider, Message, Purpose
from app.services.llm.registry import available_providers, build_llm, get_llm

__all__ = [
    "CompletionResult",
    "LLMProvider",
    "Message",
    "Purpose",
    "available_providers",
    "build_llm",
    "get_llm",
]
