"""Stage [6]: put a pack into the database (BACKLOG E7, PRD 6.1).

The last stage, and the only one that touches infrastructure: it publishes the
audio, writes the rows a learner's session reads, and records what the content
cost to make.

Four refusals stand between a pack and the database, and each exists because
the alternative is discovered by a learner rather than by a command:

  · **provenance** — empty ``sources`` or a blank ``licence`` is refused here
    and again by a CHECK constraint (PRD 5.5, red line R7);
  · **integrity** — the checksum has to match what the pack says it is;
  · **validation** — a pack built with ``--allow-invalid`` carries
    ``validated: false`` and is refused (D-094);
  · **immutability** — a version already in ``content_packs`` is not
    overwritten. Learners' attempts reference lesson_items by id; replacing
    them under a live database is a migration, not an import (D-098).

Lessons are one per concept, in the seed's teaching order (D-099).

    make import PACK=pipeline/packs/zh-HSK1.pack.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.commerce import CostLedger
from app.models.content import Concept as ConceptRow
from app.models.content import ContentPack, Course, Lesson, LessonItem, MediaAsset
from pipeline.pack import MediaAsset as PackMediaAsset
from pipeline.pack import Pack, PackConcept
from pipeline.storage import MediaStore, StorageNotImplementedError, get_media_store

#: V1 teaches against an examination syllabus, so every pack imported here is
#: an exam course. Job packs (PRD 12) carry an org and arrive with M4.
COURSE_TYPE: Final = "exam"

#: ref for the content-production rows in cost_ledger (PRD 11.3), which is what
#: keeps one-off content cost out of the per-user operating figures.
CONTENT_PRODUCTION: Final = "content_production"


class ImportRefusedError(Exception):
    """The pack was not imported, and why. Nothing has been written."""


@dataclass
class ImportReport:
    """What one import did."""

    pack_id: uuid.UUID | None = None
    course_id: uuid.UUID | None = None
    concepts: int = 0
    concepts_updated: int = 0
    lessons: int = 0
    items: int = 0
    media: int = 0
    uploaded_bytes: int = 0
    ledger_rows: int = 0
    notes: list[str] = field(default_factory=list)


def check(pack: Pack) -> list[str]:
    """Everything wrong with a pack, before anything is written."""
    problems: list[str] = []
    if not pack.sources:
        problems.append("no sources; PRD 5.5 requires provenance on every pack")
    if not pack.licence.strip():
        problems.append("licence is blank")
    if not pack.checksum_matches:
        problems.append(
            "checksum does not match the pack's contents: it was edited after it was built"
        )
    if not pack.validated:
        problems.append(
            "built with --allow-invalid, so validate.py never passed it (D-094); "
            "rebuild from a draft that validates"
        )
    if pack.with_video:
        problems.append("carries video, which nothing imports yet (PRD 7.2, S2)")
    return problems


def import_pack(
    pack: Pack,
    *,
    session: Session,
    store: MediaStore,
    media_dir: Path,
    now: dt.datetime | None = None,
) -> ImportReport:
    """Write one pack. Caller owns the transaction, so a failure rolls it back.

    Raises:
        ImportRefusedError: a guard failed, or this version is already imported.
    """
    problems = check(pack)
    if problems:
        raise ImportRefusedError("; ".join(problems))

    existing = session.scalar(
        sa.select(ContentPack).where(
            ContentPack.language == pack.language,
            ContentPack.level == pack.level,
            ContentPack.version == pack.version,
        )
    )
    if existing is not None:
        same = "identical" if existing.checksum == pack.checksum else "different"
        raise ImportRefusedError(
            f"{pack.language} {pack.level} {pack.version} is already imported with "
            f"{same} content. Packs are immutable: publish a new version rather than "
            f"replacing one that learners' attempts already point at (D-098)."
        )

    report = ImportReport()
    urls = _publish(pack, store=store, media_dir=media_dir, report=report)
    assets = {asset.id: asset for asset in pack.media}
    course = _upsert_course(pack, session=session)
    report.course_id = course.id
    concepts = _upsert_concepts(pack, session=session, report=report)
    _write_lessons(
        pack,
        session=session,
        course=course,
        concepts=concepts,
        urls=urls,
        assets=assets,
        report=report,
    )

    content_pack = ContentPack(
        version=pack.version,
        language=pack.language,
        level=pack.level,
        checksum=pack.checksum,
        sources=list(pack.sources),
        licence=pack.licence,
    )
    session.add(content_pack)
    session.flush()
    report.pack_id = content_pack.id

    _record_cost(pack, session=session, now=now, report=report)
    return report


# ------------------------------------------------------------------ the parts


def _publish(
    pack: Pack, *, store: MediaStore, media_dir: Path, report: ImportReport
) -> dict[str, str]:
    """Upload every clip and return asset id -> URL.

    A clip the pack names but whose file is missing stops the import: the row
    would otherwise be written with a URL that answers 404, and nothing after
    this point would notice.
    """
    urls: dict[str, str] = {}
    for asset in pack.media:
        source = media_dir / asset.path
        try:
            data = source.read_bytes()
        except OSError as error:
            raise ImportRefusedError(
                f"{asset.path} is named in the pack but cannot be read from {media_dir}: "
                f"{error.strerror or error}"
            ) from None
        if len(data) != asset.bytes:
            raise ImportRefusedError(
                f"{asset.path} is {len(data)} bytes, the pack says {asset.bytes}: "
                "the file changed after the pack was built"
            )
        urls[asset.id] = store.put(asset.path, data)
        report.media += 1
        report.uploaded_bytes += len(data)
    return urls


def _upsert_course(pack: Pack, *, session: Session) -> Course:
    course = session.scalar(
        sa.select(Course).where(
            Course.language == pack.language,
            Course.level == pack.level,
            Course.course_type == COURSE_TYPE,
            Course.org_id.is_(None),
        )
    )
    if course is None:
        course = Course(
            course_type=COURSE_TYPE,
            language=pack.language,
            level=pack.level,
            # The level itself, until a native speaker writes the Khmer. Neutral
            # rather than invented: "HSK1" is what the course is called on the
            # certificate too (D-099).
            title_km=pack.level,
            title_zh=pack.level,
        )
        session.add(course)
        session.flush()
    return course


def _upsert_concepts(
    pack: Pack, *, session: Session, report: ImportReport
) -> dict[str, ConceptRow]:
    """Insert or update by slug. Never deletes: concept_mastery points here."""
    existing = {
        row.slug: row
        for row in session.scalars(
            sa.select(ConceptRow).where(
                ConceptRow.slug.in_([concept.slug for concept in pack.concepts])
            )
        )
    }
    rows: dict[str, ConceptRow] = {}
    for order, concept in enumerate(pack.concepts, start=1):
        row = existing.get(concept.slug)
        if row is None:
            row = ConceptRow(
                slug=concept.slug,
                language=pack.language,
                level=pack.level,
                pattern=concept.pattern,
                km_explanation=concept.km_explanation,
                common_l1_errors=[],
                hskk_task_types=list(concept.hskk_task_types),
                sort_order=order * 10,
            )
            session.add(row)
            report.concepts += 1
        else:
            row.pattern = concept.pattern
            row.km_explanation = concept.km_explanation
            row.hskk_task_types = list(concept.hskk_task_types)
            row.sort_order = order * 10
            report.concepts_updated += 1
        rows[concept.slug] = row
    session.flush()
    return rows


def _write_lessons(
    pack: Pack,
    *,
    session: Session,
    course: Course,
    concepts: dict[str, ConceptRow],
    urls: dict[str, str],
    assets: dict[str, PackMediaAsset],
    report: ImportReport,
) -> None:
    """One lesson per concept, its items in the order a lesson runs (PRD 3.3)."""
    taken = set(session.scalars(sa.select(Lesson.sequence).where(Lesson.course_id == course.id)))
    sequence = 0
    for concept in pack.concepts:
        sequence += 1
        while sequence in taken:
            sequence += 1
        row = concepts[concept.slug]
        lesson = Lesson(
            course_id=course.id,
            concept_ids=[row.id],
            sequence=sequence,
            # The pattern itself: a learner of Chinese reads "我要 + [名词]" and
            # knows what the lesson is, which a transliterated slug would not
            # give them. Khmer titles are part of the copy pass (D-099).
            title_km=concept.pattern,
        )
        session.add(lesson)
        session.flush()
        report.lessons += 1
        _write_items(
            concept,
            session=session,
            lesson=lesson,
            concept_row=row,
            urls=urls,
            assets=assets,
            report=report,
        )


def _write_items(
    concept: PackConcept,
    *,
    session: Session,
    lesson: Lesson,
    concept_row: ConceptRow,
    urls: dict[str, str],
    assets: dict[str, PackMediaAsset],
    report: ImportReport,
) -> None:
    position = 0

    def add(item_type: str, payload: dict[str, Any], audio: str) -> None:
        nonlocal position
        position += 1
        item = LessonItem(
            lesson_id=lesson.id,
            concept_id=concept_row.id,
            item_type=item_type,
            payload=payload,
            media_type="audio",
            anchor=item_type == "explain",
            sequence=position,
        )
        session.add(item)
        session.flush()
        if audio and audio in urls:
            asset = assets[audio]
            session.add(
                MediaAsset(
                    lesson_item_id=item.id,
                    kind="audio",
                    url=urls[audio],
                    duration_ms=asset.duration_ms,
                    bytes=asset.bytes,
                    provider=asset.provider,
                    # The clip's identity is the hash of the text that made it,
                    # which is exactly what this column is for.
                    source_text_hash=audio,
                )
            )
        report.items += 1

    add(
        "explain",
        {"pattern": concept.pattern, "km_explanation": concept.km_explanation},
        concept.explanation_audio,
    )
    for sentence in concept.target_sentences:
        add(
            "drill",
            {"zh": sentence.zh, "pinyin": sentence.pinyin, "km_gloss": sentence.km_gloss},
            sentence.audio,
        )
    for substitution in concept.substitutions:
        add(
            "vocab",
            {
                "zh": substitution.zh,
                "pinyin": substitution.pinyin,
                "km_gloss": substitution.km_gloss,
            },
            substitution.audio,
        )
    for dialogue in concept.dialogues:
        add(
            "qa",
            {
                "hskk_task_type": dialogue.hskk_task_type,
                "prompt_zh": dialogue.prompt_zh,
                "prompt_km": dialogue.prompt_km,
                "sample_answer_zh": dialogue.sample_answer_zh,
            },
            dialogue.prompt_audio_zh,
        )


def _record_cost(
    pack: Pack, *, session: Session, now: dt.datetime | None, report: ImportReport
) -> None:
    """The content-production rows CLAUDE.md section 8 requires (D-084).

    Two rows, one per stage, both with no user: this cost is one-off and not
    attributable to anybody's account (PRD 11.3). Written even when the cost
    rounds to zero, because "the call happened and cost nothing measurable" and
    "no call happened" are different facts.
    """
    occurred = now or dt.datetime.now(dt.UTC)
    for provider, unit, quantity, cents in (
        ("llm", "calls", pack.usage.llm_calls, pack.usage.llm_cost_usd_cents),
        ("tts", "calls", pack.usage.tts_calls, pack.usage.tts_cost_usd_cents),
    ):
        session.add(
            CostLedger(
                occurred_at=occurred,
                user_id=None,
                provider=f"{provider}:{pack.provider}",
                unit=unit,
                quantity=float(quantity),
                cost_usd_cents_est=cents,
                ref=CONTENT_PRODUCTION,
            )
        )
        report.ledger_rows += 1


# ------------------------------------------------------------------------ CLI


def run(pack: Pack, *, settings: Settings, media_dir: Path, engine: sa.Engine) -> ImportReport:
    """Import in one transaction: either the whole pack lands or none of it."""
    store = get_media_store(settings, root=media_dir.parent / "published")
    with Session(engine) as session, session.begin():
        return import_pack(pack, session=session, store=store, media_dir=media_dir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pack", type=Path, help="the pack JSON written by build_pack.py")
    parser.add_argument(
        "--media-dir",
        type=Path,
        default=None,
        help="where the audio files are (default: <pack>-media beside the pack)",
    )
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    settings = get_settings()
    try:
        pack = Pack.model_validate_json(arguments.pack.read_text(encoding="utf-8"))
    except OSError as error:
        print(f"error: cannot read {arguments.pack}: {error.strerror or error}", file=sys.stderr)
        return 1

    media_dir = arguments.media_dir or arguments.pack.parent / (
        f"{pack.language}-{pack.level}-media"
    )
    engine = sa.create_engine(_sync_url(settings.database_url))

    try:
        report = run(pack, settings=settings, media_dir=media_dir, engine=engine)
    except (ImportRefusedError, StorageNotImplementedError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(
        f"imported {pack.language} {pack.level} {pack.version}: "
        f"{report.concepts} new concept(s), {report.concepts_updated} updated, "
        f"{report.lessons} lesson(s), {report.items} item(s), "
        f"{report.media} clip(s) ({report.uploaded_bytes} bytes), "
        f"{report.ledger_rows} cost row(s)"
    )
    return 0


def _sync_url(url: str) -> str:
    """The application's URL, without its async driver.

    The pipeline is a script: it wants a plain connection, not an event loop
    (ARCHITECTURE 2.3). The same database, reached the ordinary way.
    """
    return url.replace("+asyncpg", "").replace("+psycopg_async", "")


if __name__ == "__main__":
    raise SystemExit(main())
