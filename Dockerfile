# syntax=docker/dockerfile:1

# ---- stage 1: SPA dependencies (shared by build and test) ----
FROM node:20-alpine AS web-deps
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./

# ---- SPA production build ----
FROM web-deps AS web
RUN npm run build

# ---- frontend test target: docker build --target test-frontend ----
FROM web-deps AS test-frontend
RUN mkdir -p /app/test-reports
CMD ["npx", "vitest", "run", "--reporter=verbose", \
     "--reporter=junit", "--outputFile=/app/test-reports/frontend-results.xml"]

# ---- stage 2: runtime ----
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# ffmpeg brings ffprobe with it; sqlite3 is for poking at the DB inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --index-url https://pypi.org/simple/

COPY app/ ./app/
COPY run.py ./
COPY version.txt* ./
COPY --from=web /web/dist ./web/dist

ENV MMN_DATA_DIR=/app/data \
    MMN_HOST=0.0.0.0 \
    MMN_PORT=8000 \
    MMN_WEB_DIST=/app/web/dist

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"

CMD ["python", "run.py"]

# ---- backend test target: docker build --target test-backend ----
# Keeps ffmpeg, so the @pytest.mark.integration ffmpeg test actually runs here
# instead of being skipped.
FROM python:3.12-slim AS test-backend
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt --index-url https://pypi.org/simple/

COPY app/ ./app/
COPY tests/ ./tests/
RUN mkdir -p /app/test-reports

CMD ["python", "-m", "pytest", "tests/", "-v", \
     "--junitxml=/app/test-reports/backend-results.xml", "--junit-prefix=backend"]
