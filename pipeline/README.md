# pipeline/

The offline content pipeline (PRD section 6, ARCHITECTURE section 2.3). A set
of CLIs, not part of the request path: everything here runs hours before a
learner sees its output, which is what red line R1 means in practice.

## The stages

| | Command | Reads | Writes |
|---|---|---|---|
| [1] | *by hand* | the whitelisted sources of PRD 5 | `seed/<family>/<level>.yaml` |
| | `make seed` | the seed files | validation report |
| [2] | `make generate` | a seed file | `packs/<lang>-<level>.draft.json` |
| [3] | `make validate` | a draft | the eight rules of PRD 6.2 |
| [4] | `make review` | a draft | `packs/<lang>-<level>.review.csv` |
| [5] | `make build` | a draft | `packs/<lang>-<level>.pack.json` + audio |
| [6] | `make import` | a pack | the database, and published audio |

Each stage refuses rather than degrades. `generate` and `build` will not run a
billed provider without `--confirm-spend`; `build` will not freeze a draft
`validate` rejects; `import` will not take a pack that was forced past it, that
has been edited since, or whose version is already in the database.

Nothing in `packs/` is committed — it is large, regenerable, and a committed
draft is a file somebody starts editing by hand.

## The data that is committed

| Path | What it is |
|---|---|
| `seed/` | concept skeletons, typed from the sources PRD 5.5 permits |
| `wordlists/` | each level's vocabulary, and the transcription it derives from |

Both carry their provenance, and both have tests that check it: a seed cites
source ids from `seed_sources.py`, and a word list keeps the numbered table it
was read from.

## Where the rules live

| File | Rule |
|---|---|
| `seed_sources.py` | red line R7: which sources may enter the pipeline at all |
| `seed_schema.py` | what a seed file has to be |
| `generate.py` | the prompt, and the R7 comment PRD 5.5 asks for by name |
| `validate.py` | the eight content rules of PRD 6.2 |
| `wordlist.py` | rule 1's data, and the checks behind its transcription |
| `build_pack.py` | PRD 6.3: every fixed string is synthesised before it ships |
| `import_pack.py` | provenance, integrity and immutability at the database edge |
