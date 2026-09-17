"""Which arm a learner is in, derived from their id and nothing else.

BACKLOG C7 writes this as ``hash(user_id + key)``, and Python's built-in
``hash`` is the one function that must not be used for it. PEP 456 salts the
hash of str and bytes with a per-process random seed, so the same learner
buckets differently in every uvicorn worker::

    $ for i in 1 2 3; do PYTHONHASHSEED=$i python3 -c \
        "print(hash('explain_media:3f2504e0-...') % 1000)"; done
    747
    208
    399

Nothing would fail. The learner sees video on one request and audio on the next,
their completion is counted under whichever arm answered last, and PRD 7.2's
comparison — the one deciding a $375-1,500 commitment — is run on a sample where
the arms are not arms. SHA-256 is stable across processes, machines, platforms
and Python versions, which is the entire requirement (docs/DECISIONS.md D-020).

Deliberately pure: no session, no settings lookup beyond the values passed in,
no clock. ARCHITECTURE section 1 puts ``services/experiments`` in the domain
layer, and unlike ``entitlements`` (docs/DECISIONS.md D-018) there is no reason
this part cannot honour that — persistence lives next door in assign.py.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.core.config import Settings
from app.services.media import VARIANT_AUDIO, VARIANT_VIDEO

#: Bits of the digest used as the bucket. 48 rather than 64 for an exact reason:
#: a float64 has a 53-bit mantissa, so ``(2**64 - 1) / 2**64`` rounds to exactly
#: 1.0 and the user holding that digest would fall outside a 100% rollout. At 48
#: bits every fraction is exact and the largest is 0.9999999999999964, so
#: ``fraction < 1.0`` is true for every possible learner. 2.8e14 buckets is more
#: resolution than any split will ever ask for.
_BUCKET_BYTES: Final = 6
_BUCKET_SPACE: Final = 1 << (_BUCKET_BYTES * 8)


class UnknownExperimentError(LookupError):
    """No definition for this experiment key.

    Raised rather than bucketing anyway. An unregistered key has no enabled flag
    and no split to read, so "assign it" could only mean inventing both, and the
    rows would look exactly like real assignments in the S3 analysis.
    """


@dataclass(frozen=True)
class ExperimentDefinition:
    """One A/B: its two arms and where its configuration lives.

    ``read_enabled`` and ``read_split`` are passed to the constructor, so they
    are instance attributes holding plain functions — not methods, and not bound
    to the definition.
    """

    #: experiments.experiment_key. Stored, so it is permanent once rows exist.
    key: str
    #: The arm that is today's behaviour. Everyone gets this while the split is 0.
    control: str
    #: The arm under test. The split is the fraction assigned here.
    treatment: str
    read_enabled: Callable[[Settings], bool]
    read_split: Callable[[Settings], float]


#: PRD 7.2's S2-to-S3 gate: does the video explainer lift completion by >= 15%?
#:
#: The arms are imported from services.media rather than restated, so the string
#: written to the table and the string media.resolve() compares against cannot
#: drift apart. Both are domain-layer modules and media does not import this
#: one, so there is no cycle.
EXPLAIN_MEDIA: Final = ExperimentDefinition(
    key="explain_media",
    control=VARIANT_AUDIO,
    treatment=VARIANT_VIDEO,
    read_enabled=lambda s: s.experiment_explain_media_enabled,
    read_split=lambda s: s.experiment_explain_media_split,
)

DEFINITIONS: Final[dict[str, ExperimentDefinition]] = {EXPLAIN_MEDIA.key: EXPLAIN_MEDIA}


def definition(experiment_key: str) -> ExperimentDefinition:
    """Look up a registered experiment.

    Raises:
        UnknownExperimentError: the key has no definition.
    """
    try:
        return DEFINITIONS[experiment_key]
    except KeyError:
        raise UnknownExperimentError(
            f"no experiment named {experiment_key!r}; registered: {sorted(DEFINITIONS)}"
        ) from None


def bucket_fraction(user_id: uuid.UUID, *, experiment_key: str) -> float:
    """Where this learner falls in [0, 1) for this experiment.

    The key is part of the digest so that assignments are independent across
    experiments. Hashing the id alone would put the same learners in the
    treatment arm of every A/B ever run, and two experiments overlapping in time
    would silently measure each other.

    The key comes first and the id last, and a UUID renders as a fixed 36
    characters from a fixed alphabet — so the split point is unambiguous
    whatever a future key contains, with no escaping needed.
    """
    digest = hashlib.sha256(f"{experiment_key}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:_BUCKET_BYTES], "big") / _BUCKET_SPACE


def choose_variant(
    user_id: uuid.UUID, *, experiment: ExperimentDefinition, settings: Settings
) -> str | None:
    """The arm this learner would be assigned to, or None if the experiment is off.

    None rather than the control arm. An experiment that is switched off is not
    an experiment everybody lost — media.resolve() reads None as "the A/B is not
    running, decide on eligibility alone", and returning ``control`` there would
    hold video back from paying learners with nothing recording why
    (docs/DECISIONS.md D-022).

    ``split`` is the share assigned to the treatment arm, and the comparison is
    strict — which is what makes both ends of the range mean what they say: at
    0.0 no learner is in the treatment arm, and at 1.0 every learner is
    (docs/DECISIONS.md D-021).
    """
    if not experiment.read_enabled(settings):
        return None

    split = experiment.read_split(settings)
    fraction = bucket_fraction(user_id, experiment_key=experiment.key)
    return experiment.treatment if fraction < split else experiment.control
