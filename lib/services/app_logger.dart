// [신규] 원격 점검용 파일 로거.
//
// 무엇을 남기나
// - 앱 시작/종료 정보, 10분마다 "alive" 생존 신호(언제부터 멈췄는지 확인용)
// - 처리되지 않은 에러(FlutterError, 비동기 에러)와 스택트레이스
// - 기존 debugPrint(...) 출력 전부 (release 빌드에서는 화면에 안 보이므로 파일로 보존)
// - 직접 남기고 싶은 곳: AppLog.i('메시지') / AppLog.e('메시지', error, stack)
//
// 어디에 남나: Windows  %LOCALAPPDATA%\ClickyKeyring\logs\app.log
//   (2MB 넘으면 app.old.log 로 교체되어 용량이 무한정 늘지 않습니다)
// 주의: 개인정보(이름/연락처/토큰 등)는 로그에 넣지 마세요.
//
// 플랫폼 분기: 웹 빌드는 no-op (app_logger_web.dart), 그 외는 파일 기록.
import 'dart:async';

import 'package:flutter/foundation.dart';

import 'app_logger_web.dart' if (dart.library.io) 'app_logger_io.dart' as sink;

class AppLog {
  AppLog._();

  static bool _inited = false;

  static String? get path => sink.logFilePath;

  /// main() 맨 앞(WidgetsFlutterBinding.ensureInitialized 직후)에서 한 번 호출.
  static Future<void> init() async {
    if (_inited) return;
    _inited = true;
    await sink.initSink();

    // 1) Flutter 프레임워크 에러
    final prevOnError = FlutterError.onError;
    FlutterError.onError = (details) {
      e('FlutterError: ${details.exceptionAsString()}', null, details.stack);
      prevOnError?.call(details);
    };

    // 2) 처리되지 않은 비동기 에러
    PlatformDispatcher.instance.onError = (error, stack) {
      e('Uncaught', error, stack);
      return true; // 앱이 죽지 않고 계속 동작(키오스크라 중요)
    };

    // 3) 기존 debugPrint 출력도 파일로 같이 저장
    final originalDebugPrint = debugPrint;
    debugPrint = (String? message, {int? wrapWidth}) {
      originalDebugPrint(message, wrapWidth: wrapWidth);
      if (message != null) i(message);
    };

    i('===== app started (mode=${kReleaseMode ? 'release' : 'debug'}) =====');

    // 생존 신호: 로그가 이 줄에서 끊겼다면 그 시각 즈음에 멈춘 것.
    Timer.periodic(
      const Duration(minutes: 10),
      (_) => i('alive'),
    );
  }

  static void i(String msg) => _write('INFO ', msg);

  static void e(String msg, [Object? error, StackTrace? stack]) {
    final buf = StringBuffer(msg);
    if (error != null) buf.write(' | $error');
    if (stack != null) buf.write('\n$stack');
    _write('ERROR', buf.toString());
  }

  static void _write(String level, String msg) {
    sink.writeLine('${DateTime.now().toIso8601String()} $level $msg');
  }
}
