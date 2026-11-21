FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir . && useradd --system --create-home appuser
USER appuser

CMD ["sh", "-c", "exec uvicorn text2sql_mcp.http_app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*' "]
