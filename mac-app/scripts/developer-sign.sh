#!/bin/bash
# Invoked only for an explicitly selected Developer ID release. Never downgrade.
set -euo pipefail
for name in MAC_CERTIFICATE_BASE64 MAC_CERTIFICATE_PASSWORD MAC_SIGN_IDENTITY APPLE_ID APPLE_APP_PASSWORD APPLE_TEAM_ID; do
  if [ -z "${!name:-}" ]; then echo "Missing signing secret: $name" >&2; exit 1; fi
done
SIGN_ROOT="$(mktemp -d "${RUNNER_TEMP:-/tmp}/blog-sign.XXXXXX")"
KEYCHAIN="$SIGN_ROOT/signing.keychain-db"
KEYCHAIN_PASSWORD="$(openssl rand -hex 32)"
cleanup() {
  security delete-keychain "$KEYCHAIN" >/dev/null 2>&1 || true
  # mktemp creates this exact owned directory; never delete a user keychain.
  rm -f "$SIGN_ROOT/certificate.p12"
  rmdir "$SIGN_ROOT" 2>/dev/null || true
}
trap cleanup EXIT
export SIGN_ROOT
python - <<'PY'
import base64, os
from pathlib import Path
p = Path(os.environ['SIGN_ROOT']) / 'certificate.p12'
p.write_bytes(base64.b64decode(os.environ['MAC_CERTIFICATE_BASE64'], validate=True))
p.chmod(0o600)
PY
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security set-keychain-settings -lut 7200 "$KEYCHAIN"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security import "$SIGN_ROOT/certificate.p12" -k "$KEYCHAIN" -P "$MAC_CERTIFICATE_PASSWORD" -T /usr/bin/codesign >/dev/null
security set-key-partition-list -S apple-tool:,apple: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
APP_PATH="dist/mac-arm64/Blog.app"
# Sign nested Mach-O code and bundles inside out, with secure timestamps.
while IFS= read -r -d '' component; do
  if /usr/bin/file -b "$component" | /usr/bin/grep -q 'Mach-O'; then
    codesign --force --sign "$MAC_SIGN_IDENTITY" --keychain "$KEYCHAIN" --timestamp --options runtime --entitlements entitlements.mac.plist "$component"
  fi
done < <(find "$APP_PATH" -type f -print0)
while IFS= read -r -d '' component; do
  codesign --force --sign "$MAC_SIGN_IDENTITY" --keychain "$KEYCHAIN" --timestamp --options runtime --entitlements entitlements.mac.plist "$component"
done < <(find "$APP_PATH" -depth -type d \( -name '*.framework' -o -name '*.app' \) -print0)
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$SIGN_ROOT/notarize.zip"
xcrun notarytool submit "$SIGN_ROOT/notarize.zip" --apple-id "$APPLE_ID" --password "$APPLE_APP_PASSWORD" --team-id "$APPLE_TEAM_ID" --wait --timeout 30m --output-format json > dist/notarization.json
python - <<'PY'
import json
with open('dist/notarization.json') as stream:
    assert json.load(stream).get('status') == 'Accepted', 'Apple notarization was not accepted'
PY
xcrun stapler staple "$APP_PATH"
xcrun stapler validate "$APP_PATH"
spctl --assess --type execute --verbose=2 "$APP_PATH"
rm -f "$SIGN_ROOT/notarize.zip"
