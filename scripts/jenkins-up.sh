#!/usr/bin/env bash
# Start local Jenkins; generates an admin password on first run (kept in jenkins/.env, gitignored).
set -euo pipefail
cd "$(dirname "$0")/../jenkins"
if [ ! -f .env ]; then
  echo "JENKINS_ADMIN_PASSWORD=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)" > .env
  echo "generated jenkins/.env"
fi
docker compose up -d --build
echo "Jenkins: http://localhost:8080  (user admin, password in jenkins/.env)"
