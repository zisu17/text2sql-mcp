FROM public.ecr.aws/docker/library/python:3.12-slim
# uv binary pinned as the uv Docker guide recommends. Bump together with the CI uv version.
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Dependencies come only from uv.lock; --locked fails the build if the lock is stale.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable --no-install-project --no-cache
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable --no-cache \
    && useradd --system --create-home appuser
USER appuser

CMD ["sh", "-c", "exec uvicorn text2sql_mcp.http_app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*' "]
