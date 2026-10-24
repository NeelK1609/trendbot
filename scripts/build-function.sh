#!/usr/bin/env bash
# Assembles build/function: the Functions entry point, the trendbot package and pinned requirements.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build/function && mkdir -p build/function
cp azure/function/function_app.py azure/function/host.json build/function/
cp -R src/trendbot build/function/trendbot
find build/function -name __pycache__ -type d -prune -exec rm -rf {} +
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt > build/function/requirements.txt
echo "azure-functions" >> build/function/requirements.txt
(cd build/function && zip -qr ../function.zip .)
echo "Built build/function.zip"
