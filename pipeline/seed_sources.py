"""Where seed content is allowed to come from. Red line R7, made mechanical.

PRD section 5.5 draws the line: national standards, exam syllabuses, public
word lists, public band descriptors and CC-licensed material are usable;
publisher textbooks are not — **and "not" covers rewriting them and using them
as few-shot examples for generation**, not only copying them.

A prose rule nobody can run is a rule that gets forgotten around midnight. So
every seed file names its sources by id, and an id that is not in the registry
below is refused. Adding a source is therefore a diff somebody reviews, with
the licence written down next to it, rather than a sentence typed into a YAML
file at the moment it was needed.

**The registry is code, not configuration.** CONFIG_REFERENCE.md's test for a
configuration value is "could this change because of production data?" — a
copyright boundary cannot, and an environment variable that widens it would be
a way to widen it without review (docs/DECISIONS.md D-078).

The forbidden-title scan at the bottom is a second, weaker net. A whitelist
already refuses an unknown source id; the scan catches the case where the id is
legitimate and a textbook's name turns up in the free text beside it — someone
recording where a sentence really came from. It stops an honest mistake. It
cannot stop a determined one, and nothing here should be read as though it
could: the protection is the whitelist plus review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Source:
    """One approved origin for seed material."""

    #: Stable id written in seed files. Dotted, language-prefixed.
    id: str
    #: What it is, in English, for someone reading the registry cold.
    title: str
    #: Licence or public status, as the rights holder states it.
    licence: str
    #: True when the licence obliges us to credit, i.e. every CC-BY family
    #: licence. A seed citing such a source must carry the attribution text,
    #: because PRD 5.5 requires it to travel with the pack into the product's
    #: about page — and text nobody wrote at seed time never appears there.
    attribution_required: bool
    url: str = ""


#: The whitelist of PRD section 5.5, enumerated. Grouped by the PRD subsection
#: each entry comes from so the two can be read side by side.
#:
#: **Licence strings are a rights claim, so where they came from matters.**
#: Three were checked against the rights holder's own words (2026-09-20):
#: kh.moeys.oer states CC-BY 3.0 on oer.moeys.gov.kh, corpus.tatoeba states
#: CC BY 2.0 FR on its downloads page, and corpus.cc_cedict states CC-BY-SA
#: **3.0** — the PRD says only "CC-BY-SA", and this registry said 4.0 until
#: that check. The rest carry a status rather than a licence — a national
#: standard, an examination syllabus, a published word list — which is what
#: PRD 5.5 permits them as, not a statement that somebody read their terms.
#: Before any of the English-side entries is actually built on, check them:
#: "public word list" is our shorthand, not Oxford's.
ALLOWED_SOURCES: Final[tuple[Source, ...]] = (
    # --- 5.1 Chinese ------------------------------------------------------
    Source(
        id="zh.standard.2021",
        title="国际中文教育中文水平等级标准 (GF 0025-2021)",
        licence="national-standard",
        attribution_required=False,
    ),
    Source(
        id="zh.hsk.syllabus",
        title="HSK examination syllabus (levels, word lists, item types)",
        licence="public-syllabus",
        attribution_required=False,
    ),
    Source(
        id="zh.hskk.syllabus",
        title="HSKK speaking examination syllabus (task types, scoring criteria)",
        licence="public-syllabus",
        attribution_required=False,
    ),
    Source(
        id="zh.bct.syllabus",
        title="BCT business Chinese syllabus (workplace scenario list)",
        licence="public-syllabus",
        attribution_required=False,
    ),
    Source(
        id="zh.yct.syllabus",
        title="YCT youth Chinese syllabus (reserved; V1 does not build on it)",
        licence="public-syllabus",
        attribution_required=False,
    ),
    # --- 5.2 English (v2; seed may accumulate now) ------------------------
    Source(
        id="en.gse.toolkit",
        title="GSE Teacher Toolkit learning objectives",
        licence="public-reference",
        attribution_required=False,
    ),
    Source(
        id="en.cefr.companion",
        title="CEFR Companion Volume",
        licence="public-reference",
        attribution_required=False,
    ),
    Source(
        id="en.oxford3000",
        title="Oxford 3000 word list",
        licence="public-word-list",
        attribution_required=False,
    ),
    Source(
        id="en.oxford5000",
        title="Oxford 5000 word list",
        licence="public-word-list",
        attribution_required=False,
    ),
    Source(
        id="en.ielts.band_descriptors",
        title="IELTS public speaking band descriptors",
        licence="public-rubric",
        attribution_required=False,
    ),
    # --- 5.3 Cambodia -----------------------------------------------------
    Source(
        id="kh.moeys.oer",
        title="MoEYS open educational resources",
        licence="CC-BY-3.0",
        attribution_required=True,
        url="https://oer.moeys.gov.kh",
    ),
    Source(
        id="kh.moeys.curriculum",
        title="MoEYS curriculum framework",
        licence="public-curriculum",
        attribution_required=False,
    ),
    # --- 5.4 Corpora ------------------------------------------------------
    Source(
        id="corpus.tatoeba",
        title="Tatoeba English-Khmer sentence pairs",
        licence="CC-BY-2.0-FR",
        attribution_required=True,
        url="https://tatoeba.org",
    ),
    Source(
        id="corpus.cc_cedict",
        title="CC-CEDICT Chinese-English dictionary",
        licence="CC-BY-SA-3.0",
        attribution_required=True,
        url="https://cc-cedict.org",
    ),
)

SOURCES_BY_ID: Final[dict[str, Source]] = {source.id: source for source in ALLOWED_SOURCES}


#: Titles that must never appear anywhere in a seed file. The first three are
#: named in PRD 5.5; the rest are the same kind of thing — widely used
#: publisher series a contributor might reach for without thinking of them as
#: "a textbook". Matched as substrings, case-insensitively, against every
#: string in the file, book-title brackets stripped, so 《发展汉语》 and
#: "Fazhan Hanyu" are both caught.
FORBIDDEN_TITLES: Final[tuple[str, ...]] = (
    "HSK标准教程",
    "标准教程",
    "发展汉语",
    "fazhan hanyu",
    "新实用汉语课本",
    "实用汉语课本",
    "practical chinese reader",
    "博雅汉语",
    "boya chinese",
    "integrated chinese",
    "汉语教程",
)

#: Stripped before matching so that 《发展汉语》 and 发展汉语 are the same
#: string. CJK brackets and quotes only — ASCII punctuation appears inside
#: legitimate free text.
_TITLE_NOISE: Final[str] = "《》〈〉「」『』\u201c\u201d\u2018\u2019 \t"


def known_source(source_id: str) -> Source | None:
    """The registry entry for an id, or None when it is not on the whitelist."""
    return SOURCES_BY_ID.get(source_id)


def forbidden_title_in(text: str) -> str | None:
    """The first forbidden textbook title found in ``text``, if any.

    Case-insensitive, and blind to the brackets a Chinese title is usually
    written in. Returns the title as the registry spells it so the error
    message can quote the rule rather than the user's own words.
    """
    haystack = "".join(character for character in text.lower() if character not in _TITLE_NOISE)
    for title in FORBIDDEN_TITLES:
        needle = "".join(character for character in title.lower() if character not in _TITLE_NOISE)
        if needle in haystack:
            return title
    return None
