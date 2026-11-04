# device_app

A new Flutter project.

## Getting Started

This project is a starting point for a Flutter application.

A few resources to get you started if this is your first Flutter project:

- [Learn Flutter](https://docs.flutter.dev/get-started/learn-flutter)
- [Write your first Flutter app](https://docs.flutter.dev/get-started/codelab)
- [Flutter learning resources](https://docs.flutter.dev/reference/learning-resources)

For help getting started with Flutter development, view the
[online documentation](https://docs.flutter.dev/), which offers tutorials,
samples, guidance on mobile development, and a full API reference.

## 원격 업데이트

`front_device` 브랜치에 push하면 GitHub Actions(`.github/workflows/device-app-release.yml`)가
릴리즈 키로 서명한 APK를 빌드해 Releases의 `device-app-latest`에 APK와 `version.json`을 올린다.

기기 앱은 10분마다 `version.json`을 확인하고, 새 빌드가 있으면 APK를 백그라운드로 받아 둔다.
주문이 없을 때 오른쪽 위에 **새 버전 업데이트** 버튼이 뜨고, 관리자 비밀번호를 넣으면
화면 고정을 풀고 안드로이드 설치 화면이 열린다. **업데이트 → 열기**를 누르면 끝.

- 처음 한 번은 기기마다 "이 출처 허용"(출처를 알 수 없는 앱 설치)을 켜야 한다.
- 업데이트 확인은 CI 빌드에만 켜진다. (`--dart-define=BUILD_NUMBER`, `UPDATE_MANIFEST_URL`)
  `flutter run`이나 로컬에서 직접 빌드한 APK는 확인하지 않는다.
- 빌드 번호는 `100 + 워크플로 실행 번호`. pubspec의 `version`은 표시용 이름만 쓴다.

필요한 Secrets (Settings → Secrets and variables → Actions)

| 이름 | 값 |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | `base64 -i android/my-release-key.jks` 결과 |
| `ANDROID_KEYSTORE_PASSWORD` | `android/key.properties`의 `storePassword` |
| `ANDROID_KEY_PASSWORD` | (선택) `keyPassword`가 다를 때만 |

> 서명 키(`my-release-key.jks`)를 잃어버리면 기기에 덮어쓰기 설치를 할 수 없다. 꼭 따로 백업해 둘 것.
