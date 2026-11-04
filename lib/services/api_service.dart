import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../debug/app_log.dart';

class ApiException implements Exception {
  final String action;
  final int statusCode;
  final String body;
  final Map<String, dynamic>? data;

  ApiException({
    required this.action,
    required this.statusCode,
    required this.body,
    this.data,
  });

  @override
  String toString() {
    return '$action 실패 ($statusCode): $body';
  }
}

/// Flask 백엔드(Cloud Run) 연동.
///
/// 모든 요청은 [_send]를 거친다.
/// - 요청마다 번호(#12)를 붙여 요청 ↔ 응답 로그를 짝지을 수 있게 한다.
/// - [timeout]을 넘기면 TimeoutException을 던진다.
///   (멈춘 요청 때문에 폴링이 조용히 멈추는 것을 막기 위함)
/// - 걸린 시간(ms)과 응답 본문을 한 줄로 출력한다.
/// - 응답이 JSON이 아니면(Cloud Run 502 HTML 등) 경고로 출력한다.
class ApiService {
  final String baseUrl = 'http://192.168.0.100:5000';

  static const Duration timeout = Duration(seconds: 5);

  static int _requestSeq = 0;

  Future<Map<String, dynamic>> getStationStatus({required int stationId}) {
    return _send(
      action: 'STATION_STATUS',
      method: 'GET',
      path: '/station/$stationId/status',
    );
  }

  Future<Map<String, dynamic>> getOrderStatus({required String orderId}) {
    return _send(
      action: 'ORDER_STATUS',
      method: 'GET',
      path: '/order/$orderId/status',
    );
  }

  /// QR 인증 = 조립 시작
  ///
  /// POST /station/{order_id}/start
  Future<Map<String, dynamic>> startStation({
    required String orderId,
    required int stationId,
  }) {
    return _send(
      action: 'START',
      method: 'POST',
      path: '/station/$orderId/start',
      body: {'station_id': stationId},
    );
  }

  /// 노쇼(호출 시간 초과) 처리
  ///
  /// POST /station/{station_id}/unclaim
  /// 서버에서 주문 stage를 expired로 바꾸고 조립대를 비운 뒤
  /// 대기열의 다음 주문을 재배정한다.
  Future<Map<String, dynamic>> cancelStation({
    required int stationId,
    required String orderId,
  }) {
    return _send(
      action: 'UNCLAIM',
      method: 'POST',
      path: '/station/$stationId/unclaim',
      body: {'order_id': orderId},
    );
  }

  /// 조립 완료
  ///
  /// POST /station/{station_id}/complete
  Future<Map<String, dynamic>> completeStation({
    required int stationId,
    required String orderId,
  }) {
    return _send(
      action: 'COMPLETE',
      method: 'POST',
      path: '/station/$stationId/complete',
      body: {'order_id': orderId},
    );
  }

  Future<Map<String, dynamic>> _send({
    required String action,
    required String method,
    required String path,
    Map<String, dynamic>? body,
  }) async {
    final int id = ++_requestSeq;
    final Uri uri = Uri.parse('$baseUrl$path');
    final String? encodedBody = body == null ? null : jsonEncode(body);

    AppLog.d(
      LogTag.api,
      '#$id → $action $method $path'
      '${encodedBody == null ? '' : ' body=$encodedBody'}',
    );

    final Stopwatch stopwatch = Stopwatch()..start();

    final http.Response res;

    try {
      final Future<http.Response> request = method == 'GET'
          ? http.get(
              uri,
              headers: {
                'Accept': 'application/json',
                'ngrok-skip-browser-warning': 'true',
              },
            )
          : http.post(
              uri,
              headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'ngrok-skip-browser-warning': 'true',
              },
              body: encodedBody,
            );

      res = await request.timeout(timeout);
    } on TimeoutException {
      AppLog.e(
        LogTag.api,
        '#$id ✕ $action 타임아웃 (${timeout.inSeconds}초 초과) $method $path',
      );
      rethrow;
    } catch (e) {
      // 네트워크 오류는 스택보다 오류 내용(SocketException 등)이 중요하다.
      AppLog.e(
        LogTag.api,
        '#$id ✕ $action 통신 실패 (${stopwatch.elapsedMilliseconds}ms) '
        '$method $path',
        error: e,
      );
      rethrow;
    }

    final int elapsed = stopwatch.elapsedMilliseconds;

    Map<String, dynamic>? data;

    try {
      final dynamic decoded = jsonDecode(res.body);

      if (decoded is Map<String, dynamic>) {
        data = decoded;
      } else {
        AppLog.w(LogTag.api, '#$id 응답이 JSON 객체가 아님 (${decoded.runtimeType})');
      }
    } catch (_) {
      AppLog.w(
        LogTag.api,
        '#$id 응답을 JSON으로 읽을 수 없음 '
        '(content-type: ${res.headers['content-type'] ?? '-'})',
      );
    }

    final String summary =
        '#$id ← $action ${res.statusCode} ${elapsed}ms ${AppLog.short(res.body)}';

    if (res.statusCode == 200) {
      AppLog.d(LogTag.api, summary);

      return data ?? <String, dynamic>{};
    }

    AppLog.w(LogTag.api, summary);

    throw ApiException(
      action: action,
      statusCode: res.statusCode,
      body: res.body,
      data: data,
    );
  }
}
