FROM python:3.12-slim

WORKDIR /app

# System deps (httpx needs ca-certificates for TLS, that's about it)
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY *.py ./

# Fly.io sets PORT env var. Default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
