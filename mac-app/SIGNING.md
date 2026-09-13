# Blog M1 서명과 공증

`Build Mac M1 App` 작업은 macOS ARM64에서 Python 엔진과 Electron 앱을 빌드한 뒤 코드 서명, 한국어 이미지 출력, 실제 앱 실행을 검사합니다. `signing-verification.json`에 결과와 서명 종류를 기록합니다.

기본 `adhoc`는 실행 코드 무결성을 확인하는 임시 서명이며 Apple Developer ID 인증이나 Apple 공증이 아닙니다. 현재 저장소에는 배포 인증서가 등록되어 있지 않습니다.

Apple 공증판은 GitHub 저장소 **Settings → Secrets and variables → Actions**에 아래 비밀값을 등록하고 작업 실행 시 `developer-id`를 선택합니다. 비밀값은 채팅이나 소스 파일에 넣지 않습니다.

- `MAC_CERTIFICATE_BASE64`: 개인 키가 포함된 Developer ID Application 인증서 P12의 Base64
- `MAC_CERTIFICATE_PASSWORD`: P12 암호
- `MAC_SIGN_IDENTITY`: 인증서의 Developer ID Application 서명 이름
- `APPLE_ID`, `APPLE_APP_PASSWORD`, `APPLE_TEAM_ID`: Apple 공증 계정, 앱 전용 암호, 팀 ID

일회용 키체인에 인증서를 가져와 내부 실행 파일부터 서명합니다. Apple `notarytool`의 Accepted 결과, 공증 티켓 첨부와 검증, Gatekeeper 검증까지 통과해야 패키지를 생성합니다. 비밀값 누락이나 공증 실패 시 임시 서명으로 대체하지 않고 빌드를 실패시킵니다. 키체인은 작업 종료 시 제거합니다.

네이버와 LLM CLI 구독 로그인은 코드 서명과 별개입니다. 배포 앱에는 다른 사람의 로그인 토큰을 포함하지 않으며 최초 실행 시 사용자가 본인의 계정으로 로그인합니다. ChatGPT는 일반 로그인과 기기 코드 로그인을 지원합니다.
