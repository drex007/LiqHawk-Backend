# syntax=docker/dockerfile:1.7

# --- Stage 1: build dependencies into a venv with uv ---
FROM ghcr.io/astral-sh/uv:0.5.11 AS uv

FROM python:3.12-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install only the lockfile + project metadata first so this layer caches when
# only source code changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra ml

# Now copy source and finish the install (registers the project itself).
COPY app ./app
COPY contracts ./contracts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ml


# --- Stage 2: lean runtime image ---
FROM python:3.12-slim AS runtime

# Non-root user — keep the container from running as root.
RUN groupadd --system app && useradd --system --gid app --home /app --shell /sbin/nologin app

WORKDIR /app

# Copy the pre-built venv + source from the builder stage.
COPY --from=builder --chown=app:app /app /app

# Put the venv first on PATH so `python` and `uvicorn` resolve to it.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
