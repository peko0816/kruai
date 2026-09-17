"""Media selection, including every reason video does not get served.

BACKLOG C6 names two cases — a data-saver learner on a video item, and a video
item with no video asset. Both are here, along with the other four gates,
because the reason matters as much as the outcome: PRD 7.2 decides whether S3
happens by comparing completion rates between the arms, and a learner who got
audio through data saver is not in the same population as one the experiment
assigned there.

The fallback is not a nicety. PRD 7.3 calls it the availability floor on
Cambodian mobile networks, which is why several tests assert that audio is still
reachable even when video is what got chosen.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Plan, Settings
from app.services.media import (
    AVATAR_DISCLAIMER_KEY,
    VARIANT_AUDIO,
    VARIANT_VIDEO,
    MediaCandidate,
    ResolvedMedia,
    ViewerContext,
    resolve,
)

AUDIO = "https://cdn.example/explain-001.mp3"
VIDEO = "https://cdn.example/explain-001.mp4"

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


def settings(**overrides: str) -> Settings:
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


def s2_enabled(**overrides: str) -> Settings:
    """Configuration as it will be once PRD 7.2's S2 conditions are met."""
    return settings(MEDIA_VIDEO_ENABLED="true", **overrides)


def item(
    *, media_type: str = "video", audio: str | None = AUDIO, video: str | None = VIDEO
) -> MediaCandidate:
    return MediaCandidate(item_media_type=media_type, audio_url=audio, video_url=video)


def viewer(
    *, plan: Plan = "pro", data_saver: bool = False, variant: str | None = None
) -> ViewerContext:
    return ViewerContext(plan=plan, data_saver=data_saver, experiment_variant=variant)


def resolved(
    candidate: MediaCandidate | None = None,
    who: ViewerContext | None = None,
    config: Settings | None = None,
) -> ResolvedMedia:
    return resolve(candidate or item(), who or viewer(), settings=config or s2_enabled())


# ---------------------------------------------------- the two named acceptances


def test_a_data_saver_learner_gets_audio_from_a_video_item() -> None:
    """BACKLOG C6 acceptance. A deliberate choice about someone's data bill
    outranks their entitlement — a Pro learner who turned it on still gets audio."""
    result = resolved(who=viewer(plan="pro", data_saver=True))

    assert result.primary_kind == "audio"
    assert result.primary_url == AUDIO
    assert result.decision == "data_saver"


def test_a_video_item_with_no_video_asset_gets_audio() -> None:
    """BACKLOG C6 acceptance. Marked as video but never rendered, which is every
    item until a build runs with --with-video."""
    result = resolved(item(media_type="video", video=None))

    assert result.primary_kind == "audio"
    assert result.decision == "no_video_asset"
    assert result.video_url is None


# ------------------------------------------------------------- the other gates


def test_video_is_off_until_s2() -> None:
    """MEDIA_VIDEO_ENABLED defaults false and outranks everything, so switching
    it off is genuinely total."""
    result = resolve(item(), viewer(), settings=settings())

    assert result.primary_kind == "audio"
    assert result.decision == "video_globally_disabled"


def test_the_global_switch_outranks_a_fully_eligible_learner() -> None:
    result = resolve(item(), viewer(plan="pro", variant=VARIANT_VIDEO), settings=settings())
    assert result.decision == "video_globally_disabled"


def test_an_audio_item_stays_audio() -> None:
    """The content pack decided this one is a recording, not a lecture video."""
    result = resolved(item(media_type="audio"))

    assert result.primary_kind == "audio"
    assert result.decision == "item_is_audio_only"


@pytest.mark.parametrize("plan", ["free", "basic"])
def test_a_plan_below_the_minimum_gets_audio(plan: Plan) -> None:
    """MEDIA_VIDEO_MIN_PLAN defaults to pro (PRD 4.3: video opens at S2 for Pro,
    S3 for Basic)."""
    result = resolved(who=viewer(plan=plan))

    assert result.primary_kind == "audio"
    assert result.decision == "plan_below_minimum"


def test_the_minimum_plan_comes_from_configuration() -> None:
    """S3 opens video to Basic by moving this setting, not by editing code."""
    result = resolved(who=viewer(plan="basic"), config=s2_enabled(MEDIA_VIDEO_MIN_PLAN="basic"))
    assert result.primary_kind == "video"


def test_the_experiment_control_arm_gets_audio() -> None:
    """PRD 7.2's A/B: the same eligible learner, assigned to audio."""
    result = resolved(who=viewer(variant=VARIANT_AUDIO))

    assert result.primary_kind == "audio"
    assert result.decision == "experiment_control"


def test_the_experiment_video_arm_gets_video() -> None:
    result = resolved(who=viewer(variant=VARIANT_VIDEO))
    assert result.primary_kind == "video"


def test_no_variant_does_not_block_video() -> None:
    """With the experiment off, eligibility alone decides."""
    assert resolved(who=viewer(variant=None)).primary_kind == "video"


# ------------------------------------------------------------ video, when it is


def test_an_eligible_learner_gets_video() -> None:
    result = resolved()

    assert result.primary_kind == "video"
    assert result.primary_url == VIDEO
    assert result.decision == "video"


