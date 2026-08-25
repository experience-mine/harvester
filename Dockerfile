# Образ сканирования проекта: агент сбора знаний и tldr для анализа кода.
#
# Сборка:  docker build -t project-snapshot .
# Запуск:  docker run --rm -v /путь/к/проекту:/project:ro -v $PWD/out:/out project-snapshot

FROM debian:12-slim AS tldr

ARG TLDR_VERSION=0.4.0
ARG TLDR_ASSET=tldr-cli-x86_64-unknown-linux-gnu.tar.xz
ARG TLDR_BASE=https://github.com/parcadei/tldr-code/releases/download

# Бинарь берётся из релиза и сверяется с контрольной суммой из того же релиза:
# сборка из исходников потребовала бы тулчейна Rust и минут ожидания.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils; \
    rm -rf /var/lib/apt/lists/*; \
    cd /tmp; \
    curl -fsSLO "${TLDR_BASE}/v${TLDR_VERSION}/${TLDR_ASSET}"; \
    curl -fsSLO "${TLDR_BASE}/v${TLDR_VERSION}/${TLDR_ASSET}.sha256"; \
    sha256sum -c "${TLDR_ASSET}.sha256"; \
    tar -xJf "${TLDR_ASSET}"; \
    install -m 0755 "$(find /tmp -type f -name tldr -perm -u+x | head -n 1)" /usr/local/bin/tldr; \
    /usr/local/bin/tldr --version

FROM python:3.12-slim

LABEL org.opencontainers.image.title="project-snapshot" \
      org.opencontainers.image.description="Слепок проекта: сбор состояния репозитория и анализ кода tldr" \
      org.opencontainers.image.licenses="MIT AND AGPL-3.0-only"

# git нужен анализатору истории; ca-certificates — для сетевых обращений агента.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates; \
    rm -rf /var/lib/apt/lists/*

COPY --from=tldr /usr/local/bin/tldr /usr/local/bin/tldr

WORKDIR /opt/agent
COPY pyproject.toml ./
COPY agent ./agent
RUN pip install --no-cache-dir .

COPY docker/scan.sh /usr/local/bin/scan
RUN chmod 0755 /usr/local/bin/scan

# Анализируемый проект монтируется только на чтение, результаты пишутся в /out.
VOLUME ["/project", "/out"]
WORKDIR /work
ENV SCAN_PROJECT=/project SCAN_OUT=/out

ENTRYPOINT ["/usr/local/bin/scan"]
CMD []
