# seed/zh-hsk3.0/

The Chinese concept backbone, graded against 《国际中文教育中文水平等级标准》
(the "3.0" standard) and the HSK/HSKK syllabuses built on it.

One file per level, named for it: `hsk1.yaml`, `hsk2.yaml`, … Every concept in
a file carries that file's level in its slug (`zh.hsk1.…`), which is what makes
a concept pasted into the wrong file an error rather than a silent regrading.

V1 teaches HSK1 only (PRD 1.3). Later levels may be seeded ahead of time;
nothing generates from a level until its course exists.

`hsk1.yaml` holds all 48 level-1 grammar points, numbered 一01–一48 as appendix
A of the standard numbers them. `pipeline/tests/test_seed_hsk1.py` asserts the
file carries exactly that set, so a dropped concept fails a build rather than
being found by hand at M1.

Format and the source rules: `../README.md`.
