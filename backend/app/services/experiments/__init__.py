"""A/B assignment for the S2-to-S3 decision (PRD 7.2).

Two halves, deliberately separated. bucket.py decides which arm a learner
belongs to and is pure — no session, no clock, no settings beyond the values
handed to it. assign.py writes that decision down once and then defends it:
after a learner is in the table, the stored row outranks any later calculation.

Read bucket.py's module docstring before touching the hash. The built-in
``hash`` is salted per process and using it here would break the experiment in a
way that produces no error and no log line.
"""

from app.services.experiments.assign import Experiments
from app.services.experiments.bucket import (
    DEFINITIONS,
    EXPLAIN_MEDIA,
    ExperimentDefinition,
    UnknownExperimentError,
    bucket_fraction,
    choose_variant,
    definition,
)

__all__ = [
    "DEFINITIONS",
    "EXPLAIN_MEDIA",
    "ExperimentDefinition",
    "Experiments",
    "UnknownExperimentError",
    "bucket_fraction",
    "choose_variant",
    "definition",
]
