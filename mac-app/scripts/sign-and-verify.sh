#!/bin/bash
set -euo pipefail
APP_PATH="dist/mac-arm64/Blog.app"
ENGINE_ROOT="$APP_PATH/Contents/Resources/blog-backend"
test -f "$ENGINE_ROOT/BlogEngine"
# Python extension libraries are resources, not nested .app bundles: sign them explicitly.
while IFS= read -r -d '' component; do
  if /usr/bin/file -b "$component" | /usr/bin/grep -q 'Mach-O'; then
    /usr/bin/codesign --force --sign - --timestamp=none --options runtime \
      --entitlements entitlements.mac.plist "$component"
  fi
done < <(/usr/bin/find "$ENGINE_ROOT" -type f -print0)
/usr/bin/codesign --force --deep --sign - --timestamp=none --options runtime \
  --entitlements entitlements.mac.plist "$APP_PATH"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP_PATH"
/usr/bin/lipo -verify_arch arm64 "$APP_PATH/Contents/MacOS/Blog"
/usr/bin/lipo -verify_arch arm64 "$ENGINE_ROOT/BlogEngine"
"$ENGINE_ROOT/BlogEngine" --data-dir "${RUNNER_TEMP:-/tmp}/blog-signed-engine-smoke" self-test
"$APP_PATH/Contents/MacOS/Blog" --smoke-test
