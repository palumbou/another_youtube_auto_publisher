#!/usr/bin/env bash
# Builds infra/build/deps-layer.zip with the pure-Python runtime dependencies of the
# Lambdas (jsonschema and its validators). Run before `tofu apply`.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
build="$root/infra/build/layer"
rm -rf "$build" && mkdir -p "$build/python" "$root/infra/build"
python3 -m pip install --quiet --target "$build/python" "jsonschema>=4.23" "rfc3339-validator>=0.1"
(cd "$build" && zip -qr "$root/infra/build/deps-layer.zip" python)
echo "wrote $root/infra/build/deps-layer.zip"
