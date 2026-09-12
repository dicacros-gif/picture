#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
SOURCE_APP="$SCRIPT_DIR/Blog.app"
TARGET_DIR="$HOME/Applications"
TARGET_APP="$TARGET_DIR/Blog.app"
STAGING_DIR=""
APP_IDENTIFIER="com.dicacros.picturecleaner.mac"

fail() {
  echo "$1" >&2
  [[ -t 0 ]] && read -r "?Enter 키로 종료합니다."
  exit 1
}

cleanup() {
  local result=$?
  # mktemp created this directory under the resolved installation directory.
  # Refuse cleanup if its location or type changed while the installer ran.
  if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" && ! -L "$STAGING_DIR" &&
        "$STAGING_DIR" == "$TARGET_DIR"/.blog-install.* &&
        "$(cd "$STAGING_DIR" && pwd -P)" == "$STAGING_DIR" ]]; then
    /bin/rm -rf -- "$STAGING_DIR"
  fi
  return "$result"
}
trap cleanup EXIT

echo "Blog M1 설치를 시작합니다."
if [[ ! -d "$SOURCE_APP" || -L "$SOURCE_APP" ]]; then
  fail "압축을 완전히 푼 Blog.app과 설치 도구를 같은 폴더에 두세요."
fi
if ! /usr/bin/codesign --verify --deep --strict "$SOURCE_APP"; then
  fail "설치 원본의 서명이 올바르지 않습니다. ZIP을 다시 받아 주세요."
fi
if [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$SOURCE_APP/Contents/Info.plist")" != "$APP_IDENTIFIER" ]]; then
  fail "Blog 설치 원본의 앱 식별자가 올바르지 않습니다."
fi
if ! /usr/bin/lipo "$SOURCE_APP/Contents/MacOS/Blog" -verify_arch arm64; then
  fail "이 설치 원본에는 Apple Silicon용 실행 파일이 없습니다."
fi
[[ ! -L "$TARGET_DIR" ]] || fail "Applications 폴더가 바로가기여서 설치하지 않았습니다: $TARGET_DIR"
/bin/mkdir -p "$TARGET_DIR" || fail "설치 폴더를 만들 수 없습니다: $TARGET_DIR"
TARGET_DIR="$(cd "$TARGET_DIR" && pwd -P)"
TARGET_APP="$TARGET_DIR/Blog.app"
[[ ! -L "$TARGET_APP" ]] || fail "설치 대상이 바로가기여서 덮어쓰지 않았습니다: $TARGET_APP"
if [[ -e "$TARGET_APP" ]]; then
  [[ -d "$TARGET_APP" ]] || fail "설치 대상에 같은 이름의 파일이 있습니다: $TARGET_APP"
  if [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$TARGET_APP/Contents/Info.plist" 2>/dev/null || true)" != "$APP_IDENTIFIER" ]]; then
    fail "설치 대상에 다른 앱이 있어 덮어쓰지 않았습니다: $TARGET_APP"
  fi
fi
STAGING_DIR="$(/usr/bin/mktemp -d "$TARGET_DIR/.blog-install.XXXXXX")"
/usr/bin/ditto "$SOURCE_APP" "$STAGING_DIR/Blog.app" || fail "앱 복사에 실패했습니다. 기존 앱은 유지합니다."
# Only this explicitly installed app's quarantine marker is removed; system policy is unchanged.
/usr/bin/xattr -dr com.apple.quarantine "$STAGING_DIR/Blog.app" 2>/dev/null || true
if ! /usr/bin/codesign --verify --deep --strict "$STAGING_DIR/Blog.app"; then
  fail "복사된 앱의 서명 검증에 실패했습니다. 기존 앱은 유지합니다."
fi

# NSRunningApplication inspects only already-running apps. Unlike 'tell
# application "Blog"', this cannot launch an absent app or resolve a namesake.
if ! /usr/bin/osascript -l JavaScript - "$APP_IDENTIFIER" "$SOURCE_APP" "$TARGET_APP" \
    "$TARGET_DIR/Picture Cleaner.app" "/Applications/Blog.app" "/Applications/Picture Cleaner.app" <<'JXA'
ObjC.import('AppKit');
ObjC.import('Foundation');
function run(argv) {
  const identifier = argv[0];
  const allowed = new Set(argv.slice(1).map(value =>
    $.NSString.stringWithString(value).stringByStandardizingPath.js));
  const running = $.NSWorkspace.sharedWorkspace.runningApplications.js.filter(application => {
    if (!application.bundleIdentifier || application.bundleIdentifier.js !== identifier || !application.bundleURL) return false;
    return allowed.has(application.bundleURL.path.stringByStandardizingPath.js);
  });
  for (const application of running) {
    if (!application.terminate) throw new Error('실행 중인 Blog를 종료할 수 없습니다. 작업을 중지하고 앱을 종료한 뒤 다시 설치하세요.');
  }
  const deadline = Date.now() + 30000;
  while (running.some(application => !application.isTerminated)) {
    if (Date.now() >= deadline) throw new Error('Blog 종료를 기다리다 설치를 중단했습니다. 기존 앱은 유지합니다.');
    $.NSThread.sleepForTimeInterval(0.25);
  }
}
JXA
then
  fail "기존 Blog의 정상 종료를 확인하지 못해 설치를 중단했습니다."
fi

BACKUP_APP=""
if [[ -e "$TARGET_APP" ]]; then
  [[ ! -L "$HOME/.Trash" ]] || fail "휴지통 폴더가 바로가기여서 기존 앱을 이동하지 않았습니다."
  /bin/mkdir -p "$HOME/.Trash" || fail "이전 앱을 보관할 휴지통에 접근할 수 없습니다."
  BACKUP_DIR="$(/usr/bin/mktemp -d "$HOME/.Trash/Blog-previous.XXXXXX")"
  BACKUP_APP="$BACKUP_DIR/Blog.app"
  /bin/mv "$TARGET_APP" "$BACKUP_APP" || fail "기존 앱을 백업하지 못해 설치를 중단했습니다."
fi
if ! /bin/mv "$STAGING_DIR/Blog.app" "$TARGET_APP"; then
  if [[ -n "$BACKUP_APP" ]]; then
    if [[ ! -e "$TARGET_APP" && ! -L "$TARGET_APP" ]] && /bin/mv "$BACKUP_APP" "$TARGET_APP"; then
      fail "설치하지 못해 이전 앱을 복원했습니다."
    fi
    fail "설치에 실패했습니다. 이전 앱은 다음 경로에 보관되어 있습니다: $BACKUP_APP"
  fi
  fail "앱을 설치 폴더로 이동하지 못했습니다."
fi
/bin/rmdir "$STAGING_DIR"
STAGING_DIR=""
echo "설치 및 ARM64 코드 서명 확인 완료. 기존 설정과 로그인 자료는 보존합니다."
/usr/bin/open "$TARGET_APP" || fail "설치는 완료했습니다. Applications 폴더에서 Blog.app을 직접 열어 주세요."
