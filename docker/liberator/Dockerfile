# Container image for the bundled `liberator-trading-api` upstream.
#
# liberator-trading-api is vendored as a git submodule under
# `third_party/liberator-trading-api/` and ships only STUB Dockerfile/compose
# files (1 byte each), so the execution-engine owns this build. This image is an
# INTERNAL implementation detail of the execution plane — it is never published
# with a host port and only `quant-execution-engine` may reach it (D9).
#
# Build context is the submodule dir; see docker-compose.liberator.yml.
# liberator-trading-api requires Python >= 3.13.
FROM python:3.13-slim

# uv for locked dependency install (the project carries pyproject + uv.lock).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY . /app

# Install the locked runtime deps (no dev group) into an in-project venv.
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Internal port — liberator defaults to 8200 (config/system.yaml api.port).
EXPOSE 8200

# `python main.py` boots app.main; it self-generates missing config via setup,
# but we mount our own config/system.yaml (Redis host = sidecar) to be deterministic.
CMD ["python", "main.py"]
