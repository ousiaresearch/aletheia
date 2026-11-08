FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home aletheia
WORKDIR /home/aletheia

# Copy source first so pip can install the local package
COPY aletheia/ aletheia/
COPY pyproject.toml README.md ./

# Install Aletheia with server extras; source changes invalidate this layer.
RUN pip install --no-cache-dir ".[server]"

# Create data directory
RUN mkdir -p /home/aletheia/.aletheia && chown -R aletheia:aletheia /home/aletheia

USER aletheia
EXPOSE 8765

# Default command
CMD ["python", "-m", "aletheia.server", "--host", "0.0.0.0", "--port", "8765"]
