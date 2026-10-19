# SamRonin profile — development & maintenance guide

This repository is a **self-updating GitHub profile**. A scheduled workflow
regenerates every SVG asset plus `README.md` / `summary.md` from live public
GitHub data, then commits only if something actually changed.

## Layout

```
scripts/profile.py                     the whole generator (stdlib only, no dependencies)
.github/workflows/update-profile.yml   scheduled refresh (every 6 hours) + tests
tests/test_profile.py                  offline test-suite (no token, no network)
assets/*.svg                           generated profile artwork (do not hand-edit)
README.md, summary.md                  generated output (do not hand-edit)
```

## Requirements

- Python **3.10+** (uses `X | Y` type unions)
- No third-party dependencies — the generator and tests use the standard library only.

## Run locally

```bash
# token is required: the contribution calendar (GraphQL API) needs one.
# A classic PAT with zero scopes, or a fine-grained read-only PAT, is enough.
export GITHUB_TOKEN=ghp_xxx          # or: GH_TOKEN / the workflow's PROFILE_TOKEN
export PROFILE_LOGIN=SamRonin        # optional, this is the default

python3 scripts/profile.py           # regenerates assets/ + README.md + summary.md
```

The generator is deterministic: identical API data in → byte-identical files out,
so a refresh run only commits when something really changed.

## Run the tests

```bash
python3 -m unittest discover -s tests -v
```

The suite is fully offline. Pagination and retry logic are exercised against a
throwaway localhost HTTP server; everything else uses fake API payloads. CI runs
the same suite before every regeneration.

## Error behaviour (what CI shows)

| Situation | Exit | Result |
|---|---|---|
| Invalid / expired token (401, plain 403) | 0 + `::warning::` | profile left unchanged |
| Rate limit exhausted (403 + limit headers, 429) | 0 + `::warning::` | profile left unchanged |
| No token at all | 0 + `::warning::` | profile left unchanged |
| Wrong `PROFILE_LOGIN` (404) | **1** + `::error::` | run fails loudly |
| Network failure after 3 retries | **1** + `::error::` | run fails loudly |
| Malformed generated SVG | **1** | run fails loudly |
| Private repo name detected in output | **1** | run fails loudly |

Transient HTTP failures (5xx / 429 / network) are retried up to 3 times with
exponential backoff, honouring `Retry-After`.

## Common problems

- **"profile left unchanged" every run** → the `PROFILE_TOKEN` secret is missing,
  expired, or revoked. Create a new read-only token and update the secret.
- **"GitHub user not found"** → `PROFILE_LOGIN` or `--login` has a typo.
- **`::warning::pagination stopped after N pages`** → the account has more than
  2 000 repos; raise `max_pages` in `gh_all_pages()`.
- **Private-leak guard fails the run** → a private repo name appeared in the
  generated text. Fix the offending description/README content, never the guard.
- **SVG validation fails** → open the file mentioned in the log; malformed output
  is a generator bug, not a rendering problem.

## Security notes

- The workflow's job has only `contents: write` and a 15-minute timeout.
- Actions are pinned to commit SHAs (`checkout@11bd719…`, `setup-python@a26af69…`).
- The token is only ever passed via the environment and is never written to disk
  or logs; only public, non-fork, non-archived repos feed the output.
- A final guard fails the build if any private repository name reaches
  `README.md` or `summary.md`.
