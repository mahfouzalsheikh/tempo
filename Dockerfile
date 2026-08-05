FROM node:22-bookworm-slim AS codex
RUN npm install --global @openai/codex

FROM docker:27-cli AS docker-cli

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIPENV_NOSPIN=1 \
    TEMPO_WORKFLOW_PATH=/app/WORKFLOW.md

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git openssh-client \
    && pip install --no-cache-dir pipenv \
    && rm -rf /var/lib/apt/lists/*

ENV TEMPO_WORKSPACE_ROOT=/data/workspaces \
    TEMPO_DATABASE_PATH=/data/database/tempo.sqlite3 \
    HOME=/home/tempo \
    CODEX_HOME=/home/tempo/.codex

COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack

WORKDIR /app
COPY Pipfile Pipfile.lock pyproject.toml README.md ./
COPY tempo ./tempo
COPY tempo_web ./tempo_web
COPY manage.py WORKFLOW.md ./
COPY docker-entrypoint.sh /usr/local/bin/tempo-entrypoint
RUN pipenv install --system --deploy

RUN useradd --create-home --uid 10001 tempo \
    && mkdir -p /data/workspaces /data/database /data/log /home/tempo/.codex \
    && chown -R tempo:tempo /data /home/tempo \
    && chmod 0755 /usr/local/bin/tempo-entrypoint

EXPOSE 8000
VOLUME ["/data/workspaces", "/data/database", "/home/tempo/.codex"]
HEALTHCHECK --interval=20s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

ENTRYPOINT ["/usr/local/bin/tempo-entrypoint"]
CMD ["--host", "0.0.0.0", "--port", "8000", "/app/WORKFLOW.md"]
