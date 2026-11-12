# ADR 0025: Local audio analysis with bliss and librosa

- **Status:** Accepted
- **Date:** 2026-11-12

## Context
#338 adds a Radio (a playlist grown by similarity from a seed) and arc order (ordering a playlist by tempo, key and energy). Both need per-track audio data that mixsync doesn't have: `Track` stores no BPM, key, energy or similarity data, and the importer reads no `bpm` or `initialkey` tags. mixsync is GPL-3.0 and runs on home servers, published for amd64 only. The analyzer and where its results live are hard to change later, because stored similarity vectors only compare with vectors from the same analyzer.

## Decision
- **bliss** (`bliss-audio`, GPL-3.0) computes each track's similarity vector. **librosa** (ISC) computes BPM, and mixsync computes key (chroma matched against key profiles) and energy (loudness) on top of it. Both ship in the main image.
- Results live in the database only, never in file tags. Every track is analyzed, and existing `bpm`/`initialkey` tags are ignored, so all values come from one analyzer.
- Each result records the analyzer and its feature version. A version change queues re-analysis, and tracks are compared only with tracks from the same version.
- Analysis is a background job at the lowest priority, one track at a time. It never delays imports, searches or fingerprinting. Newly imported tracks are queued at import, existing libraries are analyzed gradually, and an admin setting turns analysis off.
- Gap-fill candidates come from ListenBrainz LB Radio artist prompts, which mixsync already calls.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **bliss + librosa (chosen)** | Both licenses suit GPL-3.0; bliss is about 0.3 s per track; small wheels | Key and energy are our own code on librosa; no arm64 bliss wheel since 0.1.7 |
| Essentia + TensorFlow models | Most features (key, mood, danceability, embeddings) | Models are CC BY-NC-SA, a restriction GPL-3.0 can't carry; 292 MB wheel; no arm64 wheel; about 3 s per track |
| CLAP (LAION, Microsoft) | Audio and text embeddings, permissive weights | 0.7–2.4 GB of weights plus PyTorch; CPU cost unmeasured |
| AcousticBrainz dumps | CC0; no CPU | Nothing after 2022; Essentia values that conflict with ours; about 3 GB |
| Read existing `bpm`/`initialkey` tags | Saves CPU | Inconsistent across taggers (a half-speed or double-speed BPM, for example) |
| Write analysis to file tags | Media servers and DJ tools could read it | Retags every file, which needs a Plan; deferred as an opt-in |
| ListenBrainz Labs similar-recordings | Richer similarity per recording | Undocumented; some endpoints already removed |

## Consequences
- The image gains numpy, scipy, numba and bliss. Their size is measured before the first #338 stage merges. If the image grows by more than about 250 MB, an optional analysis image is reconsidered.
- An arm64 image would need bliss built from source, or analysis switched off there.
- Until a library is analyzed, Radio and arc order only cover the analyzed tracks.
