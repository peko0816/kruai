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

Nothing yet. `zh/hsk1.txt` is the one the HSK1 draft needs; the word tables of
GF 0025-2021 are inside the R7 whitelist (public national standard), and the
level-1 table numbers its 500 entries, which is what makes an extraction
checkable — see docs/DECISIONS.md D-089.
