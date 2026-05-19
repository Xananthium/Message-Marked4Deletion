FROM python:3.12-slim AS builder

# Install system dependencies required for building Python packages
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS runtime

# Install runtime system dependencies
RUN apt-get update && apt-get install -y \
    rsync \
    openssh-client \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Python dependencies from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY poller.py .
COPY schema.sql .
COPY init-db.sh .
COPY tasks/ ./tasks/
COPY agentic/ ./agentic/  # if needed

# Create a non-root user
RUN useradd -m -u 1000 -s /bin/bash appuser
USER appuser

# Environment variables (will be overridden by Cloud Run)
ENV AIB_DSN=""
ENV AIB_SA_PATH=""
ENV AIB_MAILBOX=""
ENV AIB_OPERATOR_EMAIL=""
ENV AIB_SSH_ALIAS=""
ENV AIB_MODEL=""
ENV AIB_TMP_ROOT="/tmp/aib"

# Ensure tmp directory exists
RUN mkdir -p /tmp/aib && chown appuser:appuser /tmp/aib

# Set entrypoint
ENTRYPOINT ["python", "-m", "poller"]