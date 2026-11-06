#!/bin/sh
# Writes deploy/kruai.env and deploy/postgres.env from the template, with a
# fresh JWT secret and database password. Refuses to overwrite either file.
# The generated values are never printed.
set -eu
cd "$(dirname "$0")"

for f in kruai.env postgres.env; do
  if [ -e "$f" ]; then
    echo "deploy/$f already exists; not overwriting it." >&2
    exit 1
  fi
done

umask 077
jwt=$(openssl rand -hex 32)
password=$(openssl rand -hex 24)

sed -e "s/__JWT_SECRET__/$jwt/" -e "s/__POSTGRES_PASSWORD__/$password/" kruai.env.example > kruai.env
printf 'POSTGRES_PASSWORD=%s\n' "$password" > postgres.env

echo "Wrote deploy/kruai.env and deploy/postgres.env (readable by you only)."
echo "Now fill in kruai.env: TELEGRAM_BOT_TOKEN, AZURE_SPEECH_KEY, TRIAL_TELEGRAM_IDS, ADMIN_TELEGRAM_IDS."
