# ADR 0002: Python backend

- **Status:** Accepted
- **Date:** 2026-10-09

## Context
The workload is I/O-bound: HTTP calls to slskd, MusicBrainz, AcoustID, and Navidrome, plus file copies. Raw CPU speed is irrelevant. What matters, in order:

1. **Reusable music libraries** for tagging, fingerprinting, and especially matching. Bad matching is the main complaint about soulsync.
2. **Correctness and safety** of file operations.
3. **Development speed** for a solo project.
4. **Deploy footprint.**

## Decision
**Python 3.13**, with FastAPI, pydantic v2, SQLAlchemy 2, and httpx. Type safety comes from pyright in strict mode, pydantic validation at every external boundary, and the regression and crash-safety suites.

## Alternatives considered
| | Tag read/write | Fingerprint / MB | Matching prior art | yt-dlp | Safety | Dev speed | Footprint |
|---|---|---|---|---|---|---|---|
| **Python** | mutagen (best in class; used by Picard and beets) | pyacoustid, musicbrainzngs | **beets and Picard are both Python** | native library | medium (needs pyright strict) | fastest | ~150 MB image |
| **Rust** | lofty (good, many formats) | rusty-chromaprint (pure Rust), musicbrainz_rs | none, write from scratch | subprocess | **highest** | slowest | ~20 MB binary |
| **Go** | go-taglib (WASM TagLib), dhowden/tag (read-only) | fpcalc subprocess, thin MB client | none (Navidrome only reads tags) | subprocess | high | fast | ~20 MB binary |
| **TypeScript** | node-taglib-sharp; music-metadata (read-only) | thin wrappers | none | subprocess | medium | fast | ~150 MB |
| **C#/.NET** | TagLib# (excellent) | MetaBrainz.MusicBrainz | Lidarr (reference only) | subprocess | high | medium | ~200 MB |

### Python
- **Pros:** the strongest reuse. The best available matching code (beets' distance scoring, Picard) can be embedded or read directly. mutagen is the reference tag library. yt-dlp is a native library with progress hooks.
- **Cons:** type errors show up at runtime unless the tooling is strict; the image is larger.

### Rust
- **Pros:** the type system can enforce file-operation state machines (for example, a file can't be renamed into the library until it has been verified), which is the best fit for data resiliency. Tiny binary, low RAM.
- **Cons:** the slowest iteration (compile times, async complexity), the matcher has to be written from scratch, and yt-dlp is a subprocess. Estimated 1.5–2× longer to reach v1.

### Go
- **Pros:** simple language, easy to read, single binary, the same language as Navidrome.
- **Cons:** most of the music logic gets rebuilt; fpcalc and yt-dlp are subprocesses; the tag-writing libraries are less mature.

### TypeScript
- **Pros:** one language across the stack.
- **Cons:** weak tag writing, no matching prior art. Not a good fit for this domain.

### C#/.NET
- **Pros:** excellent TagLib#; Lidarr is available as a reference.
- **Cons:** a heavy runtime. Lidarr's codebase is the kind of bloat this project is trying to avoid.

### Hybrid (Rust/Go core + Python matcher sidecar)
- **Cons:** two languages, two toolchains, and an internal RPC layer in a solo project. That is the complexity the guidelines reject.

## Consequences
- pyright strict and pydantic are mandatory, not optional.
- The beets spike at the start of phase 2 decides between embedding `beets.autotag` and porting its algorithm (MIT license). Decided in [ADR 0006](0006-port-beets-autotag.md): port it.
- If the footprint ever matters (for example, moving to a NAS), the adapter boundaries make it possible to replace hot paths later.
