# bliss-audio's PyPI wheel needs AVX-512 (SIGILL on most home CPUs), so the similarity analyzer
# is built here for baseline x86-64 against Debian's FFmpeg (ADR 0025, amendment).
FROM python:3.13-slim AS bliss
# Polochon-street/bliss-python has no tags; this commit is its 0.2.0 (on the bliss-audio crate 0.11).
ARG BLISS_PYTHON_COMMIT=c4bb63b4aaadbc199a4abd7adf23acbe8f6f771d
ARG BLISS_PYTHON_SHA256=fea946f787dc6aab58efb4eaf880355245bd779f3240ea620777ac0e0ccfbdb1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl build-essential clang \
    libclang-dev pkg-config binutils libavcodec-dev libavdevice-dev libavfilter-dev libavformat-dev \
    libavutil-dev libswresample-dev libswscale-dev \
 && rm -rf /var/lib/apt/lists/*
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.100.0
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv
ENV PATH="/root/.cargo/bin:$PATH" RUSTFLAGS="-C target-cpu=x86-64"
WORKDIR /src
RUN curl -sSfL -o /tmp/src.tar.gz \
    "https://github.com/Polochon-street/bliss-python/archive/$BLISS_PYTHON_COMMIT.tar.gz" \
 && echo "$BLISS_PYTHON_SHA256  /tmp/src.tar.gz" | sha256sum -c - \
 && tar xzf /tmp/src.tar.gz --strip-components=1 && rm /tmp/src.tar.gz \
 && sed -i 's/^version = "0.1.10"$/version = "0.2.0"/' pyproject.toml  # stale; Cargo.toml has 0.2.0
# upstream commits no lockfile; ours pins every crate
COPY deploy/bliss/Cargo.lock ./
# No build-ffmpeg feature: link the final image's FFmpeg libraries, not a static copy. The wheel
# only fits this image, so it isn't repaired into a manylinux one.
RUN uv tool run maturin@1.16.0 build --release --locked --no-default-features \
    -F bliss-audio/default -F time/macros -i python3.13 --compatibility linux --auditwheel skip \
    -o /wheels
# the point of this build: fail if any AVX-512 (zmm register) instruction made it into the .so
RUN python -m zipfile -e /wheels/*.whl /tmp/whl \
 && ! objdump -d /tmp/whl/bliss_audio/*.so | grep -m1 zmm \
 && rm -r /tmp/whl

FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"

# Deps first so source edits don't bust this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --from=bliss /wheels /tmp/wheels
RUN uv pip install --no-deps /tmp/wheels/*.whl && rm -r /tmp/wheels
# fail the build, not every analysis, if the extension can't load or run
RUN ffmpeg -v error -f lavfi -i sine=d=3 /tmp/s.wav \
 && python -c "import bliss_audio as b; assert len(b.Song('/tmp/s.wav').analysis) > 20" \
 && rm /tmp/s.wav
COPY src ./src
# --inexact keeps bliss-audio, which the lockfile doesn't list
RUN uv sync --frozen --no-dev --inexact

# Compose overrides the uid/gid via `user:` (PUID/PGID); this is the fallback.
RUN useradd --uid 1000 --create-home mixsync \
 && mkdir /config /downloads /data && chown mixsync /config /downloads /data
USER mixsync

EXPOSE 8080
ENTRYPOINT ["mixsync"]
CMD ["web"]
