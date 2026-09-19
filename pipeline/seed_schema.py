"""The seed file format, and the checker that refuses a bad one.

BACKLOG E1. A seed file is the hand-written half of the content pipeline: the
concept skeletons of PRD 3.2, typed in from the whitelisted sources of PRD 5.
Everything downstream is derived from it — generate.py writes sentences for
these concepts, build_pack.py freezes them, import_pack.py inserts them — so a
mistake here is a mistake in nine later places, and the cheapest moment to
refuse it is before generation has spent a single token.

What a file looks like (the full annotated example is in seed/README.md, and a
test parses that example so the two cannot drift):

    schema_version: 1
    meta:
      language: zh
      level: HSK1
      licence: "..."
      sources:
        - id: zh.standard.2021
          detail: "..."
    concepts:
      - slug: zh.hsk1.want_noun
        pattern: "我要 + [名词]"
        km_explanation: "..."
        hskk_task_types: [listen_and_repeat]
        sort_order: 10

Three of the rules are worth explaining, because each was a choice:

**One file carries its own sources.** Not a directory-level manifest. A seed
file is the unit that gets reviewed, moved and copied, and provenance that
lives in a sibling file is provenance that gets separated from the content
(docs/DECISIONS.md D-077).

**A Khmer explanation may be a placeholder, not English.** ``[[km:...]]``, the
same marker the message catalogue uses (D-050). Khmer is the teaching language
and has to be written by a native speaker; the alternative to a placeholder is
not "good Khmer sooner", it is English sitting in a Khmer field where nothing
will ever flag it. ``--strict`` refuses placeholders and is the M1 gate, the
same split as ``make i18n`` / ``make i18n-strict`` (D-051, D-079).

**Every duplicate key in the YAML is an error.** PyYAML silently keeps the
last, so a file with two ``km_explanation:`` lines loads, validates, and drops
one of them without a word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pipeline import seed_sources

#: The only schema version there is. Declared in every file so that a later,
#: incompatible shape can be introduced without guessing which one a file is.
SCHEMA_VERSION: Final = 1

#: HSKK speaking task types (PRD 5.1: the syllabus is the authority for what a
#: spoken turn is judged as). Closed set on purpose — the point of
#: ``hskk_task_types`` is to tie a concept to an external standard, and a typo
#: that silently becomes a new task type unties it. The three HSKK levels use
#: six task forms between them; a seventh means the syllabus changed, which is
#: a code change with a test, not a free-text field (D-080).
HskkTaskType = Literal[
    "listen_and_repeat",  # 听后重复 (初级, 中级)
    "listen_and_answer",  # 听后回答 (初级)
    "answer_questions",  # 回答问题 (初级, 中级, 高级)
    "describe_picture",  # 看图说话 (中级)
    "listen_and_retell",  # 听后复述 (高级)
    "read_aloud",  # 朗读 (高级)
]

#: Khmer, U+1780-U+17FF (CODING_STANDARDS section 11).
_KHMER = re.compile(r"[\u1780-\u17ff]")

#: An explanation still waiting for a native speaker. Same shape as the message
#: catalogue's placeholder so one habit covers both.
_KM_PLACEHOLDER = re.compile(r"^\[\[km:[a-z0-9._-]+\]\]$")

#: ``zh.hsk1.want_noun``: language, level, name. Lower case throughout because
#: it ends up in URLs, filenames and log lines.
_SLUG = re.compile(r"^[a-z]{2}\.[a-z0-9]+\.[a-z0-9_]+$")

_LEVEL = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}$")

#: Levels of the standard V1 teaches against (PRD 5.1: three stages, nine
#: levels). Restricted for ``zh`` only; other languages grade differently.
_ZH_LEVEL = re.compile(r"^HSK[1-9]$")


class SeedSource(BaseModel):
    """One citation. ``id`` must be on the R7 whitelist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    #: Which part of it — chapter, table, level. Free text, for a human.
    detail: str = ""
    url: str = ""
    #: Credit line, as the licence requires it to be shown. Required when the
    #: registry marks the source attribution_required; it travels with the pack
    #: to the product's about page (PRD 5.5).
    attribution: str = ""

    @field_validator("id")
    @classmethod
    def _on_the_whitelist(cls, value: str) -> str:
        if seed_sources.known_source(value) is None:
            allowed = ", ".join(sorted(seed_sources.SOURCES_BY_ID))
            raise ValueError(
                f"{value!r} is not an approved source (PRD 5.5, red line R7). "
                f"Approved ids: {allowed}. Adding one is a change to "
                f"pipeline/seed_sources.py, reviewed with its licence."
            )
        return value

    @model_validator(mode="after")
    def _credited(self) -> SeedSource:
        source = seed_sources.known_source(self.id)
        if source is not None and source.attribution_required and not self.attribution.strip():
            raise ValueError(
                f"{self.id!r} is {source.licence} and must be credited: set "
                f"'attribution' to the line the product will display"
            )
        return self


