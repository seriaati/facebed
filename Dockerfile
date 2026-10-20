# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=0

# /facebed rather than /app: deployments mount ./config and cookies.json here
WORKDIR /facebed
# dependencies get their own layer so source edits don't reinstall them
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
COPY . /facebed
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked


# must match the builder's python, the venv points at its interpreter path
FROM python:3.14-slim-trixie

# uid 1000 matches the previous alpine image's user, so existing mounts keep working
RUN groupadd --system --gid 1000 facebed \
 && useradd --system --gid 1000 --uid 1000 --create-home facebed

COPY --from=builder --chown=facebed:facebed /facebed /facebed

ENV PATH="/facebed/.venv/bin:$PATH" PYTHONUNBUFFERED=1

USER facebed
WORKDIR /facebed
EXPOSE 9812
CMD ["python", "-m", "facebed", "-c", "./config/config.yaml"]
