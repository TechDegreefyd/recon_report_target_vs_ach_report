FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable stdout logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install only build dependencies (Playwright --with-deps handles browser libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for Docker layer caching
COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install Playwright browser binaries and required dependencies
RUN python -m playwright install --with-deps chromium

# Copy application source files
COPY generate_all_lms_reports.py .
COPY generate_all_recon_reports.py .
COPY bhugoal_generate_report.py .
COPY bhugoal_daily_report_template.html .
COPY sheets_config.py .
COPY server.py .
COPY generate_outbound_report.py .
COPY generate_inbound_report.py .

# Create required directories
RUN mkdir -p \
    "Automation Cron Job/Target Report/local_fallback" \
    "Automation Cron Job/Recon Data" \
    "Automation Cron Job/Outbound Report" \
    "Automation Cron Job/Inbound Report" \
    "callinsight_downloads"

EXPOSE 8000

CMD ["python", "server.py"]