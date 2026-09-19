"""Telegram Bot — the Basic tier's entry point (PRD 4.1).

Display and collection only. ARCHITECTURE section 1 is explicit that the entry
layer makes no business judgement: whether an attempt passed, whether the
learner has quota left, which recording to play — every one of those answers
comes back from the API. This package turns Telegram messages into API calls
and API responses into Telegram messages, and keeps track of where in a lesson
each learner is.
"""
