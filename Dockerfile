FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml ./
COPY circus/ ./circus/
COPY circus_sdk/ ./circus_sdk/

# Install Python dependencies
RUN pip install --no-cache-dir -e .[embedding]

# Create data directory
RUN mkdir -p /data

# Expose port
EXPOSE 6200

# Set environment variable for database path
ENV CIRCUS_DATABASE_PATH=/data/circus.db

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:6200/health')"

# Run the application
CMD ["uvicorn", "circus.app:app", "--host", "0.0.0.0", "--port", "6200"]
