"""Azure Speech, for the M0 verification scripts only.

CLAUDE.md section 3 rules out real provider implementations until M0 has
concluded, and M0 cannot conclude without calling a real provider. D-125 and
D-126 resolve that by keeping these classes here: they implement the
normative interfaces unchanged, the M0 scripts reach them through
``m0.providers``, and ``app/services/`` still knows only Fake. When M0 has an
answer, E8 and E9 move whichever survives into ``app/services/`` and add one
registry entry each.
"""

from m0.azure.scoring import AzureScorer
from m0.azure.tts import AzureTTS

__all__ = ["AzureScorer", "AzureTTS"]
