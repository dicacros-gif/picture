#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
SOURCE_APP="$SCRIPT_DIR/Blog.app"
TARGET_DIR="$HOME/Applications"
TARGET_APP="$TARGET_DIR/Blog.app"
echo "Blog M1 설치를 시작합니다."
if [[ ! -d "$SOURCE_APP" || -L "$SOURCE_APP" ]]; then
  echo "압축을 완전히 푼 Blog.app과 설치 도구를 같은 폴더에 두세요."
  read -r "?Enter 키로 종료합니다."
  exit 1
fi
if ! /usr/bin/codesign --verify --deep --strict "$SOURCE_APP"; then
  echo "설치 원본의 서명이 올바르지 않습니다. ZIP을 다시 받아 주세요."
  read -r "?Enter 키로 종료합니다."
  exit 1
fi
/usr/bin/lipo -verify_arch arm64 "$SOURCE_APP/Contents/MacOS/Blog"
/usr/bin/osascript -e 'tell application "Blog" to quit' >/dev/null 2>&1 || true
/bin/mkdir -p "$TARGET_DIR"
STAGING_DIR="$(/usr/bin/mktemp -d "$TARGET_DIR/.blog-install.XXXXXX")"
/usr/bin/ditto "$SOURCE_APP" "$STAGING_DIR/Blog.app"
# Only this explicitly installed app's quarantine marker is removed; system policy is unchanged.
/usr/bin/xattr -dr com.apple.quarantine "$STAGING_DIR/Blog.app" 2>/dev/null || true
/usr/bin/codesign --verify --deep --strict "$STAGING_DIR/Blog.app"
if [[ -L "$TARGET_APP" ]]; then
  echo "설치 대상이 바로가기여서 덮어쓰지 않았습니다: $TARGET_APP"
  exit 1
fi
BACKUP_APP=""
if [[ -e "$TARGET_APP" ]]; then
  /bin/mkdir -p "$HOME/.Trash"
  BACKUP_APP="$HOME/.Trash/Blog-previous-$(date +%Y%m%d-%H%M%S).app"
  /bin/mv "$TARGET_APP" "$BACKUP_APP"
fi
if ! /bin/mv "$STAGING_DIR/Blog.app" "$TARGET_APP"; then
  [[ -n "$BACKUP_APP" ]] && /bin/mv "$BACKUP_APP" "$TARGET_APP"
  echo "설치하지 못해 이전 앱을 복원했습니다."
  exit 1
fi
/bin/rmdir "$STAGING_DIR"
echo "설치 및 ARM64 코드 서명 확인 완료. 기존 설정과 로그인 자료는 보존합니다."
/usr/bin/open "$TARGET_APP"
