# Web (and optionally collector) image. Build: docker build -t aranet4-monitor .
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra postgres --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra postgres
RUN useradd --create-home --uid 1000 aranet && mkdir -p /app/data && chown aranet /app/data
USER aranet
EXPOSE 8050
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8050/healthz', timeout=2)" || exit 1
CMD ["aranet", "serve", "--host", "0.0.0.0", "--port", "8050"]
