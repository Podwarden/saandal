# Contributing to saandal

Issues and pull requests are welcome. **saandal** is a reliability layer for
running self-hosted models on
[hermes-agent](https://github.com/NousResearch/hermes-agent). It is licensed
under **AGPL-3.0-or-later**.

## How changes get in

- **Bugs and ideas:** open an
  [issue](https://github.com/Podwarden/saandal/issues/new/choose). A bug report
  is most useful with the saandal version (the newest heading in
  `CHANGELOG.md` of your install), the hermes-agent version, the model and
  server you run it against, and the relevant part of the gateway log.
- **Pull requests:** fork, branch from `main`, and open a PR against `main`.
  Keep one change per PR and add or update tests for it.
- **How a PR lands:** `main` on GitHub advances by one squashed commit per
  release, built from the maintainers' release tree. A PR is therefore never
  merged with the merge button: a commit merged that way would be overwritten
  by the next release. When a PR is accepted, a maintainer applies it to the
  next release, so it reaches `main` as part of that release commit and is
  credited in `CHANGELOG.md`. The PR is then closed with a note naming the
  release that carries it.

## The prime directive: never break a turn

saandal is an out-of-tree plugin that sits in the hot path of every model call.
Its rules are not negotiable:

- **Every hook and middleware is fail-safe.** Any internal error must pass the
  turn through unchanged. A guard may withhold a bad answer, but it must never
  crash or hang a good one. New code follows the same rule (wrap it in
  `try/except` and degrade to a no-op).
- **Prefer hermes plugin hooks and middleware.** Reach into hermes internals
  only through the guarded harness in `plugin/monkeypatch.py` (symbol-guarded,
  self-skipping) or a documented patch-overlay. **Never vendor upstream code.**
- **Version history goes in `CHANGELOG.md`**, one entry per shipped version:
  failure, fix, config key, tests, residuals. The README stays task-oriented
  (install, use, configure, upgrade). A PR does not need to pick a version
  number; describe the change in the PR and the maintainer writes the
  changelog entry for the release that carries it.

## Developer setup

- Python 3.11 in a virtualenv with `hermes-agent` installed (the plugin imports
  it at runtime), plus `ruamel.yaml`, `openai`, `ddgs`.
- Run the offline unit suites (no server required):
  ```bash
  for t in tests/test_router_regression.py tests/test_selfheal_unit.py \
           tests/test_progress_unit.py tests/test_loadaware_unit.py \
           tests/test_harness_seed.py tests/test_monkeypatch_unit.py; do
    python "$t" || exit 1
  done
  ```
- `tests/gateway_harness.py` drives the **real** dispatch path and needs a live
  OpenAI-compatible endpoint, so it is intentionally **excluded from CI**.
- Deploy or repair a local install with `python bin/install.py`, then
  `python bin/verify.py`.

## Developer Certificate of Origin (DCO)

We use the [DCO](https://developercertificate.org/) instead of a CLA. Every
commit must be signed off, certifying you wrote the code or have the right to
submit it under the project's license:

```
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds this automatically. A PR whose commits are not signed off
cannot be accepted.

## Before you open a PR

1. Keep every new guard **fail-safe** and add unit tests for new behavior.
2. Update any relevant `docs/` and the README if a user-facing step changes.
3. Make sure all offline test suites pass.
4. Sign off your commits (DCO).

By contributing, you agree your contributions are licensed under
AGPL-3.0-or-later.
