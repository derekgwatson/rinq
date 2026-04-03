#!/bin/bash
# Rinq deploy script — called by GitHub Actions on push to main
set -e

cd /var/www/rinq

echo "=== Pulling latest code ==="
git fetch origin main
git reset --hard origin/main

echo "=== Installing dependencies ==="
source venv/bin/activate
pip install -q -r requirements.txt

echo "=== Restarting Rinq ==="
sudo systemctl restart rinq

echo "=== Waiting for health check ==="
sleep 2
HEALTH=$(curl -s --unix-socket /var/www/rinq/rinq.sock http://localhost/health 2>/dev/null || echo '{"status":"error"}')
echo "$HEALTH"

if echo "$HEALTH" | grep -q '"healthy"'; then
    echo "=== Deploy successful ==="
else
    echo "=== WARNING: Health check failed ==="
    exit 1
fi

