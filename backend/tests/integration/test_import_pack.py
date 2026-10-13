"""Importing a pack into an empty database (BACKLOG E7).

The acceptance criterion is "imports into an empty database and passes a
consistency check; refuses a pack with no sources", and both halves need a real
database: the consistency being checked is between tables, and two of the
guards are CHECK constraints rather than Python.

Lives here rather than in pipeline/tests because this is where the scratch
database fixtures are. The code under test is the pipeline's.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pipeline.import_pack import ImportRefusedError, check, import_pack, run
from pipeline.pack import (
    MediaAsset,
    Pack,
    PackConcept,
    PackDialogue,
    PackSentence,
    PackSubstitution,
    PackUsage,
)
from pipeline.storage import LocalMediaStore
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.core.config import Settings

pytestmark = pytest.mark.integration

KM = "ខ្ញុំចង់បានទឹក"
CLIP = b"\xff\xfbT\xc4" + bytes(188)


def settings_for(url: URL, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "DATABASE_URL": url.render_as_string(hide_password=False),
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
        "JWT_SECRET": "x" * 32,
        **overrides,
    }
    return Settings(**values)


def a_pack(tmp_path: Path, *, concepts: int = 2, **overrides: Any) -> tuple[Pack, Path]:
    """A pack, with its clips on disk where the pack says they are."""
    media_dir = tmp_path / "media"
    assets: list[MediaAsset] = []
    built: list[PackConcept] = []

    for number in range(concepts):
        clips = {}
        for role, text, language in (
            ("explain", f"{KM}{number}", "km"),
            ("drill", f"我要水{number}", "zh"),
            ("vocab", f"水{number}", "zh"),
            ("qa", f"你要水{number}吗", "zh"),
        ):
            identity = f"{language}{role}{number}" * 4
            path = f"{language}/{identity}.mp3"
            (media_dir / path).parent.mkdir(parents=True, exist_ok=True)
            (media_dir / path).write_bytes(CLIP)
            assets.append(
                MediaAsset(
                    id=identity,
                    language=language,
                    voice="zh_model" if language == "zh" else "km_narrator",
                    resolved_voice="fake",
                    provider="fake",
                    speaking_rate=0.9,
                    text=text,
                    path=path,
                    duration_ms=240,
                    bytes=len(CLIP),
                )
            )
            clips[role] = identity

        built.append(
            PackConcept(
                slug=f"zh.hsk1.c{number}",
                standard_ref=f"一{number + 1:02d}",
                pattern=f"我要 + [名词]{number}",
                hskk_task_types=("listen_and_repeat",),
                km_explanation=f"{KM}{number}",
                explanation_audio=clips["explain"],
                target_sentences=(
                    PackSentence(
                        zh=f"我要水{number}",
                        pinyin="wǒ yào shuǐ",
                        km_gloss=KM,
                        audio=clips["drill"],
                    ),
                ),
                substitutions=(
                    PackSubstitution(
                        zh=f"水{number}", pinyin="shuǐ", km_gloss=KM, audio=clips["vocab"]
                    ),
                ),
                dialogues=(
                    PackDialogue(
                        hskk_task_type="listen_and_repeat",
                        prompt_zh=f"你要水{number}吗",
                        prompt_km=KM,
                        sample_answer_zh=f"我要水{number}",
                        prompt_audio_zh=clips["qa"],
                        prompt_audio_km="",
                    ),
                ),
            )
        )

    pack = Pack(
        version="v1",
        language="zh",
        level="HSK1",
        licence="public standards",
        sources=({"id": "zh.standard.2021"},),
        built_at=dt.datetime(2026, 9, 20, tzinfo=dt.UTC),
        provider="fake",
        validated=True,
        usage=PackUsage(tts_calls=8, tts_cost_usd_cents=0, llm_calls=2, llm_cost_usd_cents=3),
        concepts=tuple(built),
        media=tuple(assets),
        **overrides,
    ).sealed()
    return pack, media_dir


def do_import(db: sa.Engine, pack: Pack, media_dir: Path, tmp_path: Path) -> Any:
    """One import, in its own transaction, on the suite's pooled engine."""
    store = LocalMediaStore(tmp_path / "published", base_url="https://cdn.example/media")
    with Session(db) as session, session.begin():
        return import_pack(pack, session=session, store=store, media_dir=media_dir)


# ------------------------------------------------------------- the acceptance


def test_a_pack_imports_into_an_empty_database(db: sa.Engine, tmp_path: Path, rows: Any) -> None:
    pack, media_dir = a_pack(tmp_path)

    report = do_import(db, pack, media_dir, tmp_path)

    assert report.concepts == 2
    assert report.lessons == 2
    assert report.items == 8  # one explain, drill, vocab and qa per concept
    assert len(rows("SELECT id FROM content_packs")) == 1
    assert len(rows("SELECT id FROM courses")) == 1


