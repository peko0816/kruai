# pipeline/seed/

Hand-written concept skeletons — stage [1] of the pipeline (PRD section 6.1).
Everything downstream is derived from what is in here.

One directory per source family, matching ARCHITECTURE section 6:

| Directory | Holds | Built from (PRD 5) |
|---|---|---|
| `zh-hsk3.0/` | The HSK 1–9 concept backbone. V1 ships HSK1. | 国际中文教育中文水平等级标准, HSK / HSKK syllabuses |
| `zh-hskk/` | Speaking-task material that is not tied to one HSK level. | HSKK syllabus |
| `zh-bct/` | B-side job packs (hotel front desk first). | BCT scenario list, M4 |
| `en-gse/` | English, v2. Seed may accumulate now; nothing builds it. | GSE Teacher Toolkit, CEFR |

Validate before committing:

```bash
make seed
```

## The format

`schema_version: 1`. Everything below is checked by `pipeline/seed_schema.py`,
which names the offending field when it refuses a file.

```yaml
schema_version: 1

meta:
  language: zh                # zh | en
  level: HSK1                 # zh must be HSK1..HSK9
  # Goes into content_packs.licence. Written by a person: combining several
  # sources' terms into one statement is a legal judgement, not a string join.
  licence: "Derived from public standards and syllabuses; see sources."
  sources:                    # at least one, every id on the R7 whitelist
    - id: zh.standard.2021
      detail: "Level 1 grammar points, table 3"
    - id: zh.hskk.syllabus
      detail: "初级 task types"

concepts:
  - slug: zh.hsk1.want_noun   # language.level.name — must match meta
    pattern: "我要 + [名词]"
    # Khmer (U+1780-U+17FF), or this placeholder until a native speaker
    # writes it. Never English: English here is untranslated text that
    # nothing downstream will flag.
    km_explanation: "[[km:concept.zh.hsk1.want_noun]]"
    hskk_task_types: [listen_and_repeat, listen_and_answer]
    sort_order: 10            # unique within the file; teaching order
    common_l1_errors:         # optional
      - wrong: "我要的书"
        right: "我要书"
        km_note: "[[km:concept.zh.hsk1.want_noun.error.de]]"
    example_sentences:        # optional; cues for generate.py
      - "我要水。"
```

### hskk_task_types

A closed set, from the HSKK syllabus. Required on every `zh` concept (PRD 6.2)
and rejected on any other language — HSKK grades Chinese.

| Value | HSKK | Level |
|---|---|---|
| `listen_and_repeat` | 听后重复 | 初级, 中级 |
| `listen_and_answer` | 听后回答 | 初级 |
| `answer_questions` | 回答问题 | 初级, 中级, 高级 |
| `describe_picture` | 看图说话 | 中级 |
| `listen_and_retell` | 听后复述 | 高级 |
| `read_aloud` | 朗读 | 高级 |

These six are written from the syllabus, not checked line by line against
an exam-board document (docs/DECISIONS.md D-080). Confirm them once during
the M1 content review; a correction is a change to the `HskkTaskType`
literal and this table.

## Where material may come from

**Red line R7.** PRD 5.5: national standards, exam syllabuses, public word
lists, public band descriptors and CC-licensed material are usable. Publisher
textbooks are not — and that covers rewriting them, and using them as few-shot
examples when generating.

The approved ids are enumerated in `pipeline/seed_sources.py`. An id that is
not there is refused, so adding a source is a reviewed change with its licence
written down beside it. Sources licensed CC-BY must carry an `attribution`
line; it travels with the pack to the product's about page.

A seed file carries its own sources rather than inheriting them from the
directory: the file is what gets reviewed, moved and copied, and provenance in
a sibling file gets separated from the content it describes.
