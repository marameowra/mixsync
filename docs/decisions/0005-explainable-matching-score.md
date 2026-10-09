# ADR 0005: Explainable matching score, ML calibration later

- **Status:** Accepted
- **Date:** 2026-10-09

## Context
Bad matching is the main complaint about soulsync. The goals ask whether a decision model should handle partial matches. A trained model needs labeled data, which doesn't exist yet. Users also need to understand *why* a file was accepted or rejected in order to trust the system and fix its mistakes.

## Decision
1. Start with an **explainable weighted score** modeled on beets' "distance" approach. Each feature contributes a visible penalty that is shown in the review UI.
2. **Per-user thresholds** turn the score into three bands: auto-accept, review, or reject. Presets are strict, balanced, and loose, and each is editable.
3. **Log every decision and every user override** as labeled data.
4. After roughly 300 overrides, **fit a logistic regression on the same features** to calibrate the weights. Adopt the new weights only if the [regression suite](../testing.md#matcher-regression-suite) improves.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Weighted score → calibrated (chosen)** | Explainable; works on day one; produces its own training data; calibration is measurable | Weights are hand-tuned at first |
| ML model from day one | Could capture subtle patterns | No training data; hard to explain; hard to debug |
| Fingerprint only (AcoustID yes/no) | Simple | Fails on tracks missing from AcoustID; can't pick between releases; ignores pre-download evidence |
| Rule chains (if X and Y then accept) | Explainable | Brittle; combinations multiply; this is roughly where soulsync's matching falls short |

## Consequences
- Every feature must be computable and displayable on its own.
- The scorer is versioned, and each stored decision records the scorer version that made it.
- Details are in [design/matching.md](../design/matching.md).
