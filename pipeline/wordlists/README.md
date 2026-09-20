# pipeline/wordlists/

The vocabulary each level is allowed to use — data for rule 1 of PRD 6.2
(`validate.py`, "词表越界").

One file per language and level, `<language>/<level>.txt`, cumulative: a level's
file holds its own words *and* every word below it, because a sentence at HSK3
may of course use HSK1 words.

```
# source: 《国际中文教育中文水平等级标准》(GF 0025-2021) 表 6, 一级
# source: ...
我
你
要
```

`#` starts a comment; `# source:` lines are kept and travel with the list, which
is how R7 provenance survives to the place the rule runs.

**A missing list is an error, not an empty one.** `validate.py` exits 2 rather
than reporting "no violations": a rule that passes because nobody supplied its
data is worse than no rule, because the build goes green.

## What is here

`zh/hsk1.txt` — the 500 words of the standard's level-1 table, expanded to 517
forms (a row like `爸爸|爸` is two words, `好玩ㄦ` is two spellings).

It is **derived**, not typed: `zh/hsk1.source.tsv` holds the transcription of
record — number, word and pinyin as the table prints them — and `make wordlist`
expands it. A correction goes into the table; editing the list by hand fails a
test.

Two checks stand behind the transcription (docs/DECISIONS.md D-091), both in
`pipeline/tests/test_wordlist_hsk1.py`: the numbering must run 1–500 unbroken,
and every row's characters must agree with its own pinyin column under
`pypinyin` — two independently read columns confirming each other. Neither
makes it certainly right; they make the remaining ways to be wrong narrow and
named.
