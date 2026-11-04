import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

import '../debug/app_log.dart';

/// GitHub Releases에 올라온 최신 빌드 정보 (version.json).
class UpdateInfo {
  final int buildNumber;
  final String versionName;
  final Uri apkUrl;
  final String sha256;
  final int? size;
  final String? commit;

  const UpdateInfo({
    required this.buildNumber,
    required this.versionName,
    required this.apkUrl,
    required this.sha256,
    this.size,
    this.commit,
  });

  factory UpdateInfo.fromJson(Map<String, dynamic> json) {
    final dynamic rawBuild = json['buildNumber'];
    final int? buildNumber = rawBuild is int
        ? rawBuild
        : int.tryParse(rawBuild?.toString() ?? '');

    final String? apkUrl = json['apkUrl']?.toString();
    final String? sha256 = json['sha256']?.toString();

    if (buildNumber == null || apkUrl == null || sha256 == null) {
      throw FormatException('version.json 형식 오류: $json');
    }

    final dynamic rawSize = json['size'];

    return UpdateInfo(
      buildNumber: buildNumber,
      versionName: json['versionName']?.toString() ?? '',
      apkUrl: Uri.parse(apkUrl),
      sha256: sha256.toLowerCase(),
      size: rawSize is int ? rawSize : int.tryParse(rawSize?.toString() ?? ''),
      commit: json['commit']?.toString(),
    );
  }

  /// 예: `1.0.0 (105)`
  String get label =>
      versionName.isEmpty ? '$buildNumber' : '$versionName ($buildNumber)';
}

/// 원격 자동 업데이트.
///
/// 1. front_device 브랜치에 push하면 GitHub Actions가 APK를 빌드해
///    Releases의 `device-app-latest`에 APK와 version.json을 올린다.
///    (.github/workflows/device-app-release.yml)
/// 2. 앱은 [checkInterval]마다 version.json을 보고, 지금보다 빌드 번호가 크면
///    APK를 백그라운드로 받아 둔다.
/// 3. 설치는 관리자가 업데이트 버튼 + 비밀번호로 시작한다.
///    (안드로이드는 일반 앱이 사용자 확인 없이 설치하는 것을 막기 때문)
///
/// BUILD_NUMBER · UPDATE_MANIFEST_URL은 CI 빌드에만 `--dart-define`으로 들어가므로
/// `flutter run`이나 로컬 수동 빌드에서는 업데이트 확인이 꺼져 있다.
class UpdateService {
  static const MethodChannel _channel = MethodChannel('device_app/updater');

  /// 이 APK의 빌드 번호 (CI가 넣어 줌, 로컬 빌드는 0)
  static const int currentBuild = int.fromEnvironment('BUILD_NUMBER');

  static const String _manifestUrl = String.fromEnvironment(
    'UPDATE_MANIFEST_URL',
  );

  static const Duration checkInterval = Duration(minutes: 10);

  static const Duration _requestTimeout = Duration(seconds: 15);

  /// 다운로드 중 이 시간 동안 데이터가 한 번도 안 오면 실패로 본다.
  static const Duration _stallTimeout = Duration(seconds: 30);

  static bool get enabled =>
      !kIsWeb &&
      Platform.isAndroid &&
      currentBuild > 0 &&
      _manifestUrl.isNotEmpty;

  /// 지금보다 새 빌드가 올라와 있으면 그 정보를, 아니면 null.
  static Future<UpdateInfo?> fetchNewer() async {
    final Uri uri = Uri.parse(_manifestUrl);

    final http.Response res = await http
        .get(uri, headers: const {'Cache-Control': 'no-cache'})
        .timeout(_requestTimeout);

    if (res.statusCode != 200) {
      throw HttpException('version.json 응답 ${res.statusCode}', uri: uri);
    }

    final Map<String, dynamic> json =
        jsonDecode(utf8.decode(res.bodyBytes)) as Map<String, dynamic>;

    final UpdateInfo info = UpdateInfo.fromJson(json);

    return info.buildNumber > currentBuild ? info : null;
  }

