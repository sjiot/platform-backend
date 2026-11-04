import 'package:flutter/foundation.dart';

/// 로그 태그. 터미널에서 `grep "\[STATE\]"`처럼 골라 보기 위해 고정 문자열로 쓴다.
class LogTag {
  static const String app = 'APP'; // 앱 시작 · 종료
  static const String state = 'STATE'; // 화면(DeviceStep) 전이
  static const String api = 'API'; // 서버 요청 · 응답
  static const String timer = 'TIMER'; // 폴링 · 재호출 · 노쇼 타이머
  static const String qr = 'QR'; // QR 스캔 · order_id 추출
  static const String screen = 'SCREEN'; // 각 화면 내부 동작
  static const String kiosk = 'KIOSK'; // 화면 고정
  static const String demo = 'DEMO'; // 테스트 메뉴로 강제 이동
  static const String update = 'UPDATE'; // 원격 업데이트 확인 · 다운로드 · 설치
  static const String error = 'ERROR'; // 처리되지 않은 예외
}

enum LogLevel { debug, info, warn, error }

/// 터미널 디버그 로그.
///
/// 출력 형식: `21:04:12.381 I [STATE] orderCall → waiting (start 200)`
///
/// - 디버그 빌드(`flutter run`)에서는 기본으로 켜진다.
/// - 릴리스 빌드에서는 꺼져 있고, 필요하면
///   `flutter run --release --dart-define=DEBUG_LOG=true`로 켠다.
/// - error 레벨은 설정과 상관없이 항상 출력한다.
///
/// 보는 법:
///   flutter run                      (실행 터미널에 바로 출력)
///   flutter logs                     (이미 실행 중인 기기)
///   adb logcat -s flutter            (안드로이드 단말)
///   flutter logs | grep -E "\[STATE\]|\[API\]"
class AppLog {
  static const bool _forceOn = bool.fromEnvironment('DEBUG_LOG');

  static bool enabled = kDebugMode || _forceOn;

  /// 응답 본문 등 긴 문자열은 이 길이에서 자른다.
  static const int maxLength = 600;

  /// 스택은 앞쪽 몇 줄만 출력한다.
  static const int maxStackLines = 15;

  static void d(String tag, String message) =>
      _write(LogLevel.debug, tag, message);

  static void i(String tag, String message) =>
      _write(LogLevel.info, tag, message);

  static void w(String tag, String message) =>
      _write(LogLevel.warn, tag, message);

  static void e(
    String tag,
    String message, {
    Object? error,
    StackTrace? stack,
  }) {
    _write(LogLevel.error, tag, message);

    if (error != null) {
      _write(LogLevel.error, tag, '  error: $error');
    }

    if (stack != null) {
      final lines = stack
          .toString()
          .split('\n')
          .where((line) => line.trim().isNotEmpty)
          .take(maxStackLines);

      for (final line in lines) {
        _write(LogLevel.error, tag, '  $line');
      }
    }
  }

  /// 긴 문자열 자르기 (응답 본문 출력용)
  static String short(String? value) {
    if (value == null) return 'null';

    final oneLine = value.replaceAll('\n', ' ');

    if (oneLine.length <= maxLength) return oneLine;

    return '${oneLine.substring(0, maxLength)}… (${oneLine.length}자)';
  }

  static void _write(LogLevel level, String tag, String message) {
    if (!enabled && level != LogLevel.error) return;

    final String prefix = '${_timestamp()} ${_levelMark(level)} [$tag]';

    for (final line in message.split('\n')) {
      debugPrint('$prefix $line');
    }
  }

  static String _levelMark(LogLevel level) => switch (level) {
    LogLevel.debug => 'D',
    LogLevel.info => 'I',
    LogLevel.warn => 'W',
    LogLevel.error => 'E',
  };

  static String _timestamp() {
    final now = DateTime.now();

    String two(int v) => v.toString().padLeft(2, '0');
    String three(int v) => v.toString().padLeft(3, '0');

    return '${two(now.hour)}:${two(now.minute)}:${two(now.second)}'
        '.${three(now.millisecond)}';
  }
}
