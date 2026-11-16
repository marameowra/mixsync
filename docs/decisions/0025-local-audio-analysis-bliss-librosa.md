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

## Amendment (2026-11-16): bliss built from source, in the main image
- **Why:** the `bliss-audio` 0.2.0 wheel on PyPI (the only one for Python 3.13, with no sdist) uses AVX-512 instructions without a fallback. It dies with SIGILL on CPUs without AVX-512: AMD Zen 1–3, the Intel N100 and most consumer Intel chips. The worker can't catch that as a per-track failure. It also crashes under Docker's x86-64 emulation on Apple silicon.
- **What:** `deploy/Dockerfile` builds the Python bindings ([Polochon-street/bliss-python](https://github.com/Polochon-street/bliss-python), which has no tags; commit `c4bb63b4aaadbc199a4abd7adf23acbe8f6f771d` is its 0.2.0; the lockfile resolves the `bliss-audio` crate to 0.11.4) in a builder stage with Rust 1.100.0 and maturin 1.16.0, for baseline x86-64 (`RUSTFLAGS="-C target-cpu=x86-64"`). `deploy/bliss/Cargo.lock` pins every crate, since upstream commits no lockfile. The build drops the bindings' default `build-ffmpeg` feature, so the extension links dynamically to the image's own Debian FFmpeg libraries (installed with the `ffmpeg` package) instead of a static FFmpeg copy. Licences: bliss-rs and aubio (built in statically) are GPL-3.0, the bindings declare MIT, and Debian's FFmpeg is GPL-2.0-or-later: all fit mixsync's GPL-3.0.
- **Not in the lockfile:** `bliss-audio` is not a Python dependency of the project, because the PyPI wheel is the one that crashes, and there is no macOS or arm64 wheel. Only the image has bliss. Without it (development on macOS, CI) analysis still runs and stores BPM, key and energy, with a null similarity vector. `librosa` is an ordinary dependency.
- **Size:** the amd64 image grows from 261 MB to 419 MB (+157 MB, `docker image ls`; numpy, scipy, scikit-learn, numba/llvmlite and librosa make up nearly all of it, the bliss wheel is under 1 MB). That is under the ~250 MB growth this ADR set as the point to reconsider an optional image, and the maintainer accepted analysis in the main image whatever the size.
- **Speed:** about 2 s per 4-minute track (BPM, key, energy and the bliss vector), measured in the amd64 image under Docker's emulation on an Apple M5; librosa alone took about 3.2 s natively on the same machine. The image build also compiles bliss (about 5 minutes under emulation) and runs it once on a generated tone, so a broken build fails there.
