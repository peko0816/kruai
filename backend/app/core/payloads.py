"""What ``lesson_items.payload`` holds, one shape per ``item_type``.

The payload is JSONB, so nothing in the database stops the importer from
writing one set of keys while the API and the bot read another — and until
D-130 that is exactly what happened. D-036 named ``target_text`` as the key a
scripted item is scored against; the importer wrote ``zh``. The bot guessed at
``target_text`` / ``text`` / ``prompt``. Every component passed its own tests,
because each test wrote the payload it expected to read. Against real imported
content every drill crashed the attempts endpoint and every step in the bot
was blank.

So the shape lives here, once — in ``core`` rather than ``models`` because the
bot reads it too, and the bot is an entry layer that reaches the system only
over HTTP: a payload's keys are part of the API's response, not a business
decision, and importing them does not pull in a single model. The importer builds payloads only through the
functions below, and the API and the bot read them only through the key
constants. A test that writes a payload by hand is testing a shape nobody
ships; tests use these builders too.
"""

from __future__ import annotations

from typing import Final, TypedDict

#: The sentence a scripted item is assessed against (D-036).
TARGET_TEXT: Final = "target_text"
PINYIN: Final = "pinyin"
KM_GLOSS: Final = "km_gloss"

PATTERN: Final = "pattern"
KM_EXPLANATION: Final = "km_explanation"

HSKK_TASK_TYPE: Final = "hskk_task_type"
PROMPT_ZH: Final = "prompt_zh"
PROMPT_KM: Final = "prompt_km"
SAMPLE_ANSWER_ZH: Final = "sample_answer_zh"


class ExplainPayload(TypedDict):
    pattern: str
    km_explanation: str


class SpokenPayload(TypedDict):
    """A drill or a vocab item: one Chinese line to say, with its reading and meaning."""

    target_text: str
    pinyin: str
    km_gloss: str


class QaPayload(TypedDict):
    hskk_task_type: str
    prompt_zh: str
    prompt_km: str
    sample_answer_zh: str


def explain_payload(*, pattern: str, km_explanation: str) -> ExplainPayload:
    return {"pattern": pattern, "km_explanation": km_explanation}


def spoken_payload(*, target_text: str, pinyin: str, km_gloss: str) -> SpokenPayload:
    return {"target_text": target_text, "pinyin": pinyin, "km_gloss": km_gloss}


def qa_payload(
    *, hskk_task_type: str, prompt_zh: str, prompt_km: str, sample_answer_zh: str
) -> QaPayload:
    return {
        "hskk_task_type": hskk_task_type,
        "prompt_zh": prompt_zh,
        "prompt_km": prompt_km,
        "sample_answer_zh": sample_answer_zh,
    }


__all__ = [
    "HSKK_TASK_TYPE",
    "KM_EXPLANATION",
    "KM_GLOSS",
    "PATTERN",
    "PINYIN",
    "PROMPT_KM",
    "PROMPT_ZH",
    "SAMPLE_ANSWER_ZH",
    "TARGET_TEXT",
    "ExplainPayload",
    "QaPayload",
    "SpokenPayload",
    "explain_payload",
    "qa_payload",
    "spoken_payload",
]
