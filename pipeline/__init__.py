"""Offline content pipeline (PRD section 6, ARCHITECTURE section 2.3).

A CLI package, not part of the request path. It imports the backend's models
and configuration but never a service: the pipeline runs on a laptop hours
before a learner ever sees its output.
"""