  /// [info]의 APK를 받아 두고 파일을 돌려준다.
  ///
  /// 같은 빌드를 이미 받아 뒀고 SHA-256이 맞으면 다시 받지 않는다.
  /// 다른 빌드 파일은 지운다.
  static Future<File> download(UpdateInfo info) async {
    final Directory dir = await _updateDir();
    final File file = File('${dir.path}/device_app-${info.buildNumber}.apk');

    await _deleteOthers(dir, keep: file);

    if (await file.exists()) {
      if (await _matchesSha(file, info.sha256)) {
        AppLog.d(LogTag.update, '이미 받아 둔 APK 사용: ${file.path}');
        return file;
      }

      await file.delete();
    }

    final File partial = File('${file.path}.part');
    final http.Client client = http.Client();

    try {
      final http.StreamedResponse res = await client
          .send(http.Request('GET', info.apkUrl))
          .timeout(_requestTimeout);

      if (res.statusCode != 200) {
        throw HttpException('APK 응답 ${res.statusCode}', uri: info.apkUrl);
      }

      final IOSink sink = partial.openWrite();
      int received = 0;

      try {
        await for (final List<int> chunk in res.stream.timeout(
          _stallTimeout,
        )) {
          sink.add(chunk);
          received += chunk.length;
        }
      } finally {
        await sink.close();
      }

      final int? expected = info.size ?? res.contentLength;

      if (expected != null && received != expected) {
        throw StateError('APK 크기 불일치: 받은 $received / 예상 $expected');
      }

      if (!await _matchesSha(partial, info.sha256)) {
        throw StateError('APK SHA-256 불일치 (그사이 새 빌드가 올라왔을 수 있음)');
      }

      await partial.rename(file.path);

      AppLog.i(
        LogTag.update,
        'APK 다운로드 완료: ${info.label} '
        '(${(received / 1024 / 1024).toStringAsFixed(1)}MB)',
      );

      return file;
    } catch (_) {
      if (await partial.exists()) {
        await partial.delete();
      }

      rethrow;
    } finally {
      client.close();
    }
  }

  /// 받아 둔 업데이트 파일을 모두 지운다. (최신 빌드를 쓰고 있을 때)
  static Future<void> clearDownloads() async {
    try {
      await _deleteOthers(await _updateDir());
    } catch (e) {
      AppLog.w(LogTag.update, '업데이트 폴더 정리 실패: $e');
    }
  }

  /// 화면 고정을 풀고 안드로이드 설치 화면을 연다.
  ///
  /// 설치를 누르면 앱이 종료되고 새 버전으로 바뀐다. 설치 완료 화면에서
  /// [열기]를 누르면 새 버전이 켜지면서 다시 화면 고정이 걸린다.
  /// 취소하고 돌아오면 resumed 처리에서 다시 고정한다.
  static Future<void> install(File apk) async {
    await _channel.invokeMethod<bool>('installApk', {'path': apk.path});
  }

  static Future<Directory> _updateDir() async {
    final String? path = await _channel.invokeMethod<String>('updateDir');

    if (path == null) {
      throw StateError('업데이트 폴더 경로를 받지 못함');
    }

    final Directory dir = Directory(path);

    await dir.create(recursive: true);

    return dir;
  }

  static Future<void> _deleteOthers(Directory dir, {File? keep}) async {
    await for (final FileSystemEntity entity in dir.list()) {
      if (entity is! File || entity.path == keep?.path) continue;

      try {
        await entity.delete();
      } catch (_) {
        // 지우지 못한 파일은 다음에 다시 시도한다.
      }
    }
  }

  static Future<bool> _matchesSha(File file, String expected) async {
    final String? actual = await _channel.invokeMethod<String>('sha256', {
      'path': file.path,
    });

    return actual != null && actual.toLowerCase() == expected;
  }
}
