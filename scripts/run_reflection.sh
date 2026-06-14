#!/bin/bash
export PYTHONPATH=/opt/hermes-trading-bot
# Read env file and export each key=value
while IFS="=" read -r key val; do
    if [ -n "$key" ] && [ "${key:0:1}" != "#" ]; then
        export "$key=$val"
    fi
done < /opt/hermes-trading-bot/.env
exec /opt/hermes-trading-bot/.venv/bin/python3 /opt/hermes-trading-bot/scripts/daily_reflection.py /opt/hermes-trading-bot/data/hermes.db
