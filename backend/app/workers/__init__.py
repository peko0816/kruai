"""Jobs that run on a schedule rather than on a request.

ARCHITECTURE section 6 puts these here; PRD section 8 picks Redis and RQ to run
them. What lives in this package is the *job* — find the rows, call the domain
service, log what happened — and never the rule it applies. A job is one more
caller of the same code a request would use, so that "what a paid order grants"
cannot mean two things depending on who noticed the payment.

Each job is runnable on its own::

    uv run python -m app.workers.payments

so it can be driven by cron, by RQ, or by a person with a terminal at three in
the morning. Wiring them to a scheduler belongs to BACKLOG D8b, which brings
the renewal timers with it.
"""
