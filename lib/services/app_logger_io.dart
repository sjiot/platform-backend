// [신규] 파일 로그 저장소(데스크톱 전용 구현).
// Windows 에서는 %LOCALAPPDATA%\ClickyKeyring\logs\app.log 에 기록합니다.
import 'dart:convert';
import 'dart:io';

File? _file;
Directory? _dir;
int _written = 0;
const int _maxBytes = 2 * 1024 * 1024; // 2MB 넘으면 app.old.log 로 교체

String? get logFilePath => _file?.path;

Future<void> initSink() async {
  try {
    final env = Platform.environment;
    final base = env['LOCALAPPDATA'] ??
        (env['HOME'] != null ? '${env['HOME']}/Library/Logs' : null) ??
        Directory.systemTemp.path;
    final dir = Directory('$base${Platform.pathSeparator}ClickyKeyring'
        '${Platform.pathSeparator}logs');
    await dir.create(recursive: true);
    _dir = dir;
    _file = File('${dir.path}${Platform.pathSeparator}app.log');
    _written = await _file!.exists() ? await _file!.length() : 0;
    if (_written > _maxBytes) _rotate();
  } catch (_) {
    // 로그 때문에 앱이 죽으면 안 되므로 조용히 무시합니다.
    _file = null;
  }
}

void _rotate() {
  try {
    final old = File('${_dir!.path}${Platform.pathSeparator}app.old.log');
    if (old.existsSync()) old.deleteSync();
    _file!.renameSync(old.path);
    _written = 0;
  } catch (_) {}
}

void writeLine(String line) {
  final f = _file;
  if (f == null) return;
  try {
    final data = '$line\n';
    f.writeAsStringSync(data, mode: FileMode.append, flush: true);
    // 한글은 UTF-8 로 글자당 3바이트이므로 글자 수가 아니라 바이트 수로 셉니다.
    _written += utf8.encode(data).length;
    if (_written > _maxBytes) _rotate();
  } catch (_) {}
}
