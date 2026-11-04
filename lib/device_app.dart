import 'dart:async';
import 'dart:io' show File;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'debug/app_log.dart';
import 'dev/demo_menu.dart';
import 'models/device_step.dart';
import 'models/order_info.dart';
import 'screens/assembling_screen.dart';
import 'screens/authenticated_screen.dart';
import 'screens/completed_screen.dart';
import 'screens/invalid_qr_screen.dart';
import 'screens/no_show_screen.dart';
import 'screens/order_call_screen.dart';
import 'screens/qr_scanner_screen.dart';
import 'screens/waiting_screen.dart';
import 'screens/workstation_setup_screen.dart';
import 'screens/wrong_workstation_screen.dart';
import 'services/api_service.dart';
import 'services/kiosk_service.dart';
import 'services/update_service.dart';
import 'theme/app_colors.dart';
import 'widgets/app_top_bar.dart';
import 'widgets/exit_password_dialog.dart';
import 'widgets/screen_canvas.dart';
import 'widgets/update_badge.dart';
import 'widgets/workstation_header.dart';

class DeviceApp extends StatelessWidget {
  const DeviceApp({super.key});

  @override
  Widget build(BuildContext context) {
    final ThemeData base = ThemeData(
      useMaterial3: true,
      fontFamily: 'Pretendard',
      scaffoldBackgroundColor: AppColors.background,
      colorScheme: ColorScheme.fromSeed(seedColor: AppColors.text),
    );

    return MaterialApp(
      debugShowCheckedModeBanner: false,
      title: '조립대 디바이스 앱',
      // 스타일을 따로 주지 않은 글씨(스낵바 · 팝업 · 기본 버튼)도
      // ScreenCanvas.minFontSize보다 작아지지 않게 한다.
      theme: base.copyWith(textTheme: _minSizedTextTheme(base.textTheme)),
      home: const DeviceRoot(),
    );
  }
}

/// 글씨 크기가 [ScreenCanvas.minFontSize]보다 작은 스타일을 끌어올린다.
TextStyle? _atLeastMinFont(TextStyle? style) {
  if (style == null) return null;

  final double? size = style.fontSize;

  if (size == null || size >= ScreenCanvas.minFontSize) {
    return style;
  }

  return style.copyWith(fontSize: ScreenCanvas.minFontSize);
}

TextTheme _minSizedTextTheme(TextTheme theme) {
  return TextTheme(
    displayLarge: _atLeastMinFont(theme.displayLarge),
    displayMedium: _atLeastMinFont(theme.displayMedium),
    displaySmall: _atLeastMinFont(theme.displaySmall),
    headlineLarge: _atLeastMinFont(theme.headlineLarge),
    headlineMedium: _atLeastMinFont(theme.headlineMedium),
    headlineSmall: _atLeastMinFont(theme.headlineSmall),
    titleLarge: _atLeastMinFont(theme.titleLarge),
    titleMedium: _atLeastMinFont(theme.titleMedium),
    titleSmall: _atLeastMinFont(theme.titleSmall),
    bodyLarge: _atLeastMinFont(theme.bodyLarge),
    bodyMedium: _atLeastMinFont(theme.bodyMedium),
    bodySmall: _atLeastMinFont(theme.bodySmall),
    labelLarge: _atLeastMinFont(theme.labelLarge),
    labelMedium: _atLeastMinFont(theme.labelMedium),
    labelSmall: _atLeastMinFont(theme.labelSmall),
  );
}

class DeviceRoot extends StatefulWidget {
  const DeviceRoot({super.key});

  @override
  State<DeviceRoot> createState() => _DeviceRootState();
}

class _DeviceRootState extends State<DeviceRoot> with WidgetsBindingObserver {
  // 서비스
  final ApiService _api = ApiService();

  // 앱 진행 상태
  DeviceStep _currentStep = DeviceStep.workstationSetup;
  String? _workstationNumber; // 앱을 종료하기 전까지 유지되는 조립대 번호
  String? _assignedWorkstationNumber;
  OrderInfo? _order;

  // 대기번호 호출 상태
  int? _orderCallNumber;
  String? _calledOrderId;

  // 조립대 상태 조회
  Timer? _stationStatusTimer;
  bool _isFetchingStationStatus = false;

  // 노쇼 처리
  //
  // 호출된 뒤 3분 안에 QR 인증이 없으면 노쇼로 보고 주문을 취소한다.
  static const Duration _noShowTimeout = Duration(minutes: 3);

  Timer? _noShowTimer;

  // QR 스캐너가 열려 있는지 여부
  bool _isQrScannerOpen = false;

  // 노쇼 취소 요청이 중복 실행되는 것을 막는다.
  bool _isHandlingNoShow = false;

  // QR 인증(start) 요청이 진행 중인지.
  // 이 사이 노쇼 시간이 끝나면 바로 취소하지 않고 start 결과를 보고 처리한다.
  bool _isStarting = false;
  bool _noShowPending = false;

