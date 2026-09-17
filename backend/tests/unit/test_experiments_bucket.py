"""Bucketing: stable, uniform, and independent between experiments.

BACKLOG C7's acceptance is "the same learner buckets the same way every time",
and the version of that which matters is not "twice in one process" — it is
across processes, because production runs several uvicorn workers and the whole
point of the table is a number compared weeks later.

So the stability test runs the real function in subprocesses under different
values of PYTHONHASHSEED, and a companion test shows the built-in ``hash`` that
BACKLOG literally names failing that same check. The failure is silent: no
error, no log line, just an A/B whose arms are not arms.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.experiments import (
    DEFINITIONS,
    EXPLAIN_MEDIA,
    UnknownExperimentError,
    bucket_fraction,
    choose_variant,
    definition,
)
from app.services.experiments.bucket import _BUCKET_SPACE
from app.services.media import VARIANT_AUDIO, VARIANT_VIDEO

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Deterministic learners. uuid4 would work — the digest does not care — but a
#: fixed sequence makes the distribution assertions below either always pass or
#: always fail, rather than failing one run in ten thousand.
POPULATION = [uuid.UUID(int=i) for i in range(20_000)]

ALICE = uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301")

_MINIMAL: dict[str, str] = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "TELEGRAM_BOT_TOKEN": "",
    "AZURE_SPEECH_KEY": "",
    "AZURE_SPEECH_REGION": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "ELEVENLABS_API_KEY": "",
    "OPENAI_API_KEY": "",
    "PAYWAY_MERCHANT_ID": "",
    "PAYWAY_API_KEY": "",
    "PAYWAY_BASE_URL": "",
    "BAKONG_TOKEN": "",
    "JWT_SECRET": "",
}


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def stopped(**overrides: str) -> Settings:
    """Configuration as it ships: the A/B has not started."""
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


def running(split: float = 0.5, **overrides: str) -> Settings:
    """Configuration with the explain-media A/B switched on."""
    values: dict[str, Any] = {
        **_MINIMAL,
        "EXPERIMENT_EXPLAIN_MEDIA_ENABLED": "true",
        "EXPERIMENT_EXPLAIN_MEDIA_SPLIT": str(split),
        **overrides,
    }
    return Settings(_env_file=None, **values)


def variant_of(user_id: uuid.UUID, settings: Settings) -> str | None:
    return choose_variant(user_id, experiment=EXPLAIN_MEDIA, settings=settings)


def in_subprocess(body: str, *, hash_seed: str) -> str:
    """Run a snippet in a fresh interpreter with a chosen hash seed."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        check=True,
        cwd=BACKEND_ROOT,
        env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
    )
    return result.stdout.strip()


# -------------------------------------------------------- the named acceptance


def test_the_same_learner_buckets_the_same_way_every_time() -> None:
    """BACKLOG C7's acceptance, in the weak in-process form."""
    settings = running()
    answers = {variant_of(ALICE, settings) for _ in range(100)}

    assert len(answers) == 1


def test_the_assignment_survives_a_new_process() -> None:
    """The acceptance in the form that matters.

    Production runs several workers, and an assignment is compared against a
    completion rate recorded weeks later. A bucket that is only stable within
    one process is not stable.
    """
    snippet = """
        import uuid
        from app.services.experiments import bucket_fraction
        print(bucket_fraction(
            uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
            experiment_key="explain_media",
        ))
    """
    answers = {in_subprocess(snippet, hash_seed=seed) for seed in ("0", "1", "2", "random")}

    assert answers == {"0.3003923090806033"}


def test_the_builtin_hash_would_not_have_survived_it() -> None:
    """Why ``hash(user_id + key)`` is not what got implemented.

    PEP 456 salts str and bytes hashing per process. The same learner lands in a
    different bucket in every worker, no exception is raised, and the damage
    only shows up as an A/B result that cannot be trusted and cannot be redone.
    """
    snippet = """
        print(hash("explain_media:3f2504e0-4f89-11d3-9a0c-0305e82c3301") % 1000)
    """
    answers = {in_subprocess(snippet, hash_seed=seed) for seed in ("1", "2", "3")}

    assert len(answers) > 1, "PYTHONHASHSEED no longer varies str hashing; revisit bucket.py"


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [
        (uuid.UUID(int=0), 0.9569582795865372),
        (ALICE, 0.3003923090806033),
        (uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"), 0.48692132417248146),
    ],
)
def test_known_learners_land_on_known_fractions(user_id: uuid.UUID, expected: float) -> None:
    """Pinned so that changing the hash is a decision, not an accident.

    Rewriting this function re-buckets every learner who has not been assigned
    yet, which mid-experiment mixes the arms. These values make that show up as
    a failing test instead of a quiet shift in the numbers.
    """
    assert bucket_fraction(user_id, experiment_key="explain_media") == expected


# ------------------------------------------------------------ the split itself


@pytest.mark.parametrize(("split", "measured"), [(0.25, 0.2479), (0.5, 0.4966), (0.75, 0.74475)])
def test_the_split_is_honoured_across_a_population(split: float, measured: float) -> None:
    """A uniform digest, checked rather than assumed: a bucketing that clumps
    would still be stable and still be wrong."""
    settings = running(split)
    share = sum(variant_of(u, settings) == VARIANT_VIDEO for u in POPULATION) / len(POPULATION)

    assert share == pytest.approx(measured, abs=1e-4)
    assert share == pytest.approx(split, abs=0.01)


