#!/bin/bash
set -e

# ── Environment ────────────────────────────────────────────────────
if [ -f /app/.env ]; then
  # overlayfs blocks rename() — use cat > to overwrite in-place
  # If read-only mount, just clean in memory via env vars
  if [ -w /app/.env ]; then
    sed 's/ *=[ ]*/=/g' /app/.env > /tmp/.env.clean && cat /tmp/.env.clean > /app/.env && rm -f /tmp/.env.clean
  fi
  set -a
  source /app/.env
  set +a
fi

# ── WireGuard tunnel ───────────────────────────────────────────────
if [ -n "$WG_PRIVATE_KEY" ] && [ -n "$WG_PEER_PUBLIC_KEY" ] && [ -n "$WG_PEER_ENDPOINT" ]; then
  echo "🔒 Bringing up WireGuard tunnel..."

  sed \
    -e "s|__WG_PRIVATE_KEY__|${WG_PRIVATE_KEY}|g" \
    -e "s|__WG_ADDRESS__|${WG_ADDRESS:-10.99.0.245/32}|g" \
    -e "s|__WG_PEER_PUBLIC_KEY__|${WG_PEER_PUBLIC_KEY}|g" \
    -e "s|__WG_PEER_ALLOWEDIPS__|${WG_PEER_ALLOWEDIPS:-0.0.0.0/0}|g" \
    -e "s|__WG_PEER_ENDPOINT__|${WG_PEER_ENDPOINT}|g" \
    -e "s|__WG_PEER_PERSISTENT_KEEPALIVE__|${WG_PEER_PERSISTENT_KEEPALIVE:-25}|g" \
    /etc/wireguard/wg0.conf.template > /etc/wireguard/wg0.conf

  wg-quick up wg0
  # Keep the docker bridge subnet on eth0: full-tunnel WireGuard (table
  # 51820) would otherwise route replies to the bridge gateway
  # (docker-proxy's local end for host-mapped ports) via wg0 and drop them.
  BRIDGE_NET=$(ip -4 route show table main | awk '/scope link/ {print $1; exit}')
  [ -n "$BRIDGE_NET" ] && ip route replace ${BRIDGE_NET} dev eth0 table 51820 2>/dev/null || true

  echo "✅ WireGuard tunnel established"
  wg show
else
  echo "⚠️  WireGuard env vars not set — skipping tunnel"
fi

# ── Start tunester ─────────────────────────────────────────────────
echo "🚀 Starting tunester..."
exec python3 app.py "$@"
