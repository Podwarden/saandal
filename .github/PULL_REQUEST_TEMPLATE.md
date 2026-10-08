## What this changes

<!-- Brief description of the failure/behavior and the fix. -->

## Checklist

- [ ] Every new hook/middleware is **fail-safe** (any internal error passes the turn through unchanged).
- [ ] Added/updated unit tests; all offline suites pass locally.
- [ ] Updated the README (one section per version) and any relevant `docs/`.
- [ ] Commits are **signed off** for the DCO (`git commit -s`).
- [ ] No hermes-agent source is vendored; core overrides go through the monkeypatch harness or a patch-overlay.

## Related issues

<!-- Fixes #... -->
