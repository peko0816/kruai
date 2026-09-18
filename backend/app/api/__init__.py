"""HTTP layer.

ARCHITECTURE section 1: this layer authenticates, validates and orchestrates.
It does not decide anything — not whether a learner has quota, not whether an
attempt passed, not whether to serve video. Those answers come from the domain
services, so that they stay testable without a request.
"""
