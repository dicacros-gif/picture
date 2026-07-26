# Picture Cleaner for Mac (Apple Silicon)

Android판의 세 단계를 macOS 데스크톱 앱으로 옮긴 버전입니다.

## 실행

1. `Picture Cleaner.app`을 Applications 폴더로 옮깁니다.
2. 최초 실행 때 macOS가 개발자를 확인할 수 없다고 표시하면 앱을 Control-클릭하고 **열기**를 선택합니다.
3. 네이버 블로그 자동화에서 블로그 아이디를 입력하고 **네이버 로그인/블로그 열기**를 눌러 로그인합니다.
4. 댓글 자동 답글은 최근 10일 게시글만 확인하며, 내 답글이 화면에 있거나 로컬 처리 기록에 있는 댓글은 건너뜁니다.

네이버의 보안 확인 또는 CAPTCHA는 자동으로 우회하지 않습니다. 전용 네이버 창에서 직접 확인을 마친 뒤 다시 실행하세요.

## 개발 빌드

```bash
npm ci
npm run build:mac
```

결과물은 `dist/Picture-Cleaner-M1-1.0.0.zip`입니다.