def test_the_imported_rows_are_consistent_with_each_other(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """The consistency check E7 asks for, as queries rather than as a claim."""
    pack, media_dir = a_pack(tmp_path)

    do_import(db, pack, media_dir, tmp_path)

    # Every lesson belongs to the imported course and names concepts that exist.
    orphan_lessons = rows(
        "SELECT l.id FROM lessons l LEFT JOIN courses c ON c.id = l.course_id WHERE c.id IS NULL"
    )
    dangling_concepts = rows(
        "SELECT l.id FROM lessons l WHERE EXISTS ("
        "  SELECT 1 FROM unnest(l.concept_ids) cid"
        "  WHERE NOT EXISTS (SELECT 1 FROM concepts WHERE id = cid))"
    )
    orphan_items = rows(
        "SELECT i.id FROM lesson_items i LEFT JOIN lessons l ON l.id = i.lesson_id "
        "WHERE l.id IS NULL"
    )
    orphan_media = rows(
        "SELECT m.id FROM media_assets m LEFT JOIN lesson_items i ON i.id = m.lesson_item_id "
        "WHERE i.id IS NULL"
    )

    assert orphan_lessons == []
    assert dangling_concepts == []
    assert orphan_items == []
    assert orphan_media == []


def test_every_lesson_runs_explain_drill_vocab_qa_in_that_order(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """PRD 3.3 is the order a lesson plays in; sequence is what encodes it."""
    pack, media_dir = a_pack(tmp_path, concepts=1)

    do_import(db, pack, media_dir, tmp_path)
    order = [
        item_type
        for (item_type,) in rows("SELECT item_type FROM lesson_items ORDER BY lesson_id, sequence")
    ]

    assert order == ["explain", "drill", "vocab", "qa"]


def test_each_concept_becomes_one_lesson_in_teaching_order(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """D-099. The sequence is the seed's order, which is the syllabus's order."""
    pack, media_dir = a_pack(tmp_path, concepts=3)

    do_import(db, pack, media_dir, tmp_path)
    lessons = rows("SELECT sequence, title_km FROM lessons ORDER BY sequence")

    assert [sequence for sequence, _ in lessons] == [1, 2, 3]
    assert [title for _, title in lessons] == [concept.pattern for concept in pack.concepts]


def test_every_lesson_names_the_concept_it_teaches(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """D-099, and the half the orphan queries cannot see.

    "Every id in concept_ids exists" is vacuously true of an empty array, so a
    lesson that names no concept passed every consistency check while teaching
    nothing: mastery and the review queue both key off concept_ids. A mutation
    survived on exactly that.
    """
    pack, media_dir = a_pack(tmp_path, concepts=2)

    do_import(db, pack, media_dir, tmp_path)
    taught = rows(
        "SELECT l.sequence, c.slug FROM lessons l "
        "JOIN concepts c ON c.id = ANY(l.concept_ids) ORDER BY l.sequence"
    )

    assert [slug for _, slug in taught] == [concept.slug for concept in pack.concepts]
    assert [count for (count,) in rows("SELECT array_length(concept_ids, 1) FROM lessons")] == [
        1,
        1,
    ]


def test_audio_is_published_and_the_rows_point_at_it(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    pack, media_dir = a_pack(tmp_path, concepts=1)

    report = do_import(db, pack, media_dir, tmp_path)
    assets = rows("SELECT url, duration_ms, bytes, provider, source_text_hash FROM media_assets")

    assert report.media == 4
    assert len(assets) == 4  # the Khmer dialogue prompt has no clip in this pack
    for url, duration_ms, size, _provider, source_text_hash in assets:
        published = tmp_path / "published" / url.removeprefix("https://cdn.example/media/")
        assert published.exists()
        assert duration_ms == 240
        assert size == len(CLIP)
        assert source_text_hash


def test_the_content_production_cost_reaches_the_ledger(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """D-084's chain, closed: generation and synthesis both land here."""
    pack, media_dir = a_pack(tmp_path)

    do_import(db, pack, media_dir, tmp_path)
    ledger = rows(
        "SELECT provider, unit, quantity, cost_usd_cents_est, ref, user_id "
        "FROM cost_ledger ORDER BY provider"
    )

    assert [ref for *_, ref, _ in ledger] == ["content_production", "content_production"]
    assert all(user_id is None for *_, user_id in ledger)
    assert {provider for provider, *_ in ledger} == {"llm:fake", "tts:fake"}
    assert sum(cents for *_, cents, _, _ in ledger) == 3


# ------------------------------------------------------------------- refusals


def test_a_pack_with_no_sources_is_refused(tmp_path: Path) -> None:
    """E7's other acceptance criterion, and red line R7."""
    pack, _ = a_pack(tmp_path)
    stripped = pack.model_copy(update={"sources": ()}).sealed()

    assert any("sources" in problem for problem in check(stripped))


def test_a_pack_with_a_blank_licence_is_refused(tmp_path: Path) -> None:
    pack, _ = a_pack(tmp_path)

    assert any(
        "licence" in problem for problem in check(pack.model_copy(update={"licence": " "}).sealed())
    )


def test_an_edited_pack_is_refused(db: sa.Engine, tmp_path: Path) -> None:
    """The checksum is what notices a pack changed after it was built."""
    pack, media_dir = a_pack(tmp_path)
    tampered = pack.model_copy(update={"licence": "something else"})

    with pytest.raises(ImportRefusedError, match="checksum"):
        do_import(db, tampered, media_dir, tmp_path)


def test_a_pack_that_never_passed_validation_is_refused(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """D-094: --allow-invalid travels in the pack and stops here."""
    pack, media_dir = a_pack(tmp_path)
    forced = pack.model_copy(update={"validated": False}).sealed()

    with pytest.raises(ImportRefusedError, match="allow-invalid"):
        do_import(db, forced, media_dir, tmp_path)
    assert rows("SELECT id FROM content_packs") == []


def test_a_version_already_imported_is_not_overwritten(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """D-098: attempts point at lesson_items, so replacing them is a migration."""
    pack, media_dir = a_pack(tmp_path)
    do_import(db, pack, media_dir, tmp_path)

    with pytest.raises(ImportRefusedError, match="already imported"):
        do_import(db, pack, media_dir, tmp_path)

    assert len(rows("SELECT id FROM content_packs")) == 1
    assert len(rows("SELECT id FROM lessons")) == 2


def test_a_missing_clip_stops_the_import(db: sa.Engine, tmp_path: Path, rows: Any) -> None:
    """A row with a URL that answers 404 is found by a learner, not by a test."""
    pack, media_dir = a_pack(tmp_path)
    (media_dir / pack.media[0].path).unlink()

    with pytest.raises(ImportRefusedError, match="cannot be read"):
        do_import(db, pack, media_dir, tmp_path)
    assert rows("SELECT id FROM lessons") == []


def test_a_clip_that_changed_since_the_build_stops_the_import(
    db: sa.Engine, tmp_path: Path
) -> None:
    pack, media_dir = a_pack(tmp_path)
    (media_dir / pack.media[0].path).write_bytes(CLIP + b"\x00")

    with pytest.raises(ImportRefusedError, match="the file changed"):
        do_import(db, pack, media_dir, tmp_path)


def test_nothing_is_written_when_the_import_fails_partway(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """One transaction: a pack lands whole or not at all."""
    pack, media_dir = a_pack(tmp_path, concepts=3)
    (media_dir / pack.media[-1].path).unlink()

    with pytest.raises(ImportRefusedError):
        do_import(db, pack, media_dir, tmp_path)

    for table in ("content_packs", "courses", "concepts", "lessons", "lesson_items"):
        assert rows(f"SELECT id FROM {table}") == [], table


# ----------------------------------------------------------------- re-import


def test_a_second_level_reuses_the_course_and_keeps_its_lessons(
    db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """A later version adds to the same course rather than starting another."""
    first, media_dir = a_pack(tmp_path, concepts=1)
    do_import(db, first, media_dir, tmp_path)

    second, second_dir = a_pack(tmp_path / "again", concepts=1)
    second = second.model_copy(update={"version": "v2"}).sealed()
    do_import(db, second, second_dir, tmp_path)

    assert len(rows("SELECT id FROM courses")) == 1
    assert [sequence for (sequence,) in rows("SELECT sequence FROM lessons ORDER BY sequence")] == [
        1,
        2,
    ]
    # The concept was already there and was updated, not duplicated.
    assert len(rows("SELECT id FROM concepts")) == 1


def test_storage_refuses_a_configured_bucket_it_cannot_write_to(tmp_path: Path) -> None:
    """D-097: a laptop-local write dressed up as an upload is the worst option."""
    from pipeline.storage import StorageNotImplementedError, get_media_store

    configured = settings_for(
        URL.create("postgresql", host="unused", database="unused"),
        OBJECT_STORAGE_ENDPOINT="https://s3.example",
    )

    with pytest.raises(StorageNotImplementedError, match="D-097"):
        get_media_store(configured, root=tmp_path)


def test_the_runner_wraps_one_transaction(
    settings: Settings, db: sa.Engine, tmp_path: Path, rows: Any
) -> None:
    """`run` is what the CLI calls; the fixtures above drive import_pack directly."""
    pack, media_dir = a_pack(tmp_path, concepts=1)

    report = run(pack, settings=settings, media_dir=media_dir, engine=db)

    assert report.lessons == 1
    assert len(rows("SELECT id FROM content_packs")) == 1
