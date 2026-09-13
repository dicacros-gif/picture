#!/bin/bash
set -euo pipefail
/bin/zsh -n Install-Blog.command
/bin/zsh -n Install-Picture-Cleaner.command
INSTALLER_CHECK="${RUNNER_TEMP:-/tmp}/blog-installer-check.js"
node -e 'const fs = require("node:fs"); const text = fs.readFileSync("Install-Blog.command", "utf8"); process.stdout.write(text.split("<<\x27JXA\x27\n")[1].split("\nJXA")[0]);' > "$INSTALLER_CHECK"
# A nonexistent allowed path ensures this native bridge check cannot quit an app.
/usr/bin/osascript -l JavaScript "$INSTALLER_CHECK" com.dicacros.picturecleaner.mac /nonexistent-blog-installer-check/Blog.app
APP_PATH="dist/mac-arm64/Blog.app"
ENGINE_ROOT="$APP_PATH/Contents/Resources/blog-backend"
test -f "$ENGINE_ROOT/BlogEngine"
SIGNING_MODE="${SIGNING_MODE:-adhoc}"
if [ "$SIGNING_MODE" = developer-id ]; then
  bash scripts/developer-sign.sh
elif [ "$SIGNING_MODE" = adhoc ]; then
# Python extension libraries are resources, not nested .app bundles: sign them explicitly.
while IFS= read -r -d '' component; do
  if /usr/bin/file -b "$component" | /usr/bin/grep -q 'Mach-O'; then
    /usr/bin/codesign --force --sign - --timestamp=none --options runtime \
      --entitlements entitlements.mac.plist "$component"
  fi
done < <(/usr/bin/find "$ENGINE_ROOT" -type f -print0)
/usr/bin/codesign --force --deep --sign - --timestamp=none --options runtime \
  --entitlements entitlements.mac.plist "$APP_PATH"
else
  echo "Unknown signing mode: $SIGNING_MODE" >&2; exit 1
fi
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP_PATH"
/usr/bin/lipo "$APP_PATH/Contents/MacOS/Blog" -verify_arch arm64
/usr/bin/lipo "$ENGINE_ROOT/BlogEngine" -verify_arch arm64
"$ENGINE_ROOT/BlogEngine" --data-dir "${RUNNER_TEMP:-/tmp}/blog-signed-engine-smoke" self-test
BLOG_SMOKE_SCREENSHOT="$PWD/dist/Blog-M1-ui.png" "$APP_PATH/Contents/MacOS/Blog" --smoke-test
export SIGNING_MODE
python - <<'PY'
import json, os, platform
from pathlib import Path
mode = os.environ['SIGNING_MODE']
Path('dist/signing-verification.json').write_text(json.dumps({
    'architecture': platform.machine(), 'signing': mode,
    'apple_notarized': mode == 'developer-id', 'codesign_verified': True,
    'engine_self_test': True, 'gui_smoke_test': True,
    'version': json.loads(Path('package.json').read_text())['version']
}, indent=2))
PY
