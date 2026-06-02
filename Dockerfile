FROM python:3.12-slim

# Install system dependencies (gcc/libpq for asyncpg + chromium deps for Playwright)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc libpq-dev \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
        libgtk-3-0 libx11-xcb1 libxcb-dri3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (chromium only)
RUN playwright install chromium

# Copy application code
COPY generate_all_lms_reports.py .
COPY generate_all_recon_reports.py .
COPY sheets_config.py .
COPY server.py .

# Pre-create output directories so scripts don't fail on first run
RUN mkdir -p \
    "Automation Cron Job/Target Report/local_fallback" \
    "Automation Cron Job/Recon Data"

CMD ["python", "server.py"]
