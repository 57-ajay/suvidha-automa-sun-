#!/bin/bash
set -e

mkdir -p /tmp/vnc-tokens
touch /tmp/vnc-tokens/tokens.cfg

websockify \
    --web=/usr/share/novnc \
    --token-plugin=TokenFile \
    --token-source=/tmp/vnc-tokens/tokens.cfg \
    6080 \
    > /tmp/websockify.log 2>&1 &

WSPID=$!
sleep 1

if kill -0 $WSPID 2>/dev/null; then
    echo "websockify started (pid=$WSPID) on :6080"
else
    echo "ERROR: websockify crashed on startup. Log:"
    cat /tmp/websockify.log
    echo "---"
    echo "Falling back: starting without token plugin..."
    websockify --web=/usr/share/novnc 6080 localhost:5900 > /tmp/websockify.log 2>&1 &
fi

exec uv run src/main.py
