#!/bin/bash
set -eux

# Install Docker
apt update
apt install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin redis-tools

# Authenticate Docker to Artifact Registry using VM's service account
gcloud auth configure-docker asia-south1-docker.pkg.dev --quiet

# Fetch config from instance metadata (passed by the MIG template)
REDIS_HOST=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-host)
REDIS_AUTH=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-auth)
DOMAIN=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/domain)
API_URL=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/api-url)
MAX_SLOTS=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/max-slots)


REDIS_AUTH_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$REDIS_AUTH")
REDIS_URL="redis://default:${REDIS_AUTH_ENC}@${REDIS_HOST}:6379"

# Write compose + env
mkdir -p /opt/worker
cat > /opt/worker/docker-compose.yaml <<EOF
services:
  worker:
    image: asia-south1-docker.pkg.dev/cabswale-ai/automation-agent-images/worker:v2
    environment:
      - REDIS_URL=\${REDIS_URL}
      - API_URL=\${API_URL}
      - DOMAIN=\${DOMAIN}
      - MAX_SLOTS=\${MAX_SLOTS}
    shm_size: "4gb"
    network_mode: "host"
    restart: unless-stopped
EOF

cat > /opt/worker/.env <<EOF
REDIS_URL=$REDIS_URL
DOMAIN=$DOMAIN
API_URL=$API_URL
MAX_SLOTS=$MAX_SLOTS
EOF

# Start
cd /opt/worker
docker compose pull
docker compose up -d
