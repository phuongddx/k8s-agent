# linux/amd64 only: scripts/install-kustomize.sh downloads the amd64 kustomize.
FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/install-kustomize.sh /tmp/install-kustomize.sh
RUN /tmp/install-kustomize.sh /usr/local/bin
RUN pip install --no-cache-dir uv==0.13.7
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin agent \
    && mkdir -p /work \
    && chown agent:agent /work
COPY --from=build /usr/local/bin/kustomize /usr/local/bin/kustomize
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 REPO_DIR=/work/repo
USER 10001
WORKDIR /work
EXPOSE 8080
ENTRYPOINT ["k8s-agent"]
