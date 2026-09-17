"""Media selection and audio fallback.

Pure logic — the domain layer as ARCHITECTURE section 1 describes it. The caller
supplies the item's assets and the learner's context as values; nothing here
reads a row or a URL.

The whole decision lives on the server on purpose (ARCHITECTURE 2.2). A client
that worked it out for itself would work it out differently on each platform,
and PRD 7.2's comparison of completion rates needs every learner's arm to be
knowable without asking their phone.
"""

from app.services.media.resolve import (
    AVATAR_DISCLAIMER_KEY,
    VARIANT_AUDIO,
    VARIANT_VIDEO,
    MediaCandidate,
    MediaDecision,
    MediaKind,
    ResolvedMedia,
    ViewerContext,
    resolve,
)

__all__ = [
    "AVATAR_DISCLAIMER_KEY",
    "VARIANT_AUDIO",
    "VARIANT_VIDEO",
    "MediaCandidate",
    "MediaDecision",
    "MediaKind",
    "ResolvedMedia",
    "ViewerContext",
    "resolve",
]
