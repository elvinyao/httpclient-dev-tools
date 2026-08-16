#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="python:3.9-bookworm"

docker run --rm -i --init \
  -e PATH="/workspace/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  -v "${PROJECT_ROOT}:/workspace" \
  -w /workspace \
  "${IMAGE}" \
  "$@"
