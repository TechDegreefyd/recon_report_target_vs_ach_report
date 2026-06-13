# Microsoft's official Playwright image — ships with Chrome, Chromium, Firefox, WebKit
# and all system dependencies pre-installed. No manual apt installs needed.
FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

# Prevent Python from writing .pyc files and enable stdout logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source files
COPY generate_all_lms_reports.py .
COPY generate_all_recon_reports.py .
COPY bhugoal_generate_report.py .
COPY bhugoal_daily_report_template.html .
COPY sheets_config.py .
COPY server.py .
COPY generate_outbound_report.py .
COPY generate_inbound_report.py .
COPY download_callinsight.py .

# Create required directories
RUN mkdir -p \
    "Automation Cron Job/Target Report/local_fallback" \
    "Automation Cron Job/Recon Data" \
    "Automation Cron Job/Outbound Report" \
    "Automation Cron Job/Inbound Report" \
    "callinsight_downloads"

EXPOSE 8000

CMD ["python", "server.py"]