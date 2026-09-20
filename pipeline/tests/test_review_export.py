"""The review sample (BACKLOG E5).

What matters about a sample is not that it is random but that it is the right
shape and the same every time: M1 is decided on a pass rate over these rows, so
a sample that under-represents the Khmer prose, or that differs between two
people who exported it, cannot support the number it produces.
"""

from __future__ import annotations

import csv
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from pipeline import review_export
from pipeline.draft import Draft
from pipeline.review_export import (
    COLUMNS,
    ENCODING,
    STRATA,
    VERDICTS,
    instructions,
    sample_size,
    select,
    write_csv,
)

KM = "ខ្ញុំចង់បានទឹក"
ROOT = Path(__file__).resolve().parents[2]


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql://unused/unused",
        "redis_url": "redis://unused",
        "telegram_bot_token": "",
        "azure_speech_key": "",
        "azure_speech_region": "",
        "google_application_credentials": "",
        "elevenlabs_api_key": "",
        "openai_api_key": "",
        "payway_merchant_id": "",
        "payway_api_key": "",
        "payway_base_url": "",
        "bakong_token": "",
        "jwt_secret": "x" * 32,
    }
    return Settings(**{**base, **overrides})


def a_draft(
    *, concepts: int = 10, sentences: int = 8, substitutions: int = 12, dialogues: int = 3
) -> Draft:
    return Draft.model_validate(
        {
            "language": "zh",
            "level": "HSK1",
            "licence": "public standards",
            "sources": [{"id": "zh.standard.2021"}],
            "seed_file": "test.yaml",
            "provider": "fake",
            "model": "fake",
            "generated_at": "2026-09-20T00:00:00Z",
            "usage": {},
            "concepts": [
                {
                    "slug": f"zh.hsk1.c{number}",
                    "pattern": "我要 + [名词]",
                    "hskk_task_types": ["listen_and_repeat"],
                    "km_explanation": f"{KM} {number}",
                    "target_sentences": [
                        {"zh": f"我要水{i}", "pinyin": "wǒ yào shuǐ", "km_gloss": KM}
                        for i in range(sentences)
                    ],
                    "substitutions": [
                        {"zh": f"水{i}", "pinyin": "shuǐ", "km_gloss": KM}
                        for i in range(substitutions)
                    ],
                    "dialogues": [
                        {
                            "hskk_task_type": "listen_and_repeat",
                            "prompt_zh": f"你要水{i}",
                            "prompt_km": KM,
                            "sample_answer_zh": "我要水",
                        }
                        for i in range(dialogues)
                    ],
                }
                for number in range(concepts)
            ],
        }
    )


def counts(rows: list[Any]) -> dict[str, int]:
    return {stratum: sum(1 for row in rows if row.kind == stratum) for stratum in STRATA}


# ------------------------------------------------------------ stratification


def test_every_kind_of_content_is_represented() -> None:
    """D-081: the Khmer explanations are a stratum, not a tenth of one pool."""
    rows = select(a_draft(), rate=0.10)

    assert all(counts(rows)[stratum] > 0 for stratum in STRATA)


def test_each_stratum_is_sampled_at_the_rate_not_the_corpus() -> None:
    draft = a_draft(concepts=10)  # 10 explanations, 80 sentences, 120 subs, 30 dialogues

    rows = counts(select(draft, rate=0.10))

    assert rows == {
        "km_explanation": 1,
        "target_sentence": 8,
        "substitution": 12,
        "dialogue": 3,
    }


def test_the_prose_would_be_swamped_without_stratification() -> None:
    """Why D-081 exists, as a number.

    One pool of 240 rows at 10% is 24 rows, of which the explanations are a
    tenth by population — one or two — and could easily be none at all.
    Stratified, they are a guaranteed share.
    """
    draft = a_draft(concepts=10)
    total = sum(counts(select(draft, rate=1.0)).values())

    explanations = counts(select(draft, rate=0.10))["km_explanation"]

    assert total == 240
    assert explanations == math.ceil(10 * 0.10)


def test_a_stratum_smaller_than_the_rate_still_gets_a_row() -> None:
    """Ten percent of three dialogues is nought, and nought is no evidence."""
    draft = a_draft(concepts=1, sentences=1, substitutions=1, dialogues=3)

    assert counts(select(draft, rate=0.10)) == {
        "km_explanation": 1,
        "target_sentence": 1,
        "substitution": 1,
        "dialogue": 1,
    }


@pytest.mark.parametrize(
    ("population", "rate", "expected"),
    [(0, 0.1, 0), (10, 0.0, 0), (10, 0.1, 1), (48, 0.1, 5), (3, 0.1, 1), (10, 1.0, 10)],
)
def test_sample_size_rounds_up_and_never_exceeds_the_population(
    population: int, rate: float, expected: int
) -> None:
    assert sample_size(population, rate) == expected


# --------------------------------------------------------------- the rate


