FROM python:3.12-slim

WORKDIR /app

# DuckDB and pyarrow need no system build tools at runtime.
COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web

RUN pip install --no-cache-dir -e .

ENV PYTHONPATH=/app/src
EXPOSE 8077

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8077/health', timeout=4)"

CMD ["uvicorn", "dbi.api.main:app", "--host", "0.0.0.0", "--port", "8077"]
