"""The error every provider registry raises when a deployment is misconfigured.

Subclasses ValueError on purpose. A bad provider name or an uncoverable
capability is a deployment error, which CODING_STANDARDS section 5.1 says
should crash rather than become an AppError — and ValueError is what that looked
like through B1 to B4. Keeping the inheritance means those call sites and their
tests stay correct while the startup self-check gains a type it can catch
precisely.

That precision is the point. ``except ValueError`` around a provider factory
would swallow a genuine bug inside it and report the deployment as
misconfigured, sending whoever is on call to the wrong file.
"""

from __future__ import annotations


class ProviderConfigurationError(ValueError):
    """A configured provider does not exist, or cannot do what is asked of it."""
