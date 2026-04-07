#!/bin/bash
set -e

MT5_HOST="${MT5_HOST:-mt5}"
MT5_PORT="${MT5_PORT:-18812}"

echo "════════════════════════════════════════════════════════"
echo "  Zone Bot — connecting to MT5 RPyC server"
echo "  Target: ${MT5_HOST}:${MT5_PORT}"
echo "════════════════════════════════════════════════════════"

RETRIES=0
until nc -z "${MT5_HOST}" "${MT5_PORT}" 2>/dev/null; do
    RETRIES=$((RETRIES + 1))
    if [ "$RETRIES" -ge 60 ]; then
        echo "  ❌ Gave up waiting for MT5 after 5 minutes."
        exit 1
    fi
    echo "  ⏳ MT5 not ready yet — retrying in 5s... (${RETRIES}/60)"
    sleep 5
done

echo ""
echo "  ✅ MT5 RPyC server is up!"
echo ""
echo "  ⚠️  One-time setup (first run only):"
echo "  1. Open http://localhost:3000 in your browser"
echo "  2. Log in to your broker account inside MT5"
echo "  3. Tools → Options → Expert Advisors → Allow Algorithmic Trading"
echo "  4. Run: docker exec zone_bot touch /app/ready_to_trade"
echo ""

while [ ! -f /app/ready_to_trade ]; do
    sleep 5
done

echo "  🚀 Starting bot..."
cd /app && exec python3 -m src.main