def test_a_split_of_zero_assigns_nobody_to_video() -> None:
    """The strict comparison is what makes this exact. With ``<=`` the learner
    whose digest is 0 would be in a rollout described as 0%."""
    settings = running(0.0)

    assert all(variant_of(u, settings) == VARIANT_AUDIO for u in POPULATION)


def test_a_split_of_one_assigns_everybody_to_video() -> None:
    """Exact only because the bucket is 48 bits wide. At 64 the largest fraction
    rounds to 1.0 in float64 and that learner falls out of a 100% rollout."""
    settings = running(1.0)

    assert all(variant_of(u, settings) == VARIANT_VIDEO for u in POPULATION)


def test_a_learner_exactly_on_the_split_is_in_the_control_arm() -> None:
    """The comparison is strict, pinned at the one place the difference shows.

    No population test can catch ``<=`` for ``<`` — it takes a learner whose
    bucket is exactly the split, and there is no such learner among twenty
    thousand. Setting the split to a known learner's own fraction makes one.
    Strictness is what gives a split of 0.0 its meaning.
    """
    edge = bucket_fraction(ALICE, experiment_key="explain_media")

    assert variant_of(ALICE, running(edge)) == VARIANT_AUDIO
    assert variant_of(ALICE, running(edge * 1.000001)) == VARIANT_VIDEO


def test_the_widest_possible_bucket_is_still_below_one() -> None:
    """Why the bucket is 48 bits and not the digest's full width.

    A float64 carries 53 bits of mantissa, so a 64-bit bucket cannot represent
    its own top value: it rounds to exactly 1.0, and that learner would sit
    outside a rollout described as 100%. The test asserts both halves so the
    narrower width reads as a choice rather than an oversight.
    """
    assert (_BUCKET_SPACE - 1) / _BUCKET_SPACE < 1.0
    assert (2**64 - 1) / 2**64 == 1.0


def test_every_fraction_is_inside_the_unit_interval() -> None:
    fractions = [bucket_fraction(u, experiment_key="explain_media") for u in POPULATION]

    assert all(0.0 <= f < 1.0 for f in fractions)


# ------------------------------------------------- independence between A/Bs


def test_two_experiments_bucket_the_same_learner_independently() -> None:
    """The key is in the digest for this reason.

    Hashing the id alone would put the same learners in the treatment arm of
    every experiment forever, so two A/Bs running at once would measure each
    other rather than their own change.
    """
    disagreements = sum(
        (bucket_fraction(u, experiment_key="explain_media") < 0.5)
        != (bucket_fraction(u, experiment_key="other_test") < 0.5)
        for u in POPULATION
    )

    assert disagreements / len(POPULATION) == pytest.approx(0.5, abs=0.01)


def test_the_experiment_key_changes_the_bucket() -> None:
    assert bucket_fraction(ALICE, experiment_key="explain_media") != bucket_fraction(
        ALICE, experiment_key="explain_media_v2"
    )


# ------------------------------------------------------------ on and off


def test_an_experiment_that_is_off_returns_no_variant() -> None:
    """None, not the control arm. media.resolve() reads None as "the A/B is not
    running", and handing it ``audio`` would hold video back from paying
    learners with nothing recording why."""
    assert variant_of(ALICE, stopped()) is None


def test_off_is_the_default() -> None:
    """EXPERIMENT_EXPLAIN_MEDIA_ENABLED defaults false; the A/B starts at S2."""
    assert stopped().experiment_explain_media_enabled is False


def test_the_off_switch_outranks_a_full_split() -> None:
    assert variant_of(ALICE, running(1.0, EXPERIMENT_EXPLAIN_MEDIA_ENABLED="false")) is None


# --------------------------------------------------------------- the registry


def test_the_arms_are_the_strings_media_compares_against() -> None:
    """One definition, imported rather than restated: a variant written to the
    table that media.resolve() does not recognise would degrade every video
    learner to audio and look like a preference."""
    assert EXPLAIN_MEDIA.control == VARIANT_AUDIO
    assert EXPLAIN_MEDIA.treatment == VARIANT_VIDEO


def test_the_experiment_key_matches_the_column_value() -> None:
    """Stored in experiments.experiment_key, so it is permanent once rows exist
    and cannot be renamed without a migration."""
    assert EXPLAIN_MEDIA.key == "explain_media"
    assert DEFINITIONS["explain_media"] is EXPLAIN_MEDIA


def test_an_unregistered_experiment_is_refused() -> None:
    """Bucketing it anyway would mean inventing an enabled flag and a split, and
    the rows would be indistinguishable from real assignments later."""
    with pytest.raises(UnknownExperimentError, match="no experiment named 'streak_nudge'"):
        definition("streak_nudge")


def test_the_error_lists_what_is_registered() -> None:
    with pytest.raises(UnknownExperimentError, match=r"\['explain_media'\]"):
        definition("typo_media")


def test_the_split_comes_from_configuration() -> None:
    """R3: moving the ratio is a config change, not an edit here."""
    alice_at_low = variant_of(ALICE, running(0.2))
    alice_at_high = variant_of(ALICE, running(0.8))

    assert alice_at_low == VARIANT_AUDIO
    assert alice_at_high == VARIANT_VIDEO
