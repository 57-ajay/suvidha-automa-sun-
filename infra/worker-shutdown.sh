#!/bin/bash
# Runs when GCE is about to terminate the VM.
# Flips the drain flag in Redis, waits for running slots to clear,
# then exits cleanly so MIG can delete us.

set -eu

echo "[shutdown] starting drain protocol"

# Hostname from metadata
HOSTNAME=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/name)

# Redis config from metadata
REDIS_HOST=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-host)
REDIS_AUTH=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-auth)

# redis-cli should be in the worker image; install on host as fallback
if ! command -v redis-cli >/dev/null 2>&1; then
    apt-get install -y redis-tools || true
fi

# Flip drain flag (24h TTL — self-cleaning)
redis-cli -h "$REDIS_HOST" -a "$REDIS_AUTH" --no-auth-warning \
    SET "vm:${HOSTNAME}:draining" 1 EX 86400 || true

echo "[shutdown] drain flag set for $HOSTNAME"

# Poll: wait up to 14 minutes for all slots on this VM to clear.
# MIG terminationTimeout is 15 min; leave 1 min of margin for compose down.
DEADLINE=$(( $(date +%s) + 14*60 ))

while true; do
    now=$(date +%s)
    if [ $now -ge $DEADLINE ]; then
        echo "[shutdown] drain timeout reached — forcing exit"
        break
    fi

    # Count active jobs claimed by this hostname from Redis
    ACTIVE=$(redis-cli -h "$REDIS_HOST" -a "$REDIS_AUTH" --no-auth-warning \
        --scan --pattern "job:*" 2>/dev/null | while read key; do
            vm=$(redis-cli -h "$REDIS_HOST" -a "$REDIS_AUTH" --no-auth-warning \
                HGET "$key" vmHostname 2>/dev/null)
            status=$(redis-cli -h "$REDIS_HOST" -a "$REDIS_AUTH" --no-auth-warning \
                HGET "$key" status 2>/dev/null)
            if [ "$vm" = "$HOSTNAME" ] && { [ "$status" = "running" ] || [ "$status" = "waiting_for_human" ]; }; then
                echo 1
            fi
        done | wc -l)

    echo "[shutdown] active jobs on $HOSTNAME: $ACTIVE"

    if [ "$ACTIVE" = "0" ]; then
        echo "[shutdown] all slots clear, exiting"
        break
    fi

    sleep 10
done

# Stop the container cleanly
cd /opt/worker
docker compose down --timeout 30 || true

echo "[shutdown] drain complete"
