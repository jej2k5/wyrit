FROM node:26-bookworm-slim AS node
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap libatomic1 libseccomp2 libstdc++6 util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 wyrit
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN node --permission --no-allow-net -e ''

WORKDIR /opt/wyrit
COPY pyproject.toml ./
COPY src/ src/
RUN python3 -m pip install --no-cache-dir .
COPY bench/ bench/
COPY tests/ tests/
COPY scripts/verify-linux.sh scripts/verify-linux.sh

USER wyrit
CMD ["scripts/verify-linux.sh"]
