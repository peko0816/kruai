"""What generate.py produces: a draft pack, before review and before media.

BACKLOG E3. The draft is the seed plus everything a language model wrote for
it — target sentences, substitution items, dialogue tasks, the Khmer
explanation and pinyin. validate.py (E4) is what judges the content; this
module only says what shape it has, so that "the generator produced something
usable" is a question with a mechanical answer.

It is a separate file from the seed for one reason: the seed is written by a
person and lives in git, the draft is machine output and is regenerated. Mixing
them would mean a regeneration rewrites hand-typed work.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from pipeline.seed_schema import HskkTaskType

#: Bumped when the draft's shape changes incompatibly, so build_pack.py can
#: refuse a draft it does not understand instead of half-reading it.
DRAFT_VERSION: Final = 1

#: C0 controls and DEL, less the three that are ordinary text. A model that
#: loses its footing mid-answer emits these, and nothing downstream wants them:
#: PostgreSQL refuses NUL in a text column outright, and the rest reach a
#: learner's screen as nothing at all.
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _no_control_characters(value: str) -> str:
    """Refuse text a model garbled, at the point the draft is assembled.

    This is not a ninth rule for validate.py: a field holding a NUL byte is
    malformed rather than non-compliant, and the difference matters for where
    it is caught. Refused here, generate.py records the concept as a failure
    and the existing retry and repair machinery asks again. Allowed through, it
    survives validation, is paid for in TTS, and fails at the database — the
    one place where the fix is most expensive and the error says least.
    """
    found = _CONTROL.search(value)
    if found is not None:
        raise ValueError(
            f"contains the control character {hex(ord(found.group()))} at "
            f"position {found.start()}; the model garbled this field"
        )
    return value


#: Every text field a model writes. Non-empty, and free of what it emits when
#: it loses its footing.
Text = Annotated[str, Field(min_length=1), AfterValidator(_no_control_characters)]


class TargetSentence(BaseModel):
    """One sentence a learner will be asked to say (PRD 3.3 step 2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    zh: Text
    #: Tone-marked pinyin, diacritics rather than digits: nǐ hǎo, not ni3 hao3.
    #: It is what a learner reads, and validate.py regenerates it with pypinyin
    #: and compares (PRD 6.2), so the two have to agree on a notation.
    pinyin: Text
    #: Khmer gloss. Drafted by the model, reviewed by a native speaker (D-081).
    km_gloss: Text


class Substitution(BaseModel):
    """One filler for the slot in a pattern (PRD 3.1, Vocab Builder)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    zh: Text
    pinyin: Text
    km_gloss: Text


class Dialogue(BaseModel):
    """One conversational task, in the form HSKK grades (PRD 5.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hskk_task_type: HskkTaskType
    #: What the learner hears or reads.
    prompt_zh: Text
    #: The same, in Khmer, so a beginner knows what is being asked.
    prompt_km: Text
    #: A model answer. Not the only acceptable one — scoring is by
    #: pronunciation, not by string match — but drills need something to show.
    sample_answer_zh: Text


class ConceptDraft(BaseModel):
    """Everything generated for one concept, plus the seed fields it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(min_length=1)
    standard_ref: str = ""
    pattern: str = Field(min_length=1)
    hskk_task_types: tuple[HskkTaskType, ...] = Field(min_length=1)
    #: Khmer, drafted. The seed carried a placeholder (D-079); this is the
    #: draft that replaces it and that a native speaker then reviews (D-081).
    km_explanation: Text
    target_sentences: tuple[TargetSentence, ...] = Field(min_length=1)
    substitutions: tuple[Substitution, ...] = Field(min_length=1)
    dialogues: tuple[Dialogue, ...] = Field(min_length=1)


class Usage(BaseModel):
    """What the run cost, in the units the provider reports.

    Carried in the draft rather than written to cost_ledger here: the pipeline
    is a CLI that does not open the application's database (ARCHITECTURE 2.3),
    and content production is a one-off cost that PRD 11.3 wants recorded under
    ref='content_production'. import_pack.py is the stage that has a database
    connection, so that is where these become ledger rows (D-084).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd_cents: int = 0


class Failure(BaseModel):
    """A concept the provider could not produce. Kept, not dropped.

    PRD 6.1 sends failures back round for regeneration, which needs a record of
    which ones they were.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    error_code: str = ""
    error_message: str = ""


class Draft(BaseModel):
    """One generation run over one seed file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_version: int = DRAFT_VERSION
    language: str
    level: str
    #: Provenance, copied from the seed. R7 travels with the content: a draft
    #: that lost its sources could not be imported (PRD 5.5) and nobody would
    #: know where its sentences came from.
    licence: str = Field(min_length=1)
    sources: tuple[dict[str, Any], ...] = Field(min_length=1)
    seed_file: str
    provider: str
    model: str
    generated_at: dt.datetime
    usage: Usage
    concepts: tuple[ConceptDraft, ...] = ()
    failures: tuple[Failure, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.failures
