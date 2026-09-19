# pipeline/packs/

Output of the pipeline: generation drafts (E3), built packs (E6).

Nothing here is committed — `.gitignore` excludes `*.json` and `*.tar.gz`. They
are large, they are regenerable from the seed, and a committed draft is a file
somebody edits by hand, which puts machine output and reviewed content in the
same place with no way to tell them apart.

    make generate        # writes zh-HSK1.draft.json here

To keep a draft, keep the seed and the provider settings that produced it: the
draft records both (`seed_file`, `provider`, `model`, `generated_at`).
