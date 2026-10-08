# QUEUED design brief — fires AFTER v1.15.0 re-run confirms

Trigger condition: v1.15.0 (host-gated guards) deployed, gateways restarted, and
batch6_armA_gated.md confirms DeepSeek+saandal correctness ≈ vanilla (~97.7%)
while profiles (vllm) retain the full guard stack (vllm-path regression green).
If arm A' does NOT reach target, fix that first; do not start this brief yet.

## Goal
Make saandal self-calibrating: a load-bearing scaffold for models that need it,
a near-transparent pass-through for models that don't — decided per request, not
by a hand-maintained host list.

## Design agent scope
1. **Model-tier abstraction** (replaces the constrained_hosts hack as the guard
   signal). Config `model_tier: {<model>: weak|strong, default: weak}`. Each guard
   declares the min tier it applies at. Unknown model ⇒ weak (protect by default,
   fail-safe). One line to onboard a new model. Keep constrained_hosts only for the
   genuinely host/endpoint-specific decoding knobs (structural_tag, no_think).
2. **Add-vs-withhold guard audit.** Classify every guard:
   - ADD-only (secret-scrub, router token-shrink, a nudge, a log) → may fire freely,
     any tier — they can't remove a good answer.
   - WITHHOLD/REPLACE (corruption guard, forced-synthesis, topic-continuity
     injection, finisher) → must require behavioral corroboration (>=2 signals) or
     be reversible; NEVER remove/replace an answer on a single suspicion. This is the
     lesson of the batch4 r09 corruption false-positive that deleted a correct table.
3. **Behavioral escalation everywhere** (generalize the selfheal pattern): guards
   earn the right to act by OBSERVED signals in-turn, not by anticipation. A strong
   model never trips them (transparent); a weak model does (protected) — no nameplate
   needed. Tier just sets thresholds/defaults, not hard on/off.

## Deliverable
docs/tier_design.md + work-list for an implementation round; then A/B re-validate
on BOTH models (deepseek must stay ~vanilla; 27B profiles must stay at their high
guarded pass rate) to prove the abstraction didn't regress either end.
