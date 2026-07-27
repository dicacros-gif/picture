#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_APP="$SCRIPT_DIR/Picture Cleaner.app"
TARGET_DIR="$HOME/Applications"
TARGET_APP="$TARGET_DIR/Picture Cleaner.app"

echo "Picture Cleaner M1 설치를 시작합니다."

if [[ ! -d "$SOURCE_APP" ]]; then
  echo "Picture Cleaner.app을 설치 파일과 같은 폴더에서 찾지 못했습니다."
  read -r "?Enter 키를 누르면 종료합니다."
  exit 1
fi

/usr/bin/osascript -e 'tell application "Picture Cleaner" to quit' >/dev/null 2>&1 || true
/bin/mkdir -p "$TARGET_DIR"

if [[ -e "$TARGET_APP" ]]; then
  BACKUP_APP="$HOME/.Trash/Picture Cleaner-old-$(date +%Y%m%d-%H%M%S).app"
  /bin/mv "$TARGET_APP" "$BACKUP_APP"
  echo "기존 앱은 휴지통에 백업했습니다."
fi

/usr/bin/ditto "$SOURCE_APP" "$TARGET_APP"
/usr/bin/xattr -dr com.apple.quarantine "$TARGET_APP" 2>/dev/null || true
/usr/bin/xattr -cr "$TARGET_APP"

if ! /usr/bin/codesign --verify --deep --strict "$TARGET_APP"; then
  echo "앱 서명 확인에 실패했습니다. ZIP을 다시 내려받아 주세요."
  read -r "?Enter 키를 누르면 종료합니다."
  exit 1
fi

echo "설치 및 서명 확인이 완료되었습니다."
/usr/bin/open "$TARGET_APP"
