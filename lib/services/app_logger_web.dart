// [신규] 웹 빌드용 로그 저장소 — 파일 시스템이 없으므로 아무것도 하지 않습니다.
String? get logFilePath => null;
Future<void> initSink() async {}
void writeLine(String line) {}