class L1Error(BaseModel):
    """A mistake Khmer speakers typically make on this pattern (PRD 3.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    wrong: str = Field(min_length=1)
    right: str = Field(min_length=1)
    #: Why, in Khmer. Optional at seed time; a placeholder is fine.
    km_note: str = ""

    @model_validator(mode="after")
    def _differs(self) -> L1Error:
        if self.wrong.strip() == self.right.strip():
            raise ValueError("'wrong' and 'right' are the same sentence")
        return self


class SeedConcept(BaseModel):
    """One concept skeleton: the unit of mastery (PRD 3.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    pattern: str = Field(min_length=1)
    #: Khmer prose, or a ``[[km:...]]`` placeholder. Never English.
    km_explanation: str = Field(min_length=1)
    hskk_task_types: tuple[HskkTaskType, ...] = ()
    common_l1_errors: tuple[L1Error, ...] = ()
    #: Optional examples for the generator to take its cue from. They are seed
    #: material like everything else here and fall under the file's sources.
    example_sentences: tuple[str, ...] = ()
    #: Teaching order within the level. Unique per file: two concepts claiming
    #: the same position leave their order to whatever the database returns.
    sort_order: int = Field(default=0, ge=0)

    # One validator per field rather than one per model: pydantic reports the
    # field a validator is attached to, and "concepts[7].km_explanation" is
    # what somebody needs to fix a 40-concept file. "concepts[7]" is not.

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, value: str) -> str:
        if not _SLUG.match(value):
            raise ValueError(
                f"{value!r} is not a slug: expected language.level.name, "
                f"lower case, e.g. 'zh.hsk1.want_noun'"
            )
        return value

    @field_validator("pattern")
    @classmethod
    def _pattern_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank")
        return value

    @field_validator("km_explanation")
    @classmethod
    def _khmer_or_placeholder(cls, value: str) -> str:
        if not _is_khmer_or_placeholder(value):
            raise ValueError(
                "must be written in Khmer (U+1780-U+17FF) or left as a "
                "'[[km:...]]' placeholder for a native speaker. English here "
                "ships untranslated text nothing will flag."
            )
        return value

    @field_validator("example_sentences")
    @classmethod
    def _no_blank_sentence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not sentence.strip() for sentence in value):
            raise ValueError("contains a blank entry")
        return value

    @property
    def awaits_translation(self) -> bool:
        return _KM_PLACEHOLDER.match(self.km_explanation.strip()) is not None


class SeedMeta(BaseModel):
    """What the whole file is, and where it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: Literal["zh", "en"]
    level: str
    #: The pack's licence, as it will be stored in ``content_packs.licence``.
    #: Written by a person: combining several sources' terms into one statement
    #: is a legal judgement, not a string operation.
    licence: str
    #: Not ``min_length=1``: a failed source is dropped from the tuple, and a
    #: length rule would then add "sources is empty" underneath the real
    #: reason, contradicting it. Emptiness is checked below, which pydantic
    #: only reaches once every source validated.
    sources: tuple[SeedSource, ...]

    @field_validator("licence")
    @classmethod
    def _licence_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank; PRD 5.5 requires a licence on every pack")
        return value

    @field_validator("level")
    @classmethod
    def _level_shape(cls, value: str) -> str:
        if not _LEVEL.match(value):
            raise ValueError(f"{value!r} is not a level, e.g. 'HSK1'")
        return value

    @model_validator(mode="after")
    def _shape(self) -> SeedMeta:
        if not self.sources:
            raise ValueError("'sources' is empty; PRD 5.5 requires at least one")
        if self.language == "zh" and not _ZH_LEVEL.match(self.level):
            raise ValueError(f"{self.level!r} is not an HSK level: expected HSK1 through HSK9")
        return self

    @property
    def slug_prefix(self) -> str:
        """What every concept slug in this file has to start with."""
        return f"{self.language}.{_level_token(self.level)}."


class SeedFile(BaseModel):
    """One YAML file, parsed and checked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    meta: SeedMeta
    #: Not ``min_length=1``, for the reason SeedMeta.sources gives.
    concepts: tuple[SeedConcept, ...]

    @model_validator(mode="after")
    def _consistent(self) -> SeedFile:
        if not self.concepts:
            raise ValueError("'concepts' is empty; a seed file with no concepts seeds nothing")
        prefix = self.meta.slug_prefix
        for index, concept in enumerate(self.concepts):
            if not concept.slug.startswith(prefix):
                raise ValueError(
                    f"concepts[{index}].slug {concept.slug!r} does not belong to this "
                    f"file: language {self.meta.language!r} and level "
                    f"{self.meta.level!r} require the prefix {prefix!r}"
                )
            # PRD 6.2 refuses a concept with no HSKK task type, because then
            # nothing outside our own judgement says whether a spoken turn
            # passed. The rule is Chinese-specific: HSKK is a Chinese exam.
            if self.meta.language == "zh" and not concept.hskk_task_types:
                raise ValueError(
                    f"concepts[{index}].hskk_task_types is empty; PRD 6.2 requires "
                    f"every Chinese concept to name the HSKK task it is judged as"
                )
            if self.meta.language != "zh" and concept.hskk_task_types:
                raise ValueError(
                    f"concepts[{index}].hskk_task_types is set on a "
                    f"{self.meta.language!r} concept; HSKK grades Chinese only"
                )

        _reject_repeats("slug", [(index, c.slug) for index, c in enumerate(self.concepts)])
        _reject_repeats(
            "sort_order", [(index, c.sort_order) for index, c in enumerate(self.concepts)]
        )
        return self

    @property
    def awaiting_translation(self) -> tuple[str, ...]:
        """Slugs whose Khmer explanation is still a placeholder."""
        return tuple(c.slug for c in self.concepts if c.awaits_translation)


