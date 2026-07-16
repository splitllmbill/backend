FROM python:3.12-slim

# - PYTHONUNBUFFERED: stream logs straight to stdout (container log capture)
# - PYTHONDONTWRITEBYTECODE: no .pyc files in the image
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application package (keeps the image small; no tests/.env).
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser
USER appuser

# Honour an injected $PORT if the host sets one; default to 8080 for a plain
# `docker run` (docker-compose overrides this to 8081 for local dev).
ENV PORT=8080
EXPOSE 8080

# `exec` makes uvicorn PID 1 so it receives SIGTERM for graceful shutdown.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
