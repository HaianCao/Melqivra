FROM python:3.12-slim

ARG UV_VERSION=0.13.6

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY scripts ./scripts

RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv sync --locked --no-editable --no-cache \
    && pip uninstall --yes uv

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

CMD ["melqivra", "serve", "--host", "0.0.0.0", "--port", "8000"]