  // 조립 완료(complete) 요청 중복 방지
  bool _isCompleting = false;

  // 관리자 종료 중에는 화면 고정을 다시 걸지 않는다.
  bool _isExiting = false;

  // 원격 업데이트
  //
  // 새 빌드를 백그라운드로 받아 두고, 한가할 때 업데이트 버튼을 띄운다.
  Timer? _updateCheckTimer;
  bool _isCheckingUpdate = false;
  bool _isInstallingUpdate = false;
  UpdateInfo? _readyUpdate; // 받아 두고 설치를 기다리는 빌드
  File? _readyUpdateFile;

  // 주기
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);

    // 화면이 올라온 뒤에 고정을 건다. (액티비티가 resumed 상태여야 함)
    WidgetsBinding.instance.addPostFrameCallback((_) {
      KioskService.start();
    });

    _startUpdateChecks();
  }

  /// 사용자가 고정을 풀고 나갔다가 돌아온 경우 다시 고정한다.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    super.didChangeAppLifecycleState(state);

    if (state == AppLifecycleState.resumed && !_isExiting) {
      KioskService.start();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);

    _stationStatusTimer?.cancel();
    _stopCallTimers();
    _updateCheckTimer?.cancel();

    super.dispose();
  }

  // 1. 조립대 설정
  void _selectWorkstation(String number) {
    _stopStationStatusPolling();
    _stopCallTimers();

    _setStep(
      DeviceStep.orderCall,
      '조립대 $number 선택',
      update: () {
        _workstationNumber = number;

        // 아직 서버에서 주문을 받기 전
        _orderCallNumber = null;
        _calledOrderId = null;
        _order = null;
      },
    );

    // 주문 배정 여부 조회 시작
    _startStationStatusPolling();
  }

  // 2. 조립대 주문 조회
  void _startStationStatusPolling() {
    _stationStatusTimer?.cancel();

    // 화면 진입하자마자 한 번 즉시 조회
    _fetchStationStatus();

    // 이후 호출 화면에 있는 동안 2초마다 조회
    // (주문을 찾은 뒤에도 계속 돌아야 관리자 인증 · 주문 중단을 알 수 있다)
    _stationStatusTimer = Timer.periodic(const Duration(seconds: 2), (_) {
      if (!mounted) return;

      if (_currentStep != DeviceStep.orderCall) {
        return;
      }

      _fetchStationStatus();
    });
  }

  void _stopStationStatusPolling() {
    _stationStatusTimer?.cancel();
    _stationStatusTimer = null;
  }

  Future<void> _fetchStationStatus() async {
    if (_isFetchingStationStatus) {
      return;
    }

    // QR 인증 · 노쇼 요청이 가는 중이면 그 결과가 다음 화면을 정한다.
    if (_isStarting || _isHandlingNoShow) {
      return;
    }

    if (_workstationNumber == null) {
      return;
    }

    _isFetchingStationStatus = true;

    final int stationId = int.parse(_workstationNumber!);

    try {
      final result = await _api.getStationStatus(stationId: stationId);

      if (!mounted) return;

      // 응답을 기다리는 사이 화면이 바뀌었거나 QR 인증 · 노쇼 요청이 시작된 경우
      if (_currentStep != DeviceStep.orderCall ||
          _isStarting ||
          _isHandlingNoShow) {
        return;
      }

      final String? orderId = result['order_id']?.toString();

      final dynamic rawOrderSeq = result['order_seq'];

      final int? orderSeq = rawOrderSeq is int
          ? rawOrderSeq
          : int.tryParse(rawOrderSeq?.toString() ?? '');

      if (orderId == null || orderSeq == null) {
        AppLog.w(
          LogTag.api,
          '조립대 $stationId 응답에 order_id 또는 order_seq 없음: $result',
        );
        return;
      }

      final String? stage = result['stage']?.toString();

      switch (stage) {
        case 'arrived':
          // 같은 주문이면 그대로 둔다. (노쇼 타이머를 다시 시작하지 않는다)
          if (orderId == _calledOrderId) return;

          setState(() {
            _calledOrderId = orderId;
            _orderCallNumber = orderSeq;
          });

          AppLog.i(
            LogTag.state,
            '주문 배정 감지: order=$orderId 대기번호=$orderSeq (조립대 $stationId)',
          );

          // 3분 노쇼 타이머 시작
          _startCallTimers();
          break;

        case 'verified':
        case 'received':
          // 이 단말에서 찍지 않았는데 인증된 주문. (관리자 대신 처리 · 앱 재시작)
          _closeQrScanner('인증된 주문을 감지해 열려 있던 스캐너를 닫음');

          _moveToWaiting(
            orderId: orderId,
            stationId: stationId,
            data: result,
            reason: '인증된 주문 감지 (stage=$stage)',
          );
          break;

        case 'unclaimed':
          // 처음 보는 주문이면 폐기 중인 지난 주문이므로 호출하지 않고 기다린다.
          if (orderId != _calledOrderId) return;

          _stopCallTimers();
          _closeQrScanner('노쇼 처리된 주문을 감지해 열려 있던 스캐너를 닫음');

          _setStep(DeviceStep.noShow, '노쇼 처리된 주문 감지 (stage=unclaimed)');
          break;

        default:
          AppLog.w(LogTag.api, '조립대 $stationId: 처리하지 않는 stage=$stage');
      }
    } on ApiException catch (e) {
      if (!mounted) return;

      if (e.statusCode == 400) {
        // 아직 이 조립대에 주문이 없는 정상적인 대기 상태
        AppLog.d(LogTag.api, '조립대 $stationId: 아직 배정된 주문 없음');

        // 호출 중이던 주문이 서버에서 사라진 경우 (관리자 중단 등)
        if (_calledOrderId != null) {
          AppLog.i(LogTag.state, '호출 중이던 주문 $_calledOrderId 종료 감지');

          _stopCallTimers();
          _closeQrScanner('호출 주문 종료로 열려 있던 스캐너를 닫음');
        }

        setState(() {
          _calledOrderId = null;
          _orderCallNumber = null;
        });

        return;
      }

      AppLog.w(LogTag.api, '조립대 $stationId 주문 조회 오류 (${e.statusCode})');
    } catch (e) {
      // 타임아웃 · 네트워크 오류. 상세 내용은 [API] 로그에 이미 출력됨.
      // finally에서 플래그가 풀리므로 다음 주기에 다시 조회한다.
      AppLog.w(LogTag.api, '조립대 $stationId 주문 조회 실패, 다음 주기에 재시도: $e');
    } finally {
      _isFetchingStationStatus = false;
    }
  }

  // 3. 노쇼 타이머
  void _startCallTimers() {
    _stopCallTimers();

    // 3분 안에 QR 인증이 없으면 노쇼 처리
    _noShowTimer = Timer(_noShowTimeout, _handleNoShow);
  }

  void _stopCallTimers() {
    _noShowTimer?.cancel();
    _noShowTimer = null;
  }

  /// 호출 후 3분 동안 QR 인증이 없으면 주문을 취소하고 조립대를 비운다.
  Future<void> _handleNoShow() async {
    if (_isHandlingNoShow) return;

    _stopCallTimers();

    // QR 인증(start) 요청이 가는 중이면 결과를 기다린다.
    // 성공하면 노쇼 처리를 버리고, 실패하면 그때 처리한다. (_openQrScanner finally)
    if (_isStarting) {
      _noShowPending = true;

      AppLog.w(
        LogTag.timer,
        '노쇼 시간 만료, 그런데 QR 인증(start) 요청 중 → 결과를 보고 처리',
      );
      return;
    }

    final String? orderId = _calledOrderId;
    final String? workstationNumber = _workstationNumber;

    if (orderId == null || workstationNumber == null) {
      AppLog.w(LogTag.timer, '노쇼 타이머 만료, 그런데 호출 중인 주문이 없음');
      return;
    }

    _isHandlingNoShow = true;

    // 스캐너를 열어둔 채 시간이 지난 경우 스캐너를 먼저 닫는다.
    _closeQrScanner('노쇼 처리로 열려 있던 스캐너를 닫음');

    final int stationId = int.parse(workstationNumber);

    AppLog.i(
      LogTag.timer,
      '노쇼 타임아웃(${_noShowTimeout.inMinutes}분) → 주문 취소 요청 '
      'order=$orderId station=$stationId',
    );

    try {
      final result = await _api.cancelStation(
        stationId: stationId,
        orderId: orderId,
      );

      AppLog.i(LogTag.api, '노쇼 취소 완료: $result');

      if (!mounted) return;

      _setStep(DeviceStep.noShow, '노쇼 취소 완료 (unclaim 200)');
    } on ApiException catch (e) {
      AppLog.w(LogTag.api, '노쇼 취소 실패 (unclaim ${e.statusCode})');

      if (!mounted) return;

      if (e.statusCode == 404 || e.statusCode == 409) {
        // 이미 다른 경로로 종료·재배정된 주문이므로 조용히 호출 대기로 복귀
        _reset('노쇼 취소 ${e.statusCode}: 이미 정리된 주문으로 보고 복귀');
      } else {
        _showMessage('주문 취소 처리에 실패했습니다. (${e.statusCode})');

        _setStep(DeviceStep.noShow, '노쇼 취소 실패 (unclaim ${e.statusCode})');
      }
    } catch (e) {
      AppLog.w(LogTag.api, '노쇼 취소 요청 실패: $e');

      if (!mounted) return;

      _showMessage('서버 통신 오류로 주문 취소를 확인하지 못했습니다.');

      _setStep(DeviceStep.noShow, '노쇼 취소 통신 오류');
    } finally {
      _isHandlingNoShow = false;
    }
  }

  // 4. QR 인증
  Future<void> _openQrScanner() async {
    // 버튼 연타로 스캐너가 두 번 열리거나, 인증 중에 다시 스캔하는 것을 막는다.
    if (_isQrScannerOpen || _isStarting) {
      AppLog.d(
        LogTag.qr,
        '스캐너 열기 무시: ${_isQrScannerOpen ? '이미 열려 있음' : 'QR 인증 요청 중'}',
      );
      return;
    }

    if (_orderCallNumber == null || _calledOrderId == null) {
      _showMessage('아직 이 조립대에 배정된 주문이 없습니다.');
      return;
    }

    _isQrScannerOpen = true;

    AppLog.d(LogTag.qr, '스캐너 열림 (호출 주문 $_calledOrderId)');

    final String? qrValue = await Navigator.of(context).push<String>(
      MaterialPageRoute(builder: (context) => const QrScannerScreen()),
    );

    _isQrScannerOpen = false;

    if (!mounted) return;

    // 스캐너를 보는 사이 노쇼 처리 등으로 화면이 바뀐 경우
    if (_currentStep != DeviceStep.orderCall) {
      AppLog.w(LogTag.qr, '스캔 결과 무시: 그사이 화면이 ${_currentStep.name}(으)로 바뀜');
      return;
    }

    if (qrValue == null) {
      AppLog.d(LogTag.qr, '스캐너 닫힘 (스캔 없이 돌아가기)');
      return;
    }

    final String rawQrValue = qrValue.trim();

    final String? scannedOrderId = _extractOrderIdFromQr(rawQrValue);

    AppLog.i(
      LogTag.qr,
      '스캔 완료 raw="$rawQrValue" → order_id=${scannedOrderId ?? '추출 실패'}',
    );

    if (scannedOrderId == null) {
      _setStep(DeviceStep.invalidQr, 'QR에서 order_id를 찾지 못함');
      return;
    }

    if (_calledOrderId != null && scannedOrderId != _calledOrderId) {
      AppLog.w(LogTag.qr, '호출 주문과 불일치: 호출=$_calledOrderId 스캔=$scannedOrderId');

      _showMessage('현재 호출된 주문번호와 일치하지 않는 QR 코드입니다.');
      return;
    }

    if (_workstationNumber == null) {
      AppLog.w(LogTag.state, 'QR 인증 중단: 조립대 번호가 설정되지 않음');
      return;
    }

    final int stationId = int.parse(_workstationNumber!);

    // start 요청이 끝날 때까지 노쇼 처리와 재호출을 보류한다.
    _isStarting = true;
    bool started = false;
    bool recheckStatus = false;

    try {
      // POST /station/{order_id}/start
      // body:
      // {
      //   "station_id": 1
      // }
      final startResult = await _api.startStation(
        orderId: scannedOrderId,
        stationId: stationId,
      );

      started = true;

      // START 성공 응답에 주문 정보가 같이 들어옴
      final String responseOrderId =
          startResult['order_id']?.toString() ?? scannedOrderId;

      AppLog.i(LogTag.api, '조립 시작 성공: order=$responseOrderId');

      if (!mounted) return;

      _moveToWaiting(
        orderId: responseOrderId,
        stationId: stationId,
        data: startResult,
        reason: 'QR 인증 성공 (start 200)',
      );
    } on ApiException catch (e) {
      if (!mounted) return;

      switch (e.statusCode) {
        case 400:
          AppLog.w(LogTag.state, 'start 400: 아직 조립대에 배정되지 않은 주문');

          _showMessage('아직 조립대에 배정되지 않은 주문입니다.');
          break;

        case 403:
          final assignedStationId = e.data?['assigned_station_id'];

          _setStep(
            DeviceStep.wrongWorkstation,
            'start 403: 조립대 ${assignedStationId ?? '?'}에 배정된 주문',
            update: () {
              if (assignedStationId != null) {
                _assignedWorkstationNumber = assignedStationId
                    .toString()
                    .padLeft(2, '0');
              } else {
                _assignedWorkstationNumber = '--';
              }
            },
          );

          break;

        case 404:
          _setStep(DeviceStep.invalidQr, 'start 404: 없거나 만료된 주문');
          break;

        case 409:
          AppLog.w(LogTag.state, 'start 409: 이미 처리되었거나 현재 조립대 주문이 아님');

          final errorMessage = e.data?['error']?.toString() ?? '이미 처리된 주문입니다.';
          final conflictStage = e.data?['stage']?.toString();

          // 관리자가 먼저 인증한 경우라면 안내 없이 다음 화면으로 넘어간다.
          if (conflictStage != 'verified' && conflictStage != 'received') {
            _showMessage(errorMessage);
          }

          recheckStatus = true;
          break;

        default:
          AppLog.w(LogTag.state, '처리되지 않은 start 오류: ${e.statusCode}');

          _showMessage('QR 인증 중 오류가 발생했습니다. (${e.statusCode})');
      }
    } catch (e) {
      AppLog.w(LogTag.api, 'start 요청 실패: $e');

      if (!mounted) return;

      _showMessage('서버 통신 중 오류가 발생했습니다.');
    } finally {
      _isStarting = false;

      // start 요청 중에 노쇼 시간이 끝났던 경우
      if (_noShowPending) {
        _noShowPending = false;

        if (started) {
          AppLog.i(LogTag.timer, 'QR 인증 성공 → 보류했던 노쇼 처리 취소');
        } else if (mounted && _currentStep == DeviceStep.orderCall) {
          AppLog.i(LogTag.timer, 'QR 인증 실패 → 보류했던 노쇼 처리 진행');

          _handleNoShow();
        } else {
          // 404(주문 없음) · 403(다른 조립대) 등으로 이미 다른 화면으로 넘어간 경우
          AppLog.i(
            LogTag.timer,
            'QR 인증 실패 후 화면이 ${_currentStep.name}(으)로 바뀜 → 노쇼 처리 생략',
          );
        }
      }

      if (recheckStatus && mounted) {
        _fetchStationStatus();
      }
    }
  }

  /// 인증된 주문의 부품 도착 대기 화면으로 넘어간다.
  ///
  /// [data]는 start 응답 또는 조립대 조회 응답이다. 둘 다 keycap · colors를 준다.
  void _moveToWaiting({
    required String orderId,
    required int stationId,
    required Map<String, dynamic> data,
    required String reason,
  }) {
    final String mbti = data['keycap']?.toString() ?? '';

    final List<String> colors =
        (data['colors'] as List<dynamic>?)
            ?.map((e) => e.toString())
            .toList() ??
        [];

    AppLog.i(LogTag.state, '주문 정보: order=$orderId keycap=$mbti colors=$colors');

    // 인증이 끝났으므로 노쇼 타이머 중지
    _stopCallTimers();

    _setStep(
      DeviceStep.waiting,
      reason,
      update: () {
        _order = OrderInfo(
          orderId: orderId,
          mbti: mbti,
          colors: colors,
          assignedWorkstation: stationId,
        );
      },
    );
  }

  void _closeQrScanner(String reason) {
    if (!_isQrScannerOpen) return;

    _isQrScannerOpen = false;

    if (mounted) {
      AppLog.d(LogTag.qr, reason);

      Navigator.of(context).pop();
    }
  }

  String? _extractOrderIdFromQr(String qrValue) {
    final value = qrValue.trim();

    if (value.isEmpty) {
      return null;
    }

    // 혹시 QR에 order_id 자체만 들어있는 경우도 허용
    if (value.startsWith('ord_')) {
      return value;
    }

    // 백엔드 QR 형식:
    // https://.../station/ord_xxxxxxxx/start
    final uri = Uri.tryParse(value);

    if (uri == null) {
      return null;
    }

    final segments = uri.pathSegments;

    if (segments.length >= 3 &&
        segments[segments.length - 3] == 'station' &&
        segments.last == 'start') {
      final orderId = segments[segments.length - 2];

      if (orderId.startsWith('ord_')) {
        return orderId;
      }
    }

    return null;
  }

  // 5. 조립 완료
  Future<void> _finishAssembly() async {
    if (_workstationNumber == null) {
      _showMessage('조립대 번호가 설정되지 않았습니다.');
      return;
    }

    final int stationId = int.parse(_workstationNumber!);

    final OrderInfo? order = _order;

    if (order == null) {
      AppLog.w(LogTag.state, '조립 완료 중단: 주문 정보(_order)가 없음');
      _showMessage('주문 정보가 없어 조립 완료를 처리할 수 없습니다.');
      return;
    }

    // 버튼 연타 · 자동 완료가 겹쳐 complete가 두 번 나가는 것을 막는다.
    if (_isCompleting) {
      AppLog.d(LogTag.state, '조립 완료 요청 중복 무시 (이미 요청 중)');
      return;
    }

    _isCompleting = true;

    AppLog.i(
      LogTag.state,
      '조립 완료 요청 station=$stationId order=${order.orderId}',
    );

    try {
      final result = await _api.completeStation(
        stationId: stationId,
        orderId: order.orderId,
      );

      if (!mounted) return;

      if (result['ok'] == true) {
        _setStep(DeviceStep.completed, '조립 완료 (complete 200)');
      } else {
        AppLog.w(LogTag.api, 'complete 200 응답이지만 ok != true: $result');

        _showMessage('조립 완료 응답을 확인할 수 없습니다.');
      }
    } on ApiException catch (e) {
      AppLog.w(LogTag.state, '조립 완료 실패 (complete ${e.statusCode})');

      if (!mounted) return;

      switch (e.statusCode) {
        case 400:
          _showMessage('이 조립대에 진행 중인 주문이 없습니다.');
          break;

        case 409:
          final currentOrderId = e.data?['current_order_id'];

          AppLog.w(
            LogTag.state,
            '주문 불일치: 화면=${order.orderId} 서버=$currentOrderId',
          );

          _showMessage('현재 화면의 주문과 서버의 주문 정보가 일치하지 않습니다.');
          break;

        default:
          _showMessage('조립 완료 처리에 실패했습니다. (${e.statusCode})');
      }
    } catch (e) {
      AppLog.w(LogTag.api, 'complete 요청 실패: $e');

      if (!mounted) return;

      _showMessage('서버 통신 중 오류가 발생했습니다.');
    } finally {
      _isCompleting = false;
    }
  }

  // 6. 원격 업데이트

  /// 주문을 처리하지 않는 한가한 상태. 이때만 업데이트 버튼을 보이고 설치한다.
  bool get _isIdle =>
      _currentStep == DeviceStep.workstationSetup ||
      (_currentStep == DeviceStep.orderCall &&
          _calledOrderId == null &&
          !_isQrScannerOpen &&
          !_isStarting &&
          !_isHandlingNoShow);

  void _startUpdateChecks() {
    if (!UpdateService.enabled) {
      AppLog.d(LogTag.update, '원격 업데이트 꺼짐 (CI 빌드가 아님)');
      return;
    }

    AppLog.i(
      LogTag.update,
      '현재 빌드 ${UpdateService.currentBuild}, '
      '${UpdateService.checkInterval.inMinutes}분마다 새 버전 확인',
    );

    // 켜지자마자는 네트워크가 늦게 붙는 경우가 있어 조금 뒤에 첫 확인
    Future.delayed(const Duration(seconds: 10), _checkForUpdate);

    _updateCheckTimer = Timer.periodic(
      UpdateService.checkInterval,
      (_) => _checkForUpdate(),
    );
  }

  /// 새 빌드가 있으면 백그라운드로 받아 두고 업데이트 버튼을 띄운다.
  Future<void> _checkForUpdate() async {
    if (!mounted || _isCheckingUpdate || _isInstallingUpdate) return;

    _isCheckingUpdate = true;

    try {
      final UpdateInfo? info = await UpdateService.fetchNewer();

      if (!mounted) return;

      if (info == null) {
        AppLog.d(LogTag.update, '최신 빌드 사용 중 (${UpdateService.currentBuild})');

        if (_readyUpdate != null) {
          setState(() {
            _readyUpdate = null;
            _readyUpdateFile = null;
          });
        }

        await UpdateService.clearDownloads();
        return;
      }

      // 같은 빌드를 이미 받아 둔 경우
      if (_readyUpdate?.buildNumber == info.buildNumber &&
          (await _readyUpdateFile?.exists() ?? false)) {
        return;
      }

      AppLog.i(LogTag.update, '새 빌드 발견: ${info.label} → 백그라운드 다운로드');

      // 더 새 빌드가 올라왔으면 이전 빌드 버튼은 내린다.
      if (_readyUpdate != null) {
        setState(() {
          _readyUpdate = null;
          _readyUpdateFile = null;
        });
      }

      final File file = await UpdateService.download(info);

      if (!mounted) return;

      setState(() {
        _readyUpdate = info;
        _readyUpdateFile = file;
      });
    } catch (e) {
      AppLog.w(LogTag.update, '업데이트 확인 · 다운로드 실패, 다음 주기에 재시도: $e');
    } finally {
      _isCheckingUpdate = false;
    }
  }

  /// 업데이트 버튼 → 관리자 비밀번호 → 안드로이드 설치 화면
  Future<void> _handleUpdateTap() async {
    final UpdateInfo? info = _readyUpdate;
    final File? file = _readyUpdateFile;

    if (info == null || file == null || _isInstallingUpdate) return;

    if (!_isIdle) {
      _showMessage('진행 중인 주문이 끝난 뒤 업데이트할 수 있습니다.');
      return;
    }

    final bool? confirmed = await showDialog<bool>(
      context: context,
      // 바깥을 눌러도 닫히지 않는다. [취소] 또는 비밀번호 통과로만 닫힘.
      barrierDismissible: false,
      builder: (_) => const ExitPasswordDialog(title: '업 데 이 트'),
    );

    if (confirmed != true || !mounted) return;

    // 비밀번호를 입력하는 사이 주문이 호출된 경우
    if (!_isIdle) {
      _showMessage('주문이 호출되어 업데이트를 미뤘습니다.');
      return;
    }

    if (!await file.exists()) {
      AppLog.w(LogTag.update, '받아 둔 APK가 사라짐 → 다시 받기');

      setState(() {
        _readyUpdate = null;
        _readyUpdateFile = null;
      });

      _showMessage('업데이트 파일을 다시 받는 중입니다. 잠시 후 다시 시도해 주세요.');

      _checkForUpdate();
      return;
    }

    _isInstallingUpdate = true;

    AppLog.i(
      LogTag.update,
      '업데이트 설치 시작: ${UpdateService.currentBuild} → ${info.label}',
    );

    try {
      // 네이티브에서 화면 고정을 풀고 설치 화면을 연다.
      await UpdateService.install(file);
    } catch (e) {
      AppLog.w(LogTag.update, '설치 화면 열기 실패: $e');

      _showMessage('업데이트 설치 화면을 열지 못했습니다.');
    } finally {
      _isInstallingUpdate = false;
    }
  }

  // 화면 상태 관리

  /// 화면 전환은 모두 이 함수를 거친다.
  ///
  /// `이전 → 다음 (이유)`와 전환 직후의 상태 요약을 터미널에 출력한다.
  /// [update]에는 화면 전환과 함께 바뀌어야 하는 값을 넣는다.
  /// (같은 setState 안에서 적용된다)
  void _setStep(DeviceStep next, String reason, {VoidCallback? update}) {
    final DeviceStep prev = _currentStep;

    if (!mounted) {
      AppLog.w(
        LogTag.state,
        '${prev.name} → ${next.name} 무시: 화면이 이미 닫힘 ($reason)',
      );
      return;
    }

    setState(() {
      update?.call();
      _currentStep = next;
    });

    AppLog.i(LogTag.state, '${prev.name} → ${next.name} ($reason)');
    AppLog.d(LogTag.state, '  ${_stateSnapshot()}');
  }

  /// 현재 상태 한 줄 요약 (디버그용)
  String _stateSnapshot() {
    final List<String> activeTimers = [
      if (_stationStatusTimer?.isActive ?? false) 'stationPolling',
      if (_noShowTimer?.isActive ?? false) 'noShow',
    ];

    return 'station=${_workstationNumber ?? '-'} '
        'called=${_calledOrderId ?? '-'} '
        'seq=${_orderCallNumber ?? '-'} '
        'order=${_order?.orderId ?? '-'} '
        'assigned=${_assignedWorkstationNumber ?? '-'} '
        'scanner=${_isQrScannerOpen ? 'open' : 'closed'} '
        'noShowHandling=$_isHandlingNoShow '
        'starting=$_isStarting noShowPending=$_noShowPending '
        'completing=$_isCompleting '
        'timers=[${activeTimers.join(', ')}]';
  }

  /// 테스트 메뉴(DemoMenu)로 강제 이동.
  ///
  /// 주문 정보가 필요한 화면은 _order가 없으면 build의 `_order!`에서
  /// 바로 오류가 나므로 이동을 막는다.
  void _demoMoveTo(DeviceStep step) {
    const Set<DeviceStep> needsOrder = {
      DeviceStep.waiting,
      DeviceStep.authenticated,
      DeviceStep.assembling,
      DeviceStep.completed,
    };

    if (needsOrder.contains(step) && _order == null) {
      AppLog.w(
        LogTag.demo,
        '${step.name} 이동 취소: 주문 정보(_order)가 없음 → QR 인증을 한 번 거친 뒤 사용',
      );
      _showMessage('주문 정보가 없어 이 화면을 열 수 없습니다. (QR 인증 후 사용)');
      return;
    }

    AppLog.w(LogTag.demo, '테스트 메뉴로 강제 이동: ${step.name}');

    _setStep(step, '테스트 메뉴');
  }

  void _reset(String reason) {
    _stopStationStatusPolling();
    _stopCallTimers();

    _setStep(
      DeviceStep.orderCall,
      reason,
      update: () {
        _assignedWorkstationNumber = null;

        _orderCallNumber = null;
        _calledOrderId = null;

        // 이전 주문 정보가 남아 있지 않게 비운다.
        _order = null;
      },
    );

    _startStationStatusPolling();
  }

  // UI 보조
  void _showMessage(String message) {
    if (!mounted) return;

    ScaffoldMessenger.of(
      context,
    ).showSnackBar(SnackBar(content: Text(message)));
  }

  /// 상단 왼쪽 로고를 2초 안에 7번 눌렀을 때.
  /// 비밀번호가 맞으면 앱을 종료한다.
  Future<void> _handleExitRequest() async {
    final bool? confirmed = await showDialog<bool>(
      context: context,
      // 바깥을 눌러도 닫히지 않는다. [취소] 또는 비밀번호 통과로만 닫힘.
      barrierDismissible: false,
      builder: (_) => const ExitPasswordDialog(),
    );

    if (confirmed != true) {
      return;
    }

    AppLog.i(LogTag.app, '관리자 비밀번호 확인 → 화면 고정 해제 후 앱 종료');

    _isExiting = true;

    // 고정을 먼저 풀어야 앱이 정상적으로 내려간다.
    await KioskService.stop();

    await SystemNavigator.pop();
  }

  void _showStaffDialog() {
    showDialog(
      context: context,
      builder: (context) {
        return AlertDialog(
          title: const Text('직원 호출 완료'),
          content: const Text('잠시만 기다려 주세요.'),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('확인'),
            ),
          ],
        );
      },
    );
  }

  Color _headerStatusColor(DeviceStep step) => switch (step) {
    DeviceStep.assembling => AppColors.yellow,
    DeviceStep.invalidQr ||
    DeviceStep.wrongWorkstation ||
    DeviceStep.noShow => AppColors.red,
    DeviceStep.workstationSetup => AppColors.border,
    _ => AppColors.green,
  };

  // 빌드
  @override
  Widget build(BuildContext context) {
    Widget screen;

    switch (_currentStep) {
      case DeviceStep.workstationSetup:
        screen = WorkstationSetupScreen(onSelected: _selectWorkstation);
        break;

      case DeviceStep.orderCall:
        screen = OrderCallScreen(
          orderNumber: _orderCallNumber == null
              ? '조립대 ${_workstationNumber ?? '--'}'
              : _orderCallNumber.toString().padLeft(2, '0'),
          onQrScan: _openQrScanner,
        );
        break;

      case DeviceStep.waiting:
        screen = WaitingScreen(
          workstationNumber: _workstationNumber ?? '--',
          orderId: _order!.orderId,
          onReceived: () {
            _setStep(DeviceStep.assembling, '부품 도착 (stage=received)');
          },
        );
        break;

      case DeviceStep.authenticated:
        screen = AuthenticatedScreen(
          mbti: _order!.mbti,
          colors: _order!.colors,
          onStart: () {
            _setStep(DeviceStep.assembling, '인증 화면에서 조립 시작');
          },
        );
        break;

      case DeviceStep.assembling:
        screen = AssemblingScreen(
          mbti: _order!.mbti,
          colors: _order!.colors,
          orderNumber: _orderCallNumber?.toString().padLeft(2, '0'),
          onComplete: _finishAssembly,
        );
        break;

      case DeviceStep.completed:
        screen = CompletedScreen(
          mbti: _order!.mbti,
          colors: _order!.colors,
          onRestart: () => _reset('조립 완료 화면 종료'),
        );
        break;

      case DeviceStep.invalidQr:
        screen = InvalidQrScreen(
          onRetry: () => _reset('QR 오류 화면 종료'),
          onCallStaff: _showStaffDialog,
        );
        break;

      case DeviceStep.noShow:
        screen = NoShowScreen(
          orderNumber: _orderCallNumber?.toString().padLeft(2, '0'),
          onAutoReturn: () => _reset('노쇼 화면 종료'),
          onCallStaff: _showStaffDialog,
        );
        break;

      case DeviceStep.wrongWorkstation:
        screen = WrongWorkstationScreen(
          currentWorkstation: _workstationNumber ?? '--',
          assignedWorkstation: _assignedWorkstationNumber ?? '--',
          onAutoReturn: () => _reset('조립대 불일치 화면 종료'),
        );
        break;
    }

    return Scaffold(
      body: SafeArea(
        child: Column(
          children: [
            /// 모든 화면 위에 항상 표시되는 로고 바
            /// 왼쪽 로고를 2초 안에 7번 누르면 관리자 종료 창이 뜬다.
            AppTopBar(onSecretTap: _handleExitRequest),

            Expanded(
              child: Stack(
                children: [
                  Positioned.fill(child: screen),

                  if (_currentStep != DeviceStep.workstationSetup &&
                      _workstationNumber != null)
                    Positioned(
                      top: 20,
                      left: 40,
                      child: WorkstationHeader(
                        workstationNumber: _workstationNumber!,
                        statusColor: _headerStatusColor(_currentStep),
                      ),
                    ),

                  /// 새 버전을 받아 뒀고 한가할 때만 보이는 업데이트 버튼
                  if (_readyUpdate != null && _isIdle)
                    Positioned(
                      top: 20,
                      right: 40,
                      child: UpdateBadge(
                        versionLabel: _readyUpdate!.label,
                        onTap: _handleUpdateTap,
                      ),
                    ),

                  /// 화면 확인용 테스트 메뉴
                  Positioned(
                    right: 24,
                    bottom: 24,
                    child: DemoMenu(
                      onWaiting: () => _demoMoveTo(DeviceStep.waiting),
                      onAuthenticated: () =>
                          _demoMoveTo(DeviceStep.authenticated),
                      onAssembling: () => _demoMoveTo(DeviceStep.assembling),
                      onCompleted: () => _demoMoveTo(DeviceStep.completed),
                      onInvalidQr: () => _demoMoveTo(DeviceStep.invalidQr),
                      onWrongWorkstation: () =>
                          _demoMoveTo(DeviceStep.wrongWorkstation),
                      onNoShow: () => _demoMoveTo(DeviceStep.noShow),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