# ------------------------------------------------------------------- problems


@dataclass(frozen=True)
class SeedProblem:
    """One reason a file was refused, addressed to whoever has to fix it."""

    path: Path
    #: Dotted path to the offending field, e.g. ``concepts[2].km_explanation``.
    #: Empty when the whole file is the problem (unreadable, not a mapping).
    field: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}" if not self.field else f"{self.path}: {self.field}"
        return f"{where}: {self.message}"


class SeedError(Exception):
    """Raised by the loading functions. Carries every problem, not the first.

    One error at a time turns fixing a file into a guessing game with a
    round trip per field.
    """

    def __init__(self, problems: list[SeedProblem]) -> None:
        super().__init__("; ".join(str(problem) for problem in problems))
        self.problems = problems


# --------------------------------------------------------------------- input


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with a repeated key.

    PyYAML's default is last-one-wins, silently. In a seed file that means one
    of two ``km_explanation:`` lines is thrown away by the parser and every
    check downstream passes on what is left.
    """

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.MarkedYAMLError(
                    context="while reading a mapping",
                    problem=f"duplicate key {key!r}; one of the two values would be discarded",
                    problem_mark=key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_seed(raw: object, *, origin: Path) -> SeedFile:
    """Validate an already-parsed document.

    Raises:
        SeedError: with one problem per offending field.
    """
    if not isinstance(raw, dict):
        kind = "empty" if raw is None else type(raw).__name__
        raise SeedError([SeedProblem(origin, "", f"expected a YAML mapping, found {kind}")])

    # The R7 scan runs over the raw document, independently of whether the
    # schema validated. Otherwise a file with a typo in it would be refused
    # for the typo, fixed, and only then reveal that it names a textbook —
    # and the second refusal arrives after somebody has done the work.
    forbidden = _forbidden_titles_in(raw, origin)

    try:
        seed = SeedFile.model_validate(raw)
    except ValidationError as error:
        raise SeedError(forbidden + _problems_from(error, origin)) from None

    if forbidden:
        raise SeedError(forbidden)
    return seed


def load_seed_file(path: Path) -> SeedFile:
    """Read and validate one seed file.

    Raises:
        SeedError: unreadable, unparseable, or invalid.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SeedError(
            [SeedProblem(path, "", f"cannot read: {error.strerror or error}")]
        ) from None
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise SeedError(
            [SeedProblem(path, "", f"not valid YAML: {_yaml_message(error)}")]
        ) from None
    return parse_seed(raw, origin=path)


def check_seed_file(path: Path) -> tuple[SeedFile | None, list[SeedProblem]]:
    """Validate one file without raising. Exactly one side is populated."""
    try:
        return load_seed_file(path), []
    except SeedError as error:
        return None, error.problems


@dataclass(frozen=True)
class SeedCorpus:
    """Everything one run of the checker learned about a tree of seed files."""

    files: dict[Path, SeedFile]
    problems: list[SeedProblem]

    @property
    def concept_count(self) -> int:
        return sum(len(seed.concepts) for seed in self.files.values())

    @property
    def awaiting_translation(self) -> dict[Path, tuple[str, ...]]:
        """Files with Khmer explanations still waiting for a native speaker."""
        return {
            path: seed.awaiting_translation
            for path, seed in sorted(self.files.items())
            if seed.awaiting_translation
        }

    @property
    def placeholder_total(self) -> int:
        return sum(len(slugs) for slugs in self.awaiting_translation.values())


