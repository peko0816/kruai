"""Which recording a learner gets, and what to fall back to.

ARCHITECTURE 2.2 puts this decision entirely on the server. The client has one
job — play the primary URL, and switch to the fallback if it stalls — because a
client that decides for itself will decide differently on each platform, and the
A/B comparison in PRD 7.2 needs every learner's arm to be knowable from the
server alone.

Video is the exception, not the default. It has to clear six separate gates, and
audio is what remains when any of them closes. PRD 7.3 is blunt about why the
fallback is mandatory rather than nice to have: on Cambodian mobile networks it
is the difference between a lesson and a spinner.

The decision is reported alongside the URLs. Knowing a learner got audio is not
enough for the S2-to-S3 evaluation — the question is whether they got audio
because the experiment assigned it or because they had switched on data saver,
and those two populations cannot be compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from app.core.config import Plan, Settings

MediaKind = Literal["audio", "video"]

#: Why the resolution came out the way it did. Ordered from "nobody could have
#: had video here" to "this particular learner did not".
MediaDecision = Literal[
    "video",
    "text_only",
    "video_globally_disabled",
    "item_is_audio_only",
    "no_video_asset",
    "plan_below_minimum",
    "data_saver",
    "experiment_control",
]

#: PRD 7.4, a hard constraint: an avatar must be labelled as an AI assistant
#: wherever it first appears. Carried in the result rather than left to the
#: client to remember, because this is the only place that knows video was
#: chosen, and "we forgot the disclaimer" is not a recoverable mistake.
AVATAR_DISCLAIMER_KEY: Final = "avatar.ai_disclaimer"

#: The experiment arms for "explain_media" (PRD 7.2). Named for what they serve
#: rather than control/treatment, so a stored variant reads as itself.
VARIANT_AUDIO: Final = "audio"
VARIANT_VIDEO: Final = "video"

#: Plan ordering, for comparing against MEDIA_VIDEO_MIN_PLAN. Structural rather
#: than configurable: the ladder is what the plans are.
_PLAN_RANK: Final[dict[str, int]] = {"free": 0, "basic": 1, "pro": 2}


@dataclass(frozen=True)
class MediaCandidate:
    """A lesson item and whatever assets exist for it."""

    #: lesson_items.media_type — what the content pack intended.
    item_media_type: str
    audio_url: str | None
    video_url: str | None


@dataclass(frozen=True)
class ViewerContext:
    """The learner, as far as media selection cares."""

    plan: Plan
    data_saver: bool
    #: experiments.variant for "explain_media"; None when the A/B is off.
    experiment_variant: str | None = None


@dataclass(frozen=True)
class ResolvedMedia:
    """What to play, what to fall back to, and why."""

    primary_kind: MediaKind | None
    audio_url: str | None
    video_url: str | None
    decision: MediaDecision
    #: How long the client waits before switching. Server-supplied so the
    #: threshold can move without shipping a client (PRD 7.3).
    client_timeout_ms: int
    #: Set only when video is being served (PRD 7.4).
    disclaimer_key: str | None

    @property
    def primary_url(self) -> str | None:
        if self.primary_kind == "video":
            return self.video_url
        if self.primary_kind == "audio":
            return self.audio_url
        return None

    @property
    def fallback_url(self) -> str | None:
        """Audio, when video is primary. Audio has nothing to fall back to."""
        return self.audio_url if self.primary_kind == "video" else None

    @property
    def is_text_only(self) -> bool:
        """No playable asset at all.

        build_pack.py marks an item media_pending when TTS fails, and the pack
        still ships (ARCHITECTURE section 5). The lesson degrades to the written
        sentence rather than blocking.
        """
        return self.primary_kind is None


def resolve(
    candidate: MediaCandidate, viewer: ViewerContext, *, settings: Settings
) -> ResolvedMedia:
    """Choose between the recordings that exist for one lesson item.

    Both URLs are returned whatever the outcome — they are facts about the
    content, and D2's response carries both — while ``primary_kind`` is the
    instruction. A client never inspects plan, data saver or item type.
    """
    decision = _decide(candidate, viewer, settings=settings)
    primary = _primary_kind(decision, candidate)

    return ResolvedMedia(
        primary_kind=primary,
        audio_url=candidate.audio_url,
        video_url=candidate.video_url,
        decision=decision,
        client_timeout_ms=settings.media_client_video_timeout_ms,
        disclaimer_key=AVATAR_DISCLAIMER_KEY if primary == "video" else None,
    )


def _decide(
    candidate: MediaCandidate, viewer: ViewerContext, *, settings: Settings
) -> MediaDecision:
    """Walk the gates video has to clear, reporting the first that closes.

    Order matters for what gets reported, not for the outcome. It narrows from
    "no learner could have had video here" to "this learner did not", so the
    reason names the most general cause — otherwise a global kill switch would
    be reported as a per-user preference and the A/B numbers would be unreadable.
    """
    if candidate.audio_url is None and candidate.video_url is None:
        return "text_only"

    # S2's gate. False until the entry conditions in PRD 7.2 are met, and it
    # outranks everything else so switching it off is genuinely total.
    if not settings.media_video_enabled:
        return "video_globally_disabled"

    if candidate.item_media_type != "video":
        return "item_is_audio_only"

    if candidate.video_url is None:
        # Marked as video, never rendered — --with-video was off for this build.
        return "no_video_asset"

    if _plan_rank(viewer.plan) < _plan_rank(settings.media_video_min_plan):
        return "plan_below_minimum"

    # A deliberate choice about someone's data bill outranks their entitlement:
    # a Pro learner who turned this on still gets audio.
    if viewer.data_saver:
        return "data_saver"

    if viewer.experiment_variant == VARIANT_AUDIO:
        return "experiment_control"

    return "video"


def _primary_kind(decision: MediaDecision, candidate: MediaCandidate) -> MediaKind | None:
    if decision == "video":
        return "video"
    if decision == "text_only":
        return None
    # Every other decision means audio — unless there is no audio either, which
    # happens when a video-only item loses its one asset.
    return "audio" if candidate.audio_url is not None else None


def _plan_rank(plan: str) -> int:
    """Raises on an unknown plan rather than silently ranking it lowest.

    Ranking an unrecognised plan as free would quietly deny video to everyone on
    a plan added later, and the symptom would be a support ticket rather than an
    error.
    """
    try:
        return _PLAN_RANK[plan]
    except KeyError:
        # Insertion order is rank order, so this lists them cheapest first.
        raise ValueError(f"unknown plan {plan!r}; ranked plans are {list(_PLAN_RANK)}") from None
