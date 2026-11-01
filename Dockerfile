FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home weft
WORKDIR /home/weft

# Install Weft with server extras (leverages Docker layer cache)
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir ".[server]"

# Copy source
COPY weft/ weft/

# Create data directory
RUN mkdir -p /home/weft/.weft && chown -R weft:weft /home/weft

USER weft
EXPOSE 8765

# Default command
CMD ["python", "-m", "weft.server", "--host", "0.0.0.0", "--port", "8765"]
