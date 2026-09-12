#!/bin/bash
set -euo pipefail
test "$(uname -m)" = arm64
python -m PyInstaller --noconfirm --clean --onedir --name BlogEngine \
  --distpath backend-dist --workpath build/backend --specpath build \
  --paths ../windows --paths backend --target-arch arm64 \
  --hidden-import AppKit --hidden-import Foundation backend/engine.py
file backend-dist/BlogEngine/BlogEngine
lipo -verify_arch arm64 backend-dist/BlogEngine/BlogEngine
backend-dist/BlogEngine/BlogEngine --data-dir "${RUNNER_TEMP:-/tmp}/blog-engine-smoke" self-test
