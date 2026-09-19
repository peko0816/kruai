"""Business-rule errors.

Scope is narrow on purpose. CODING_STANDARDS section 5.1 splits failures three
ways, and only the middle one belongs here:

  1. External dependency failed (provider timeout, payment gateway down)
     -> NOT an exception. Providers return ok=False + error_code so the caller
        can choose a degradation path. Raising here would destroy that choice.
  2. Business rule not satisfied (out of quota, not subscribed)
     -> AppError subclass. The API layer maps .http_status and .code.
  3. Programming error (broken invariant, malformed internal value)
     -> native ValueError / TypeError. Let it crash; do not wrap.

So: no ConfigurationError, no ProviderError. Those are cases 3 and 1.
Add a subclass here only when a real caller raises it.
"""

from __future__ import annotations

from typing import ClassVar


class AppError(Exception):
    """A business rule was not satisfied.

    ``code`` is machine-readable and drives client-side i18n, so it is part of
    the API contract: renaming one is a breaking change.

    ``context`` carries structured detail for logging. Never put audio bytes,
    phone numbers, API keys, or raw payment payloads in it (CODING_STANDARDS
    section 6) — everything in here is expected to reach the log sink.
    """

    code: ClassVar[str] = "app.error"
    http_status: ClassVar[int] = 500

    def __init__(self, message: str | None = None, **context: object) -> None:
        self.message = message or self.code
        self.context: dict[str, object] = context
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class AuthenticationFailed(AppError):
    """The caller did not prove who they are.

    One class for every way that can happen, and the reason stays in
    ``context`` rather than in ``message``: telling a caller that the signature
    was fine but the timestamp was stale tells a forger which half to work on.
    The log keeps the detail; the response keeps the code.
    """

    code = "auth.invalid"
    http_status = 401


class PermissionDenied(AppError):
    """Authenticated, and still not allowed.

    Distinct from AuthenticationFailed on purpose: a learner whose token is
    fine but who is not an operator should be told to stop trying, not to sign
    in again. Used for the operations endpoints, where the path is in the
    published schema anyway so hiding it behind a 404 would buy nothing.
    """

    code = "auth.forbidden"
    http_status = 403


class InvalidQueryWindow(AppError):
    """A reporting window that cannot be satisfied, such as one ending before
    it starts.

    A business error rather than a native ValueError because the caller can fix
    it: raising ValueError here would answer a bad query string with a 500 and
    put someone in the logs looking for a server fault.
    """

    code = "query.window_invalid"
    http_status = 422


class ContentNotFound(AppError):
    """No such course, lesson or item — or none this learner may see.

    Deliberately the same answer for "does not exist" and "exists but is not
    yours". A distinguishable 403 would turn the id space into a directory of
    which organisations have content here (PRD 12, and the cross-org isolation
    the M4 checklist asks for).
    """

    code = "content.not_found"
    http_status = 404


class ItemNotScorable(AppError):
    """Something was said back to a card that has nothing to say back to.

    'explain' is a lecture card (PRD 9.2 gives it no mastery weight), so an
    attempt against one is a client that has lost track of where it is — not a
    broken server and not a missing item.
    """

    code = "attempt.item_not_scorable"
    http_status = 422


class AudioTooLarge(AppError):
    """The upload exceeds ATTEMPT_MAX_AUDIO_BYTES."""

    code = "attempt.audio_too_large"
    http_status = 413


class ScoringUnavailable(AppError):
    """The scorer could not assess this recording.

    Providers never raise (scoring/base.py) — they return ok=False so the
    caller can choose what to do, and this is that choice made explicit at the
    HTTP boundary. Nothing is charged and nothing is stored (ARCHITECTURE
    section 5); the learner is asked to try again.
    """

    code = "scoring.unavailable"
    http_status = 503


class InsufficientQuota(AppError):
    """Daily attempt / task allowance or realtime seconds exhausted."""

    code = "quota.insufficient"
    http_status = 402


class NotSubscribed(AppError):
    """The plan required for this capability is not active."""

    code = "sub.required"
    http_status = 402
