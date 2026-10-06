# R0 baseline (DEV-R0FREEZE)

Evidence of the full suite on the frozen R0 integration commit, run in the R0c reference environment
(`scripts/refenv.sh --src <fresh clone of the tested sha> --venv <new dir> --test`). This commit changes no code.

| what | value |
|---|---|
| tested sha | `8ead7893e4051ed82cfc357ccf7f52d1d812614b` (integration 277945de + merge of claude/DEV-R0c 78d8c69) |
| python | 3.13.16 |
| pip | 26.2.1 |
| rlo-sdk | 0.11.1 |
| ga-sdk | 0.18.1 |
| playwright | 1.56.0 (preinstalled Chromium) |
| `scripts/refenv-constraints.txt` sha256 | `8d6bd1b677c3d827bc8e788c5bfe5d9d8d0a3650b1cd2ba7dbd03ba33e0962b9` |
| result | 1411 passed, 0 failed, 0 errors, 1 skipped (pytest -q -rs), exit 0, 18:20 |
| skip | `tests/test_unified.py:202` ga.rlo.cli is there (GR moved it in): by design |
| date | 2026-10-07 KST |
