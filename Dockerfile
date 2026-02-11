FROM python:3.11-slim

LABEL maintainer="NetBox IPMI Agent Team"
LABEL description="NetBox IPMI Move Auditor - monitors IPMI connections against NetBox cabling"

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create non-root user
RUN groupadd -r agent && useradd -r -g agent agent

# Set work directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY pyproject.toml .

# Install the application
RUN pip install --no-cache-dir -e .

# Create data directory for SQLite
RUN mkdir -p /data && chown agent:agent /data
VOLUME /data

# Set default environment
ENV STATE_DB_PATH=/data/state.db

# Switch to non-root user
USER agent

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Run the agent
ENTRYPOINT ["netbox-ipmi-agent"]
