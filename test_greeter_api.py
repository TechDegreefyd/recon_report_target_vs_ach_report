"""
Temp script — test if greeter export API works via session login.
Usage: python test_greeter_api.py
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

USERNAME = os.getenv('GREETER_USERNAME')
PASSWORD = os.getenv('GREETER_PASSWORD')
LOGIN_URL  = 'https://greeter.co.in/login'
EXPORT_URL = 'https://greeter.co.in/export_call_log_data/xlsx'

if not USERNAME or not PASSWORD:
    raise SystemExit('Set GREETER_USERNAME and GREETER_PASSWORD env vars first.')

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

# Step 1: GET login page to grab CSRF token
print('Fetching login page …')
r = session.get(LOGIN_URL, timeout=30)
print(f'  Status: {r.status_code}')

# Try to extract CSRF token from HTML
csrf = None
for line in r.text.splitlines():
    if 'csrf' in line.lower() and 'value' in line.lower():
        import re
        m = re.search(r'value=["\']([^"\']{20,})["\']', line)
        if m:
            csrf = m.group(1)
            print(f'  CSRF token: {csrf[:20]}…')
            break

# Step 2: POST login
print('Logging in …')
payload = {'username': USERNAME, 'password': PASSWORD}
if csrf:
    payload['csrfmiddlewaretoken'] = csrf
    session.headers.update({'Referer': LOGIN_URL})

r = session.post(LOGIN_URL, data=payload, timeout=30, allow_redirects=True)
print(f'  Status: {r.status_code}  URL: {r.url}')

if 'login' in r.url.lower():
    raise SystemExit('Login failed — still on login page.')

print('Logged in successfully.')

# Step 3: Hit the export endpoint via POST (405 on GET)
import datetime
today = datetime.date.today().strftime('%Y-%m-%d')

payload = {
    'start_date': today,
    'end_date':   today,
}
print(f'  POST payload: {payload}')

# grab a fresh CSRF for the export form
r2 = session.get('https://greeter.co.in/reseller/call_log', timeout=30)
csrf2 = None
import re
for line in r2.text.splitlines():
    if 'csrf' in line.lower() and 'value' in line.lower():
        m = re.search(r'value=["\']([^"\']{20,})["\']', line)
        if m:
            csrf2 = m.group(1)
            break
if csrf2:
    payload['csrfmiddlewaretoken'] = csrf2
    session.headers.update({'Referer': 'https://greeter.co.in/reseller/call_log'})
    print(f'  CSRF for export: {csrf2[:20]}…')

print(f'POSTing {EXPORT_URL} …')
r = session.post(EXPORT_URL, data=payload, timeout=60, stream=True)
print(f'  Status: {r.status_code}')
print(f'  Content-Type: {r.headers.get("Content-Type")}')
print(f'  Content-Disposition: {r.headers.get("Content-Disposition")}')

ct = r.headers.get('Content-Type', '')
if r.status_code == 200 and ('spreadsheet' in ct or 'octet-stream' in ct or 'excel' in ct):
    out = 'greeter_test_export.xlsx'
    with open(out, 'wb') as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    print(f'  Saved → {out}')
else:
    print(f'  Response (first 800 chars):\n{r.text[:800]}')
