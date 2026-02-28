FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen
COPY la_sweep_bot.py .
COPY web_app.py .
COPY static/ static/
CMD ["uv", "run", "--no-sync", "python", "la_sweep_bot.py"]
