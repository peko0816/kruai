# User-visible strings

CLAUDE.md section 7: user-visible copy is never inlined in Python. Everything
the learner reads is a key in these three files.

| file | state |
|---|---|
| `en.json` | written, but **functional placeholder wording** — plain sentences that let the bot work in development. Not signed off as product copy. |
| `km.json` | `[[km:key]]` placeholders. Khmer is the teaching language (PRD 1.1) and must be written by a native speaker (PRD 15.3 item 3). |
| `zh.json` | `[[zh:key]]` placeholders. |

**The tone of user-facing copy is the owner's decision, not an implementer's**
(CLAUDE.md section 6). Nothing here should reach a real learner before someone
who owns the product voice has read it — and, for `km.json`, before a native
speaker has written it.

BACKLOG D7 adds the CI check that `km.json` carries no placeholders. That check
is meant to fail until the translations exist; it is the gate, not a nuisance.