def test_the_rate_is_configurable() -> None:
    draft = a_draft(concepts=10)

    assert len(select(draft, rate=1.0)) == 240
    assert len(select(draft, rate=0.5)) == 120
    assert select(draft, rate=0.0) == []


def test_the_default_rate_comes_from_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        review_export, "get_settings", lambda: settings(pipeline_review_sample_rate=0.5)
    )
    draft = tmp_path / "zh-HSK1.draft.json"
    draft.write_text(a_draft(concepts=10).model_dump_json(), encoding="utf-8")

    review_export.main([str(draft)])

    assert "120 row(s)" in capsys.readouterr().out


# ------------------------------------------------------------- determinism


def test_the_same_draft_gives_the_same_sample() -> None:
    draft = a_draft()

    first = [row.id for row in select(draft, rate=0.10)]
    second = [row.id for row in select(draft, rate=0.10)]

    assert first == second


def test_a_seed_draws_a_different_sample_of_the_same_size() -> None:
    """For a second review round over the same content."""
    draft = a_draft(concepts=20)

    default = {row.id for row in select(draft, rate=0.10)}
    reseeded = {row.id for row in select(draft, rate=0.10, seed=7)}

    assert len(default) == len(reseeded)
    assert default != reseeded


def test_the_sample_does_not_change_between_processes(tmp_path: Path) -> None:
    """The seed must not come from hash(), which Python randomises per process.

    Run in subprocesses with different PYTHONHASHSEED rather than asserted
    against the implementation: what matters is the observable property, and
    this is the one way to see it.
    """
    draft = tmp_path / "zh-HSK1.draft.json"
    draft.write_text(a_draft(concepts=12).model_dump_json(), encoding="utf-8")

    outputs = []
    for hash_seed in ("0", "1"):
        out = tmp_path / f"sample-{hash_seed}.csv"
        environment = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(ROOT)}
        subprocess.run(
            [sys.executable, "-m", "pipeline.review_export", str(draft), "--out", str(out)],
            check=True,
            capture_output=True,
            env=environment,
        )
        outputs.append(out.read_text(encoding=ENCODING))

    assert outputs[0] == outputs[1]


# --------------------------------------------------------------------- CSV


def test_the_file_a_reviewer_opens_has_the_columns_they_need(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    rows = select(a_draft(), rate=0.10)

    write_csv(rows, path)
    with path.open(encoding=ENCODING, newline="") as handle:
        read_back = list(csv.DictReader(handle))

    assert list(read_back[0]) == list(COLUMNS)
    assert all(row["verdict"] == "" and row["comment"] == "" for row in read_back)
    assert any(KM in row["khmer"] for row in read_back)


def test_the_file_opens_as_utf8_in_a_spreadsheet(tmp_path: Path) -> None:
    """D-093: without the byte-order mark Excel renders Khmer as mojibake."""
    path = tmp_path / "review.csv"

    write_csv(select(a_draft(), rate=0.10), path)

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_every_row_can_be_traced_back_to_the_item_it_came_from() -> None:
    """The id is how a returned verdict is matched, so it has to locate one item."""
    draft = a_draft(concepts=3)
    slugs = {concept.slug for concept in draft.concepts}

    for row in select(draft, rate=0.5):
        slug, _, where = row.id.partition("/")
        assert slug in slugs
        assert where.startswith(
            ("km_explanation", "target_sentences[", "substitutions[", "dialogues[")
        )


def test_a_dialogue_row_shows_both_halves_of_the_turn() -> None:
    rows = [row for row in select(a_draft(), rate=1.0) if row.kind == "dialogue"]

    assert "→" in rows[0].chinese


def test_the_instructions_name_every_verdict_a_reviewer_may_write(tmp_path: Path) -> None:
    text = instructions(tmp_path / "review.csv", select(a_draft(), rate=0.10))

    assert all(verdict in text for verdict in VERDICTS)


def test_rows_come_out_in_draft_order() -> None:
    """A reviewer reads down the file; jumping between concepts wastes their time."""
    draft = a_draft(concepts=5)
    order = [concept.slug for concept in draft.concepts]

    seen = [row.concept for row in select(draft, rate=0.5)]

    assert seen == sorted(seen, key=order.index)


# --------------------------------------------------------------------- CLI


def test_an_impossible_rate_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(review_export, "get_settings", settings)
    draft = tmp_path / "zh-HSK1.draft.json"
    draft.write_text(a_draft().model_dump_json(), encoding="utf-8")

    exit_code = review_export.main([str(draft), "--rate", "1.5"])

    assert exit_code == 2
    assert "between 0 and 1" in capsys.readouterr().err


def test_the_csv_lands_beside_the_draft_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(review_export, "get_settings", settings)
    draft = tmp_path / "zh-HSK1.draft.json"
    draft.write_text(a_draft().model_dump_json(), encoding="utf-8")

    review_export.main([str(draft)])

    assert (tmp_path / "zh-HSK1.review.csv").exists()
