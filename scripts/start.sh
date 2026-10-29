#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python manage.py runserver "${ESTATE_BIND:-127.0.0.1:8000}" --noreload
