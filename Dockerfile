FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

RUN python -m playwright install --with-deps chromium

COPY generate_all_lms_reports.py .
COPY generate_all_recon_reports.py .
COPY bhugoal_generate_report.py .
COPY bhugoal_daily_report_template.html .
COPY sheets_config.py .
COPY server.py .
COPY generate_outbound_report.py .
COPY generate_inbound_report.py .
COPY download_callinsight.py .
COPY tat_reports.py .
COPY generate_greeter_report.py .
COPY check_api_delay.py .
COPY generate_callback_reports.py .
COPY generate_ivr_extension_report.py .
COPY generate_admission_ledger_reports.py .
COPY generate_servicing_report.py .
COPY generate_marketing_hub_ingest.py .

# Meta Ad Library competitor digest (scraper + daily WhatsApp alert)
COPY META/scraper.py META/daily_ad_alert.py META/competitors.json ./META/

# seen_ads.json tracks which competitor ads have already been reported. Kept
# outside the image so a rebuild doesn't re-send the previous day's ads - mount
# a volume here to make it survive.
ENV AD_ALERT_STATE_FILE=/app/state/seen_ads.json
VOLUME ["/app/state"]

RUN mkdir -p \
    "state" \
    "Automation Cron Job/Target Report/local_fallback" \
    "Automation Cron Job/Recon Data" \
    "Automation Cron Job/Outbound Report" \
    "Automation Cron Job/Inbound Report" \
    "Automation Cron Job/Greeter Report" \
    "Automation Cron Job/Callback Report" \
    "Automation Cron Job/IVR Extension Report" \
    "Automation Cron Job/Admission Ledger" \
    "Automation Cron Job/Servicing Report" \
    "callinsight_downloads" \
    "greeter_downloads"

EXPOSE 8000

CMD ["python", "server.py"]
