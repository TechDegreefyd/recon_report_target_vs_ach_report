FROM python:3.12-slim

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY generate_all_lms_reports.py .
COPY generate_all_recon_reports.py .
COPY sheets_config.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Pre-create output directories so scripts don't fail on first run
RUN mkdir -p \
    "Automation Cron Job/Target Report/local_fallback" \
    "Automation Cron Job/Recon Data"

# Default: run both reports
CMD ["/entrypoint.sh", "both"]
