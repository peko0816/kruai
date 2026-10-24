"""How the M0 scripts reach a provider, and how their spending is recorded.

Two promises to hold: ``azure`` reaches the verification adapter while every
other name still goes through the application registry (D-126), and a billed
run cannot make a request that ``cost_ledger`` will not see (CLAUDE.md
section 8).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app.services.cost_ledger import LedgerRef, PendingEntry
from app.services.scoring.base import Language
from app.services.scoring.fake import FakeScorer
from app.services.tts.base import Voice
from app.services.tts.fake import FakeTTS
from m0.azure import AzureScorer, AzureTTS
from m0.providers import (
    LEDGER_REF,
    LedgerUnavailableError,
    build_tts,
    get_scorer,
    open_ledger,
)
from m0.scoring_check import Sample, score_all
from m0.tests.test_scoring_check import settings
from m0.tts_blind import Candidate, Passage, synthesise

KM = "សួស្តី"
#: Nothing listens here, so a connection is refused at once rather than
#: waiting out a timeout.
UNREACHABLE_DB = "postgresql+psycopg://kruai:kruai@127.0.0.1:1/kruai"


class Captured:
    """A ledger that keeps what it is told, in place of Postgres."""

    def __init__(self) -> None:
        self.entries: list[PendingEntry] = []

    @asynccontextmanager
    async def external_call(
        self, *, provider: str, ref: LedgerRef, user_id: uuid.UUID | None = None
    ) -> AsyncIterator[PendingEntry]:
        entry = PendingEntry(provider=provider, ref=ref, user_id=user_id)
        yield entry
        self.entries.append(entry)


# ------------------------------------------------------------------ routing


def test_azure_reaches_the_verification_adapters() -> None:
    configured = settings(azure_speech_key="k", azure_speech_region="eastasia")

    assert isinstance(build_tts("azure", settings=configured), AzureTTS)
    assert isinstance(
        get_scorer(
            Language.ZH_CN,
            settings=settings(
                scoring_provider="azure", azure_speech_key="k", azure_speech_region="eastasia"
            ),
        ),
        AzureScorer,
    )


def test_fake_still_goes_through_the_application_registry() -> None:
    assert isinstance(build_tts("fake", settings=settings()), FakeTTS)
    assert isinstance(get_scorer(Language.ZH_CN, settings=settings()), FakeScorer)


def test_a_pinned_voice_reaches_the_azure_adapter() -> None:
    provider = build_tts(
        "azure",
        settings=settings(azure_speech_key="k", azure_speech_region="eastasia"),
        voices={Voice.KM_NARRATOR: "km-KH-PisethNeural"},
    )

    assert provider.available_voices()[Voice.KM_NARRATOR] == "km-KH-PisethNeural"


# ------------------------------------------------------------------- ledger


async def test_an_unbilled_run_needs_no_database() -> None:
    """Fake calls nothing external, and the scripts' own tests run without
    Postgres — which is how they were written before anybody had a key."""
    async with (
        open_ledger(settings(database_url=UNREACHABLE_DB), billed=False) as ledger,
        ledger.external_call(provider="fake", ref=LEDGER_REF) as entry,
    ):
        entry.record(unit="calls", quantity=1, cost_usd_cents=0)


async def test_a_billed_run_will_not_open_without_the_ledger() -> None:
    with pytest.raises(LedgerUnavailableError, match="No request was made"):
        async with open_ledger(settings(database_url=UNREACHABLE_DB), billed=True):
            pytest.fail("the body must not run")


async def test_every_assessment_is_recorded(tmp_path: Path) -> None:
    (tmp_path / "a.wav").write_bytes(b"RIFF")
    samples = [
        Sample(path=tmp_path / "a.wav", reference_text="我要水。", group="good"),
        Sample(path=tmp_path / "a.wav", reference_text="我要水。__BAD__", group="bad"),
    ]
    ledger = Captured()

    await score_all(samples, settings=settings(), ledger=ledger)

    assert len(ledger.entries) == 2
    assert {e.ref for e in ledger.entries} == {"content_production"}
    assert all(e.total_usd_cents == 1 for e in ledger.entries)  # FakeScorer's synthetic cent


async def test_every_synthesis_is_recorded_per_candidate() -> None:
    """Two voices re-synthesise the whole corpus: two rows per passage."""
    passages = [Passage(id=p, text=KM, source="corpus.tatoeba") for p in ("p01", "p02")]
    ledger = Captured()

    await synthesise(
        passages,
        candidates=[Candidate.parse("fake:a"), Candidate.parse("fake:b")],
        settings=settings(),
        ledger=ledger,
    )

    assert len(ledger.entries) == 4
    assert all(e.costs for e in ledger.entries)


# --------------------------------------------------------------------- CLI


def _manifest(tmp_path: Path) -> Path:
    for name in ("a.wav", "b.wav"):
        (tmp_path / name).write_bytes(b"RIFF")
    path = tmp_path / "manifest.csv"
    path.write_text(
        "file,reference_text,group\na.wav,我要水。,good\nb.wav,我要水。,bad\n", encoding="utf-8"
    )
    return path


def _refuse_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a request was made")

    monkeypatch.setattr("m0.azure.scoring.post", refuse)
    monkeypatch.setattr("m0.azure.tts.post", refuse)


def test_scoring_stops_before_any_request_when_the_ledger_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import m0.scoring_check as module

    _refuse_requests(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: settings(
            scoring_provider="azure",
            azure_speech_key="k",
            azure_speech_region="eastasia",
            database_url=UNREACHABLE_DB,
        ),
    )

    code = module.main([str(_manifest(tmp_path)), "--confirm-spend", "--out", str(tmp_path / "r")])

    assert code == 2
    assert "cost_ledger" in capsys.readouterr().err


def test_scoring_without_a_key_stops_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a traceback: the person running this is reading for what to fix."""
    import m0.scoring_check as module

    _refuse_requests(monkeypatch)
    monkeypatch.setattr(module, "get_settings", lambda: settings(scoring_provider="azure"))
    # The key check comes before the ledger, which must never be reached.
    monkeypatch.setattr(
        "m0.scoring_check.open_ledger", lambda *_, **__: pytest.fail("ledger opened")
    )

    code = module.main([str(_manifest(tmp_path)), "--confirm-spend", "--out", str(tmp_path / "r")])

    assert code == 2
    assert "AZURE_SPEECH_KEY" in capsys.readouterr().err


def test_the_blind_export_stops_before_any_request_when_the_ledger_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import m0.tts_blind as module

    _refuse_requests(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: settings(
            azure_speech_key="k", azure_speech_region="eastasia", database_url=UNREACHABLE_DB
        ),
    )
    corpus = tmp_path / "corpus.csv"
    corpus.write_text(f"id,khmer,source\np01,{KM},corpus.tatoeba\n", encoding="utf-8")

    code = module.main(
        [
            "export",
            str(corpus),
            "--candidates",
            "azure:km-KH-SreymomNeural,azure:km-KH-PisethNeural",
            "--confirm-spend",
            "--out",
            str(tmp_path / "clips"),
            "--key",
            str(tmp_path / "key.csv"),
        ]
    )

    assert code == 2
    assert "cost_ledger" in capsys.readouterr().err
    assert not (tmp_path / "clips").exists()
