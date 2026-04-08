#!/usr/bin/env bash
# Systemd için bitcoind wrapper — ön planda çalışır (Type=simple)
# bitcoin.conf'taki daemon=1 ayarını -nodaemon ile override eder.

DATADIR="$HOME/snap/bitcoin-core/common/.bitcoin"
CONF="$DATADIR/bitcoin.conf"
PIDFILE="$DATADIR/testnet4/bitcoind.pid"

# Stale PID dosyası varsa ve process ölüyse temizle
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && ! kill -0 "$OLD_PID" 2>/dev/null; then
        rm -f "$PIDFILE"
    fi
fi

exec /snap/bin/bitcoin-core.daemon \
    -testnet4 \
    -nodaemon \
    -conf="$CONF" \
    -datadir="$DATADIR"
