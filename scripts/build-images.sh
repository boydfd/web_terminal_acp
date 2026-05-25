#!/usr/bin/env sh
set -eu

docker compose --profile build-base build backend-base
docker compose build backend frontend