def check_seed_tree(root: Path) -> SeedCorpus:
    """Validate every seed file under ``root``, plus what only the set shows.

    A slug is unique across the whole corpus, not per file — ``concepts.slug``
    is UNIQUE in the database (DATA_MODEL.sql), so a collision between two
    files is an import that fails halfway, hours after generation paid for the
    content.
    """
    files: dict[Path, SeedFile] = {}
    problems: list[SeedProblem] = []

    for path in seed_files_under(root):
        seed, found = check_seed_file(path)
        problems.extend(found)
        if seed is not None:
            files[path] = seed

    seen: dict[str, Path] = {}
    for path, seed in sorted(files.items()):
        for concept in seed.concepts:
            first = seen.setdefault(concept.slug, path)
            if first != path:
                problems.append(
                    SeedProblem(
                        path,
                        f"concepts[{concept.slug}]",
                        f"slug already defined in {first}; concepts.slug is UNIQUE",
                    )
                )
    return SeedCorpus(files=files, problems=problems)


def seed_files_under(root: Path) -> list[Path]:
    """Seed files in a tree, sorted. Nothing that starts with ``_`` or ``.``."""
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*.yaml")
        if not any(part.startswith((".", "_")) for part in path.relative_to(root).parts)
    )


# ------------------------------------------------------------------ internals


def _level_token(level: str) -> str:
    return "".join(character for character in level if character.isalnum()).lower()


def _is_khmer_or_placeholder(text: str) -> bool:
    """Khmer script present, or the explicit 'not written yet' marker.

    Presence, not purity: a good Khmer explanation may well quote the Chinese
    pattern it is explaining, and a rule that banned Han characters would
    refuse the clearest explanations in the file. What it has to catch is an
    explanation written in some other language, and that one carries no Khmer
    at all.
    """
    stripped = text.strip()
    return bool(_KM_PLACEHOLDER.match(stripped)) or bool(_KHMER.search(stripped))


def _reject_repeats(field: str, values: list[tuple[int, object]]) -> None:
    first_seen: dict[object, int] = {}
    for index, value in values:
        if value in first_seen:
            raise ValueError(
                f"concepts[{index}].{field} {value!r} repeats concepts[{first_seen[value]}]"
            )
        first_seen[value] = index


def _problems_from(error: ValidationError, origin: Path) -> list[SeedProblem]:
    """Turn pydantic's locations into field paths a person can search for."""
    problems: list[SeedProblem] = []
    for detail in error.errors():
        message = detail["msg"]
        # pydantic prefixes messages raised by a validator; the prefix says
        # nothing the field path does not already say.
        message = message.removeprefix("Value error, ").removeprefix("Assertion failed, ")
        problems.append(SeedProblem(origin, _dotted(detail["loc"]), message))
    return problems


def _dotted(location: tuple[int | str, ...]) -> str:
    rendered = ""
    for part in location:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered = f"{rendered}.{part}" if rendered else str(part)
    return rendered


def _forbidden_titles_in(raw: object, origin: Path, path: str = "") -> list[SeedProblem]:
    """Walk the raw document looking for publisher textbook names (R7)."""
    if isinstance(raw, str):
        title = seed_sources.forbidden_title_in(raw)
        if title is None:
            return []
        return [
            SeedProblem(
                origin,
                path,
                f"mentions {title!r}, a publisher textbook. PRD 5.5 excludes those "
                f"from the pipeline, including as rewritten material and as few-shot "
                f"examples. If this is a false match, name the source differently.",
            )
        ]
    if isinstance(raw, dict):
        return [
            problem
            for key, value in raw.items()
            for problem in _forbidden_titles_in(
                value, origin, f"{path}.{key}" if path else str(key)
            )
        ]
    if isinstance(raw, list):
        return [
            problem
            for index, value in enumerate(raw)
            for problem in _forbidden_titles_in(value, origin, f"{path}[{index}]")
        ]
    return []


def _yaml_message(error: yaml.YAMLError) -> str:
    """PyYAML's message without the multi-line snippet it prints around it."""
    if isinstance(error, yaml.MarkedYAMLError) and error.problem_mark is not None:
        mark = error.problem_mark
        return f"{error.problem} (line {mark.line + 1}, column {mark.column + 1})"
    return str(error).replace("\n", " ")
