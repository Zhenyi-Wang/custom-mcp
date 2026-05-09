FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml .
RUN uv sync --no-dev

COPY src/ src/

EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
