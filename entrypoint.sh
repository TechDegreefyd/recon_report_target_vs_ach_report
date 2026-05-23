#!/bin/bash
set -e

# Inject Google OAuth token from env var (avoids browser auth on VPS)
if [ -n "$GOOGLE_TOKEN_JSON" ]; then
    echo "$GOOGLE_TOKEN_JSON" > /app/token.json
    echo "✅ token.json written from env"
fi

# Inject Google client secret from env var
if [ -n "$GOOGLE_CLIENT_SECRET_JSON" ]; then
    echo "$GOOGLE_CLIENT_SECRET_JSON" > /app/client_secret_804950201435-1i6i2gtvopf98ncqdk3902lkni02g6ss.apps.googleusercontent.com.json
    echo "✅ client_secret.json written from env"
fi

SCRIPT="${1:-both}"

case "$SCRIPT" in
    lms)
        echo "▶ Running LMS reports only..."
        python3 /app/generate_all_lms_reports.py
        ;;
    recon)
        echo "▶ Running Recon reports only..."
        python3 /app/generate_all_recon_reports.py
        ;;
    both|*)
        echo "▶ Running LMS reports..."
        python3 /app/generate_all_lms_reports.py
        echo ""
        echo "▶ Running Recon reports..."
        python3 /app/generate_all_recon_reports.py
        ;;
esac

echo ""
echo "✅ All done."
