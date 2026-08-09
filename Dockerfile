# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

# uv pineado: 'latest' haria que la imagen dejara de ser reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # La consola de Windows usa cp1252 y no puede imprimir 'Δ' ni '≥'. Con la salida
    # del contenedor forzada a UTF-8 el CLI se ve igual en cualquier sistema.
    PYTHONIOENCODING=utf-8 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    TZ=UTC

WORKDIR /app

# Capa de dependencias separada: cambiar codigo no reinstala todo el arbol.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Sin privilegios: los datos de glucosa son datos personales sensibles y el
# contenedor no tiene por que poder escribir fuera de lo suyo.
RUN useradd --create-home --uid 10001 glyco \
    && mkdir -p /app/data \
    && chown -R glyco:glyco /app
USER glyco

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "glycomind.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
