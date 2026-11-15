# rxrbench's CI image: the apko base (rxrbench-ci.apko.yaml) and the tool's Python environment,
# built from the same lock files a job would install from, so a job starts with them in place:
#   /opt/venv         the tool's venv (requirements.txt, each collection's, and just)
#   /opt/collections  the Galaxy collections of requirements.yml
#   /opt/docs-venv    the doc tools' venv (docs/tools/requirements.txt), stamped as tools/docs.py stamps it
# The label rxrbench.requirements is ci/image/requirements-key of the files it was built from;
# a job uses the image only when its own key matches (.github/workflows/test.yml).
ARG BASE
FROM ${BASE}
ARG KEY
ARG JUST_VERSION=1.45.0

COPY src/ /src/

RUN set -eu; \
    uv venv --seed --python /usr/bin/python3.12 /opt/venv; \
    /opt/venv/bin/pip install -q --no-cache-dir -r /src/requirements.txt "rust-just==${JUST_VERSION}"; \
    for r in /src/collections/ansible_collections/*/*/requirements.txt; do \
      /opt/venv/bin/pip install -q --no-cache-dir -r "$r"; \
    done; \
    /opt/venv/bin/ansible-galaxy collection install -r /src/requirements.yml -p /opt/collections; \
    uv venv --seed --python /usr/bin/python3.12 /opt/docs-venv; \
    /opt/docs-venv/bin/pip install -q --no-cache-dir -r /src/docs/tools/requirements.txt; \
    sha256sum /src/docs/tools/requirements.txt | cut -d' ' -f1 > /opt/docs-venv/.lock-sha256; \
    git config --system --add safe.directory '*'; \
    rm -rf /src /root/.cache

LABEL org.opencontainers.image.title="rxrbench-ci" \
      org.opencontainers.image.description="rxrbench's CI environment: Wolfi (apko), Python 3.12 and the tool's pinned requirements" \
      org.opencontainers.image.source="https://github.com/roperscrossroads/rxrbench" \
      rxrbench.requirements="${KEY}"