def test_video_always_carries_an_audio_fallback() -> None:
    """PRD 7.3 calls this the availability floor on Cambodian mobile networks."""
    result = resolved()

    assert result.fallback_url == AUDIO
    assert result.fallback_url != result.primary_url


def test_audio_has_nothing_to_fall_back_to() -> None:
    """It is the fallback; reporting itself again would invite a retry loop."""
    assert resolved(item(media_type="audio")).fallback_url is None


def test_both_urls_are_returned_whatever_was_chosen() -> None:
    """D2's response carries both — they are facts about the content, while
    primary_kind is the instruction."""
    result = resolved(who=viewer(data_saver=True))

    assert result.audio_url == AUDIO
    assert result.video_url == VIDEO
    assert result.primary_kind == "audio"


def test_the_client_timeout_comes_from_the_server() -> None:
    """The client never holds this number, so the threshold can move without
    shipping a new client."""
    assert resolved().client_timeout_ms == 3000
    assert (
        resolved(config=s2_enabled(MEDIA_CLIENT_VIDEO_TIMEOUT_MS="1500")).client_timeout_ms == 1500
    )


# ------------------------------------------------------- the avatar disclaimer


def test_serving_video_carries_the_ai_disclaimer_key() -> None:
    """PRD 7.4 is a hard constraint: an avatar is labelled as an AI assistant
    wherever it first appears. This is the only place that knows video was
    chosen, so the key travels with the decision rather than relying on the
    client to remember."""
    assert resolved().disclaimer_key == AVATAR_DISCLAIMER_KEY


@pytest.mark.parametrize(
    "case",
    [
        {"who": {"data_saver": True}},
        {"who": {"plan": "free"}},
        {"who": {"variant": VARIANT_AUDIO}},
        {"candidate": {"media_type": "audio"}},
    ],
)
def test_audio_carries_no_disclaimer(case: dict[str, Any]) -> None:
    """There is no avatar on screen, so the notice would be noise."""
    result = resolved(
        item(**case.get("candidate", {})),
        viewer(**case.get("who", {})),
    )
    assert result.primary_kind == "audio"
    assert result.disclaimer_key is None


# ------------------------------------------------------------ nothing to play


def test_an_item_with_no_assets_degrades_to_text() -> None:
    """build_pack.py marks an item media_pending when TTS fails and ships the
    pack anyway (ARCHITECTURE section 5); the lesson shows the written sentence
    rather than blocking."""
    result = resolved(item(audio=None, video=None))

    assert result.is_text_only is True
    assert result.primary_kind is None
    assert result.primary_url is None
    assert result.decision == "text_only"


def test_a_video_only_item_with_video_disabled_degrades_to_text() -> None:
    """No audio to fall back to. Better an honest text card than a URL the
    client cannot play."""
    result = resolve(item(audio=None), viewer(), settings=settings())

    assert result.is_text_only is True
    assert result.decision == "video_globally_disabled"


def test_a_video_only_item_still_plays_when_video_is_open() -> None:
    result = resolved(item(audio=None))

    assert result.primary_kind == "video"
    assert result.fallback_url is None


# ------------------------------------------------- the reason, for the A/B


def test_two_learners_who_both_got_audio_are_distinguishable() -> None:
    """The S2-to-S3 evaluation compares completion rates between arms. A learner
    who chose data saver is not in the same population as one the experiment
    assigned to audio, and lumping them together would decide a five-figure
    question on a mixed sample."""
    by_choice = resolved(who=viewer(data_saver=True))
    by_assignment = resolved(who=viewer(variant=VARIANT_AUDIO))

    assert by_choice.primary_kind == by_assignment.primary_kind == "audio"
    assert by_choice.decision != by_assignment.decision


def test_the_reason_names_the_most_general_cause() -> None:
    """A learner who is ineligible on several counts at once reports the widest
    one, so a global switch is never filed as a personal preference."""
    everything_wrong = resolve(
        item(media_type="audio", video=None),
        viewer(plan="free", data_saver=True, variant=VARIANT_AUDIO),
        settings=settings(),
    )
    assert everything_wrong.decision == "video_globally_disabled"


def test_item_type_outranks_a_missing_asset() -> None:
    result = resolved(item(media_type="audio", video=None))
    assert result.decision == "item_is_audio_only"


def test_plan_outranks_data_saver_in_the_reason() -> None:
    """Both would produce audio; the entitlement is the more durable fact."""
    result = resolved(who=viewer(plan="free", data_saver=True))
    assert result.decision == "plan_below_minimum"


# ------------------------------------------------------------------ bad input


def test_an_unknown_plan_is_refused() -> None:
    """Ranking it lowest would quietly deny video to everyone on a plan added
    later, and the symptom would be a support ticket rather than an error."""
    unknown = ViewerContext(plan="platinum", data_saver=False)  # type: ignore[arg-type]  # the bug under test

    with pytest.raises(ValueError, match="unknown plan 'platinum'"):
        resolve(item(), unknown, settings=s2_enabled())


def test_the_error_lists_the_known_plans_cheapest_first() -> None:
    unknown = ViewerContext(plan="platinum", data_saver=False)  # type: ignore[arg-type]  # the bug under test

    with pytest.raises(ValueError, match=r"\['free', 'basic', 'pro'\]"):
        resolve(item(), unknown, settings=s2_enabled())
