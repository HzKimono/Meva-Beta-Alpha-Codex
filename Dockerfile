# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder
WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY constraints.txt ./constraints.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install --no-cache-dir --constraint constraints.txt .

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    STATE_DB_PATH=/data/btcbot_state.db

RUN useradd --create-home --uid 10001 btcbot
COPY --from=builder /opt/venv /opt/venv
COPY . /app
RUN mkdir -p /data && chown -R btcbot:btcbot /app /data
USER btcbot

ENTRYPOINT ["btcbot"]
CMD ["run", "--once"]
