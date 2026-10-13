import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:animations/animations.dart';
import 'package:http/http.dart' as http;
import 'package:uuid/uuid.dart';
import 'package:printing/printing.dart' show Printing;

import 'theme/app_theme.dart';
import 'services/api_service.dart';
// [신규] 플랫폼별 창 제어 분기(조건부 임포트).
// - 웹(Chrome) 빌드 : kiosk_window_web.dart (전부 no-op)
// - 그 외 모든 빌드  : kiosk_window_io.dart  (window_manager 기존 로직)
// window_manager 는 웹을 지원하지 않고 내부적으로 dart:io 를 쓰기 때문에
// 직접 import 하면 웹 빌드가 컴파일되지 않습니다.
import 'services/kiosk_window_web.dart'
    if (dart.library.io) 'services/kiosk_window_io.dart';

import 'services/receipt_printer_service.dart';
import 'screens/home_screen.dart';
import 'screens/mbti_choice_screen.dart';
import 'screens/mbti_quiz_screen.dart';
import 'screens/mbti_manual_screen.dart';
import 'screens/mbti_result_screen.dart';
import 'screens/board_select_screen.dart';
import 'screens/keycap_fill_screen.dart';
import 'screens/complete_screen.dart';
import 'screens/design_confirm_screen.dart';
import 'screens/receipt_screen.dart';

// [디버그/키오스크용] 개발 중(flutter run)에는 전체화면+최상단 고정이
// VS Code/터미널을 가려서 오히려 불편하므로 기본은 꺼둡니다.
// 실제 키오스크에 배포하는 release 빌드에서만 true로 바꿔서 쓰세요.
// [항상 켜짐] 하단 "사물인터넷혁신융합대학사업단" 로고를 5초 안에 7번 눌러
// 종료할 수 있는 숨겨진 동작이 마련되어 있으므로, 개발 중(flutter run)에도
// 실제 키오스크와 동일한 전체화면 환경에서 테스트할 수 있도록 항상 true로 둡니다.
const bool kKioskWindowMode = true;

// [운영진 전용] 종료 확인창에서 입력해야 하는 비밀번호.
// ⚠️ 실제 배포 전에 원하는 번호로 반드시 바꾸세요.
const String kExitPin = '2026';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // 창 크기/전체화면/최상단 고정 설정. 데스크톱에서는 기존과 동일하게
  // 동작하고, 웹에서는 아무것도 하지 않습니다.
  await initKioskWindow(kioskMode: kKioskWindowMode);

  runApp(const ClickyKeyringApp());
}

// [수정] 팀 확정: 판(케이스) "크기" 선택은 없어지고 1×4로 고정되지만,
// 판 "색상"을 고르는 화면은 이제 항상 노출됩니다(더 이상 건너뛰지 않음).
const bool kBoardSelectEnabled = true;

// [수정] 축(스위치)은 제품에서 완전히 제외되었습니다. 화면과 API 모두에서
// 축 관련 처리를 뺐습니다. axis_select_screen.dart 파일 자체는 남겨뒀지만
// 더 이상 이 흐름에서 진입하지 않습니다.
const bool kAxisSelectEnabled = false;

// [복구] UI/UX 확인이 끝나 다시 실제 서버(재고 조회/주문 생성/상태 폴링)에 연결합니다.
//
// (참고) false로 두면 서버에 전혀 접속하지 않고, 재고는 "품절 없음", 주문은 가짜
// 접수로 처리해서 영수증 화면까지 바로 갑니다. 프린터만 따로 확인할 때 쓰며,
// 이때는 실제 주문이 만들어지지 않고 화면 위쪽에 "서버 미연결 테스트 모드"
// 표시가 나옵니다. 행사용 exe는 반드시 true여야 합니다.
const bool kApiEnabled = true;

// [임시/프린터 연동 확인용] 서버가 주문을 실제로 만들지 못해(대기열 가득 등)
// 영수증 화면이 "대기 중"으로 뜨는 경우에도 영수증을 출력합니다.
// 백엔드/조립대 작업이 끝나서 주문이 정상적으로 만들어지면 false로 바꾸세요.
// (true인 채로 행사에 나가면 주문이 안 만들어진 손님에게도 주문번호 '-'와
// QR 없음 안내만 찍힌 영수증이 나와서 종이만 낭비됩니다.)
// [복구] 프린터 자체는 실제 기기에서 2회 출력 성공으로 확인이 끝났으므로
// false로 되돌립니다. 만약 실제 QR 주문으로 영수증이 나오는 것까지는 아직
// 확인 전이라면, 그 확인이 끝나기 전까지만 다시 true로 돌려서 쓰세요.
const bool kPrintReceiptWithoutOrder = false;

// [임시/프린터 연동 확인용] 콘솔을 볼 수 없는 exe 실행에서도 영수증 출력이
// 됐는지/안 됐다면 왜인지 화면 우측 상단에 표시합니다.
// 영수증 화면에서 출력하고, 처음 화면으로 돌아온 뒤에도 다음 주문을 시작할
// 때까지 남아있습니다. 확인이 끝나면 false로 바꾸고 exe를 다시 빌드하세요.
// (false면 화면에 아무것도 표시되지 않고 원래 화면 그대로입니다.)
// [복구] 프린터 점검이 끝났으므로 false로 되돌립니다. 행사 중 다시 점검이
// 필요하면 true로 바꿔서 다시 빌드하세요.
const bool kShowPrintDebugOverlay = false;

enum AppStep {
  home,
  mbtiChoice,
  mbtiQuiz,
  mbtiManual,
  mbtiResult,
  boardSelect,
  axisSelect,
  keycapFill,
  complete, // [보류] 로봇 조립 애니메이션 — 현재 플로우에서는 사용하지 않음(팀 논의 후 결정). 코드는 보존.
  designConfirm, // [신규] STEP 06 — 완성된 디자인 확인 화면 (Enter=접수 / Esc=초기화 후 STEP01)
  receipt, // [신규] 영수증 화면 (표시 전용, STEP 번호 없음, 8초 후 자동 복귀)
}

class ClickyKeyringApp extends StatelessWidget {
  const ClickyKeyringApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '딸깍 - Clicky Keyring Studio',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.themeData,
      home: const AppRoot(),
    );
  }
}

class AppRoot extends StatefulWidget {
  const AppRoot({super.key});

  @override
  State<AppRoot> createState() => _AppRootState();
}

class _AppRootState extends State<AppRoot> {
  final FocusNode _focusNode = FocusNode();
  final ApiService _api = ApiService();

  // [키오스크 종료용] 화면 하단 푸터의 "사물인터넷혁신융합대학사업단" 로고와
  // 글씨를 5초 안에 7번 이상 연속 터치하면 종료 확인창(비밀번호 입력)이 뜹니다.
  // 관람객은 우연히 찾기 어렵지만, 운영진은 알고 있으면 키보드 없이도 종료할 수
  // 있는 숨겨진 동작입니다.
  // [변경] 예전에는 "우측 하단 투명 구석을 3초 안에 5번"이었는데, 아무것도 없는
  // 구석이라 운영진도 위치를 찾기 어려워서 눈에 보이는 사업단 로고로 옮기고,
  // 대신 우연히 눌릴 확률을 낮추려고 조건을 7회 / 5초로 늘렸습니다.
  static const int _exitTapTarget = 7;
  static const Duration _exitTapWindow = Duration(seconds: 5);

  int _exitTapCount = 0;
  DateTime? _firstExitTapTime;

  // [수정] 예전에는 5번 터치하면 확인 없이 바로 종료됐는데, 관람객이
  // 우연히 여러 번 두드려서 전시 중에 앱이 갑자기 꺼지는 사고를 막기 위해
  // "종료하시겠습니까?" 확인 + 비밀번호 입력 단계를 추가했습니다.
  bool _showExitDialog = false;
  String _exitPinInput = '';
  String? _exitPinError;
  Timer? _exitDialogTimeoutTimer;
  static const Duration _exitDialogTimeout = Duration(seconds: 25);

  /// 하단 푸터의 사업단 로고/글씨를 탭할 때마다 호출됩니다.
  /// 첫 탭으로부터 [_exitTapWindow](5초) 안에 [_exitTapTarget](7회) 이상
  /// 눌리면 종료 확인창을 엽니다. 5초가 지나면 카운트가 처음부터 다시 셉니다.
  void _handleExitBrandTap() {
    final now = DateTime.now();
    if (_firstExitTapTime == null ||
        now.difference(_firstExitTapTime!) > _exitTapWindow) {
      _firstExitTapTime = now;
      _exitTapCount = 1;
    } else {
      _exitTapCount++;
    }
    if (_exitTapCount >= _exitTapTarget) {
      _exitTapCount = 0;
      _firstExitTapTime = null;
      _openExitDialog();
    }
  }

  void _openExitDialog() {
    _exitDialogTimeoutTimer?.cancel();
    setState(() {
      _showExitDialog = true;
      _exitPinInput = '';
      _exitPinError = null;
    });
    // 안전장치: 아무도 "아니오"를 안 눌러도 일정 시간 뒤 자동으로 닫힘
    _exitDialogTimeoutTimer = Timer(_exitDialogTimeout, () {
      if (mounted) _closeExitDialog();
    });
  }

  void _closeExitDialog() {
    _exitDialogTimeoutTimer?.cancel();
    if (!mounted) return;
    setState(() {
      _showExitDialog = false;
      _exitPinInput = '';
      _exitPinError = null;
    });
  }

  // 키패드 입력 처리. '*' = 지우기, '#' = 확인, 그 외(0~9) = 숫자 입력.
  void _onExitKeypadTap(String key) {
    _exitDialogTimeoutTimer?.cancel();
    _exitDialogTimeoutTimer = Timer(_exitDialogTimeout, () {
      if (mounted) _closeExitDialog();
    });

    if (key == '*') {
      setState(() {
        _exitPinInput = '';
        _exitPinError = null;
      });
      return;
    }

    if (key == '#') {
      if (_exitPinInput == kExitPin) {
        closeKioskWindow();
      } else {
        setState(() {
          _exitPinInput = '';
          _exitPinError = '비밀번호가 틀렸습니다';
        });
      }
      return;
    }

    // 숫자 키
    setState(() {
      _exitPinError = null;
      _exitPinInput += key;
    });
  }

  AppStep _step = AppStep.home;

  // 화면 전환 방향. 1 = 앞으로(오른쪽에서 슬라이드 인),
  // -1 = 뒤로가기(왼쪽에서 슬라이드 인). 피그마의 `d`(direction) 값과 대응됩니다.
  int _direction = 1;

  // ---------------- MBTI 검사 (4지선다 모드) ----------------
  static const List<Map<String, dynamic>> _mbtiQuestions = [
    {
      'question': '주말에 에너지를 얻는 방법은?',
      'options': [
        {'text': '친구들과 왁자지껄하게 놀기', 'letter': 'E'},
        {'text': '새로운 사람들과 어울리기', 'letter': 'E'},
        {'text': '혼자 조용히 쉬기', 'letter': 'I'},
        {'text': '소수의 친한 친구와 시간 보내기', 'letter': 'I'},
      ],
    },
    {
      'question': '새로운 정보를 받아들일 때 나는?',
      'options': [
        {'text': '전체적인 흐름과 가능성을 먼저 본다', 'letter': 'N'},
        {'text': '떠오르는 아이디어와 상상을 즐긴다', 'letter': 'N'},
        {'text': '구체적인 사실과 세부사항을 본다', 'letter': 'S'},
        {'text': '경험하고 검증된 것을 믿는다', 'letter': 'S'},
      ],
    },
    {
      'question': '결정을 내릴 때 나는?',
      'options': [
        {'text': '사람들의 감정과 관계를 먼저 고려한다', 'letter': 'F'},
        {'text': '공감과 조화를 중요하게 생각한다', 'letter': 'F'},
        {'text': '논리와 원칙을 기준으로 판단한다', 'letter': 'T'},
        {'text': '객관적인 사실에 따라 결정한다', 'letter': 'T'},
      ],
    },
    {
      'question': '일정을 관리할 때 나는?',
      'options': [
        {'text': '미리 계획을 세우고 그대로 실행한다', 'letter': 'J'},
        {'text': '정리하고 마감을 철저히 지킨다', 'letter': 'J'},
        {'text': '즉흥적으로 상황에 맞춰 움직인다', 'letter': 'P'},
        {'text': '유연하게 계획을 바꾸는 걸 좋아한다', 'letter': 'P'},
      ],
    },
  ];
  int _quizIndex = 0;
  List<String?> _quizAnswers = List<String?>.filled(4, null);

  // ---------------- MBTI 직접 입력(수동) 모드 ----------------
  // I/E, N/S, F/T, P/J 순서
  static const List<List<String>> _manualPairs = [
    ['I', 'E'],
    ['N', 'S'],
    ['F', 'T'],
    ['P', 'J'],
  ];
  int _manualIndex = 0;
  List<String?> _manualAnswers = List<String?>.filled(4, null);

  String? _mbtiResult; // 예: 'ISTJ'

  // ---------------- 본판 / 판 색상 ----------------
  String? _boardShape; // '1x4' 고정 (2x2 옵션은 팀 확정으로 제거됨)
  int _boardCount = 4;
  // [신규] STEP 04에서 고른 판(케이스) 색상 코드: 'g'|'y'|'b'|'r'
  // 'r'은 화면에는 핑크로 보이지만 코드/백엔드 전송 값은 그대로 'r'/'red'.
  String? _boardColorCode;

  // [삭제] 축(스위치)은 제품에서 완전히 제외됨

  // ---------------- 품절 재고 ----------------
  Set<String> _soldOutBoards = {};
  Set<String> _soldOutKeycaps = {};
  // Set<String> _soldOutAxes = {};
  // Set<String> _soldOutSwitches = {};

  bool _stockLoading = false;
  String? _stockError;

  // 키캡 색상 선택 화면 안내 문구
  String? _keycapMessage;

  // 디자인 확인 화면에서 수정/재고 안내 문구
  String? _designConfirmMessage;

  // ---------------- 키캡 색상 ----------------
  late List<String> _letters;
  late List<String?> _colorCodes; // 슬롯별 색상 코드. null = 아직 색 없음(빈 칸)
  int _cursor = 0;

  String? _orderId;
  Map<String, dynamic>? _orderStatus;
  bool _polling = false;
  Timer? _autoRestartTimer;

  // ---------------- 중복 주문 방지 ----------------
  // "한 번의 주문 시도"마다 하나씩 갖는 idempotency 키.
  // - [주문하기](Enter) 버튼을 누르는 순간 생성
  // - 응답을 못 받아서(타임아웃/네트워크 오류) 자동 재시도할 때는 재사용
  // - 사용자가 뒤로 가거나(Esc) 처음부터 다시 시작하면 다음 시도에서 새로 생성
  String? _orderAttemptId;
  // 주문 요청이 서버에 나가 있는 동안(응답 대기 중) true.
  // true인 동안은 디자인 확인 화면에서 Enter/Esc 입력을 모두 무시해서
  // 중복 클릭으로 같은 주문이 두 번 나가는 것을 막습니다.
  bool _submittingOrder = false;

  // ---------------- 대기열 현황 ----------------
  // [신규] 시작 화면에 머무는 동안 GET /queue/status 를 계속 조회해서,
  // 대기열이 가득 찬 상태면 주문을 "시작조차" 못 하게 막습니다.
  // 다 만들고 나서 마지막에 거절당하는 것보다, 시작 전에 알려주는 쪽이
  // 손님 입장에서 훨씬 낫기 때문입니다.
  static const Duration _queuePollInterval = Duration(seconds: 1);
  Timer? _queuePollTimer;
  // 응답이 늦어질 때 요청이 겹쳐서 쌓이지 않도록 하는 잠금.
  bool _queuePollInFlight = false;
  // 서버 응답의 full 값.
  //   null  — 아직 한 번도 못 받음(또는 조회 실패). 판단 보류 = 시작 허용.
  //   true  — 가득 참. 시작하기를 막고 안내 문구를 띄웁니다.
  //   false — 여유 있음. 평소대로 동작.
  bool? _queueFull;

  // ---------------- 영수증 화면 표시용 값 ----------------
  String? _receiptOrderNumber;
  String? _receiptTime;
  Uint8List? _receiptQrBytes; // GET /order/{order_id}/qr 로 받아온 실제 QR 이미지

  // [임시/프린터 연동 확인용] 화면 우측 상단에 띄울 출력 결과. (kShowPrintDebugOverlay)
  String? _printDebugMessage;
  bool? _printDebugOk; // null = 출력 중, true = 성공, false = 실패
  // 출력 작업 번호. 새 주문을 시작하면 올려서, 늦게 끝난 이전 출력의 결과가
  // 다음 손님 화면에 뜨지 않게 합니다.
  int _printJobSeq = 0;

  // [수정] 팀 논의로 축(스위치)을 제품에서 아예 제외하기로 하면서, 화면에
  // 축 이름/색을 표시할 일이 없어져 이 매핑은 삭제했습니다. (기존에는
  // 여기서 _axisLabels/_axisColors로 디자인 확인·영수증 화면에 표시했음)

  // 영수증에 표시할 키캡 색상 이름 (keycap_fill_screen.dart의 범례와 동일)
  // [수정] 'r' 코드는 그대로 유지, 표시 이름만 빨강→핑크로 변경
  static const Map<String, String> _keycapColorLabels = {
    'g': '초록',
    'y': '노랑',
    'b': '파랑',
    'r': '핑크',
  };

  // [신규] 판(케이스) 색상 코드 -> 백엔드에 보낼 영문 색상 이름.
  // Mobius cnt_order 스펙: board는 "red"|"yellow"|"green"|"blue" 중 하나.
  // 'r' 코드는 화면상 핑크로 보이지만 백엔드에는 그대로 'red'로 보냅니다.
  static const Map<String, String> _boardColorWords = {
    'g': 'green',
    'y': 'yellow',
    'b': 'blue',
    'r': 'red',
  };
  String _boardColorWord(String code) => _boardColorWords[code] ?? 'green';

  // Mobius/창고/조립대 콜백이 아직 실제로 연결 안 된 동안의 임시 안전장치.
  // 이 시간 안에 'done'이 안 되면 자동으로 홈 화면으로 돌아감.
  static const Duration _stuckTimeout = Duration(seconds: 25);
  // 완성(done) 후 자동으로 처음 화면으로 돌아가기까지의 대기 시간
  static const Duration _autoRestartAfterDone = Duration(seconds: 8);
  Timer? _doneRestartTimer;

  // 색상 코드 <-> 실제 색상. keycap_fill_screen.dart의 범례(KeycapColors)와
  // 동일한 팔레트를 써야 보드판/완성 화면에 칠해지는 색이 범례와 일치합니다.
  static const List<String> _pastelColorCycle = ['g', 'y', 'b', 'r'];
  static const Map<String, Color> _colorMap = {
    'g': KeycapColors.green,
    'y': KeycapColors.yellow,
    'b': KeycapColors.blue,
    'r': KeycapColors.red,
  };

  @override
  void initState() {
    super.initState();
    _resetLetters();
    WidgetsBinding.instance.addPostFrameCallback(
      (_) => _focusNode.requestFocus(),
    );
    // [신규] 시작 화면 대기열 현황 폴링 시작(앱이 살아있는 동안 계속 돕니다).
    _startQueuePolling();
  }

  void _resetLetters() {
    _letters = List.filled(_boardCount, '');
    _colorCodes = List<String?>.filled(_boardCount, null);
    _cursor = 0;
  }

  void _resetQuiz() {
    _quizIndex = 0;
    _quizAnswers = List<String?>.filled(4, null);
  }

  void _resetManual() {
    _manualIndex = 0;
    _manualAnswers = List<String?>.filled(4, null);
  }

  //------------ 대기열 현황 폴링 -------------
  // 타이머 하나를 앱 시작부터 끝까지 계속 돌리되, 실제 요청은 시작 화면일
  // 때만 내보냅니다. 이렇게 하면 _restart()/_goBack() 등 홈으로 돌아오는
  // 모든 경로에 폴링 시작/중지 코드를 일일이 끼워 넣지 않아도 됩니다.
  void _startQueuePolling() {
    // [임시] API 연동이 꺼져있으면 서버를 호출하지 않습니다.
    if (!kApiEnabled) return;

    _queuePollTimer?.cancel();
    _queuePollTimer = Timer.periodic(_queuePollInterval, (_) async {
      if (!mounted) return;
      // 시작 화면이 아닐 때는 건너뜁니다(주문 중에는 /order/{id}/status 폴링이
      // 따로 돌고 있고, 대기열 현황은 그때 쓸 데가 없습니다).
      if (_step != AppStep.home) return;
      // 앞 요청이 아직 안 끝났으면 이번 차례는 거릅니다.
      if (_queuePollInFlight) return;

      _queuePollInFlight = true;
      try {
        final full = await _api.isQueueFull();
        if (!mounted) return;
        // null은 "조회 실패 = 판단 보류"라는 뜻이므로 직전 값을 그대로 둡니다.
        // 네트워크가 잠깐 끊겼다고 시작하기를 막아버리면 안 되기 때문입니다.
        if (full != null && full != _queueFull) {
          setState(() => _queueFull = full);
        }
      } finally {
        _queuePollInFlight = false;
      }
    });
  }

  //------------ 재고 조회 함수 -------------
  // [수정] scope로 화면 맥락을 넘기면, 실제 요청은 항상 전체(board+keycap)를
  // 받아오지만 콘솔 로그는 그 화면에서 의미 있는 부분만 찍습니다.
  // (서버 /stock/out 자체가 board/keycap을 한 번에 같이 주는 단일
  // 엔드포인트라 요청을 나눠 보낼 수는 없습니다 — Mobius cnt_stock 참고)
  //   'board'  : 판 색상 선택 화면에서 호출 -> board만 로그
  //   'keycap' : 키캡(MBTI 글자) 관련 화면에서 호출 -> keycap만 로그
  //   'all'    : 어느 한쪽으로 좁힐 수 없는 경우(첫 진입, 최종 제출 전) -> 전체 로그
  Future<bool> _loadSoldOutStock({String scope = 'all'}) async {
    if (_stockLoading) return false;

    // [임시] API 연동이 꺼져있으면 실제 서버를 호출하지 않고
    // "품절 없음"으로 간주해 즉시 성공 처리합니다. (프론트 동작 확인용)
    if (!kApiEnabled) {
      setState(() {
        _soldOutBoards = {};
        _soldOutKeycaps = {};
        _stockLoading = false;
        _stockError = null;
      });
      return true;
    }

    setState(() {
      _stockLoading = true;
      _stockError = null;
    });

    try {
      print('>>> 재고 조회 시작($scope)'); // 테스트 시 터미널 확인용
      final stock = await _api.getSoldOutStock();

      switch (scope) {
        case 'board':
          print('>>> 재고 조회 성공(board): ${stock['board']}');
          break;
        case 'keycap':
          print('>>> 재고 조회 성공(keycap): ${stock['keycap']}');
          break;
        default:
          print('>>> 재고 조회 성공: $stock');
      }

      if (!mounted) return false;

      setState(() {
        _soldOutBoards = Set<String>.from(stock['board'] ?? const <String>{});
        _soldOutKeycaps = Set<String>.from(stock['keycap'] ?? const <String>{});
        _stockLoading = false;
      });

      return true;
    } catch (e) {
      print('>>> 재고 조회 실패: $e'); // 테스트 시 터미널 확인용

      if (!mounted) return false;

      setState(() {
        _stockLoading = false;
        _stockError = e.toString();
      });

      return false;
    }
  }

  // 재고를 확인 후 화면 변경
  Future<bool> _moveToStep(AppStep nextStep, {VoidCallback? beforeMove}) async {
    final success = await _loadSoldOutStock();

    if (!mounted || !success) {
      // 재고 조회 실패 시 현재 화면 유지
      return false;
    }

    setState(() {
      beforeMove?.call();
      _step = nextStep;
    });

    // 화면 이동 후 키보드 포커스 다시 요청
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) {
        _focusNode.requestFocus();
      }
    });

    return true;
  }

  // 주문 시작 전 품절 재고 불러오고,
  // 조회 성공 시에만 MBTI 선택 화면으로 넘어가게 하는 함수
  Future<void> _startOrder() async {
    final success = await _loadSoldOutStock();

    if (!mounted) return;

    if (!success) {
      return;
    }

    setState(() {
      _step = AppStep.mbtiChoice;
      _printJobSeq++; // [프린터 점검용] 이전 출력 결과는 여기서 지움
      _printDebugMessage = null;
      _printDebugOk = null;
    });
  }

  // ------------- MBTI 키캡 글자 별 전체 품절 판단 함수 -----------
  bool _isLetterSoldOut(String letter) {
    const colors = ['r', 'g', 'b', 'y'];

    return colors.every(
      (color) => _soldOutKeycaps.contains('${letter}_$color'),
    );
  }

  // 특정 글자와 색상의 품절 여부
  bool _isKeycapColorSoldOut(String letter, String colorCode) {
    return _soldOutKeycaps.contains('${letter}_$colorCode');
  }

  // 현재 커서에 있는 글자의 품절 색상 집합
  Set<String> _soldOutColorsAtCursor() {
    if (_letters.isEmpty || _cursor < 0 || _cursor >= _letters.length) {
      return {};
    }

    final letter = _letters[_cursor];

    return _pastelColorCycle
        .where((color) => _isKeycapColorSoldOut(letter, color))
        .toSet();
  }

  // 현재 글자의 모든 색상이 품절인지 확인하는 함수
  bool _areAllColorsSoldOutAtCursor() {
    return _soldOutColorsAtCursor().length == _pastelColorCycle.length;
  }

  // 현재 선택된 키캡 색상 중 새로 품절된 항목을 해제
  // 반환값: 처음 발견된 품절 키캡의 인덱스
  int? _removeInvalidColorSelections() {
    int? firstInvalidIndex;

    for (int i = 0; i < _colorCodes.length; i++) {
      final selectedColor = _colorCodes[i];

      // 아직 색상을 선택하지 않은 칸은 검사하지 않음
      if (selectedColor == null) continue;

      final letter = _letters[i];
      final stockCode = '${letter}_$selectedColor';

      if (_soldOutKeycaps.contains(stockCode)) {
        _colorCodes[i] = null;
        firstInvalidIndex ??= i;
      }
    }

    return firstInvalidIndex;
  }

  Future<bool> _refreshKeycapStockForCursor() async {
    final success = await _loadSoldOutStock(scope: 'keycap');

    if (!mounted || !success) {
      return false;
    }

    setState(() {
      // 선택한 색상이 재고 조회 중 품절됐다면 해제
      final invalidIndex = _removeInvalidColorSelections();

      if (invalidIndex != null) {
        _cursor = invalidIndex;
        _keycapMessage = '선택한 부품의 재고가 변경되었습니다. 색상을 다시 선택해 주세요.';
        return;
      }

      // 현재 글자의 모든 색상이 품절된 경우
      if (_areAllColorsSoldOutAtCursor()) {
        _keycapMessage = '선택 가능한 색상이 없습니다. 이전 단계로 돌아가 다른 MBTI를 선택해 주세요.';
      } else {
        _keycapMessage = null;
      }
    });

    return true;
  }

  // [삭제] 축(스위치) 품절 판단 함수 — 축 자체가 제품에서 제외됨

  // 색이 아직 없는 슬롯은 빈 칸(회색)으로 표시
  Color _colorAt(int index) {
    final code = _colorCodes[index];
    return code != null ? _colorMap[code]! : AppColors.tileEmpty;
  }

  // 제출 시점엔 모든 슬롯이 채워져 있어야 하지만, 혹시 몰라 기본값(g) 방어
  String _colorCode(int index) => _colorCodes[index] ?? _pastelColorCycle.first;

  void _goBack() {
    setState(() {
      _direction = -1; // 뒤로가기는 항상 -1
      switch (_step) {
        case AppStep.mbtiChoice:
          _step = AppStep.home;
          break;
        case AppStep.mbtiQuiz:
          _resetQuiz();
          _step = AppStep.mbtiChoice;
          break;
        case AppStep.mbtiManual:
          _resetManual();
          _step = AppStep.mbtiChoice;
          break;
        case AppStep.mbtiResult:
          _step = AppStep.mbtiChoice;
          break;
        case AppStep.boardSelect:
          _step = AppStep.mbtiResult;
          break;
        case AppStep.axisSelect:
          // [삭제] 축(스위치) 화면은 더 이상 진입하지 않는 죽은 코드지만,
          // 혹시 모를 재진입에 대비해 판 색상 선택으로 되돌림
          _boardColorCode = null;
          _boardShape = null;
          _step = AppStep.boardSelect;
          break;
        case AppStep.keycapFill:
          _boardColorCode = null;
          _boardShape = null;
          _resetLetters();
          // [수정] 축 선택 화면은 더 이상 쓰지 않으므로 항상 판 색상 선택으로 되돌아감
          _step = AppStep.boardSelect;
          break;
        default:
          break;
      }
    });
  }

  // 물리적 키 위치 기준 매핑 (logicalKey/keyLabel은 한/영 입력 소스에 따라 값이
  // 바뀌어서 한글 입력 상태일 때 글자 입력이 먹통이 될 수 있음 -> physicalKey로 고정)
  static final Map<PhysicalKeyboardKey, String> _keyCharMap = {
    PhysicalKeyboardKey.keyA: 'A',
    PhysicalKeyboardKey.keyB: 'B',
    PhysicalKeyboardKey.keyC: 'C',
    PhysicalKeyboardKey.keyD: 'D',
    PhysicalKeyboardKey.keyE: 'E',
    PhysicalKeyboardKey.keyF: 'F',
    PhysicalKeyboardKey.keyG: 'G',
    PhysicalKeyboardKey.keyH: 'H',
    PhysicalKeyboardKey.keyI: 'I',
    PhysicalKeyboardKey.keyJ: 'J',
    PhysicalKeyboardKey.keyK: 'K',
    PhysicalKeyboardKey.keyL: 'L',
    PhysicalKeyboardKey.keyM: 'M',
    PhysicalKeyboardKey.keyN: 'N',
    PhysicalKeyboardKey.keyO: 'O',
    PhysicalKeyboardKey.keyP: 'P',
    PhysicalKeyboardKey.keyQ: 'Q',
    PhysicalKeyboardKey.keyR: 'R',
    PhysicalKeyboardKey.keyS: 'S',
    PhysicalKeyboardKey.keyT: 'T',
    PhysicalKeyboardKey.keyU: 'U',
    PhysicalKeyboardKey.keyV: 'V',
    PhysicalKeyboardKey.keyW: 'W',
    PhysicalKeyboardKey.keyX: 'X',
    PhysicalKeyboardKey.keyY: 'Y',
    PhysicalKeyboardKey.keyZ: 'Z',
    PhysicalKeyboardKey.digit0: '0',
    PhysicalKeyboardKey.digit1: '1',
    PhysicalKeyboardKey.digit2: '2',
    PhysicalKeyboardKey.digit3: '3',
    PhysicalKeyboardKey.digit4: '4',
    PhysicalKeyboardKey.digit5: '5',
    PhysicalKeyboardKey.digit6: '6',
    PhysicalKeyboardKey.digit7: '7',
    PhysicalKeyboardKey.digit8: '8',
    PhysicalKeyboardKey.digit9: '9',
  };

  // 숫자 1~4 전용 매핑 (선택지/색상/축 선택용)
  static Map<PhysicalKeyboardKey, int> _digitMap = {
    PhysicalKeyboardKey.digit1: 1,
    PhysicalKeyboardKey.digit2: 2,
    PhysicalKeyboardKey.digit3: 3,
    PhysicalKeyboardKey.digit4: 4,
  };

  // [신규] 같은 물리 키를 아주 짧은 간격으로 다시 누르면(=OS의 키 반복/오토리핏,
  // 또는 실수로 두 번 눌림) 무시합니다. 키오스크는 원래 키보드가 없어서 실제
  // 운영 중엔 거의 영향 없지만, 개발 중 키보드로 테스트할 때 키를 살짝 오래
  // 누르고 있으면 재고 조회가 짧은 시간에 여러 번 나가는 문제를 막아줍니다.
  static const Duration _keyDebounce = Duration(milliseconds: 150);
  PhysicalKeyboardKey? _lastKey;
  DateTime? _lastKeyTime;

  void _handleKey(KeyEvent event) async {
    if (event is! KeyDownEvent) return;

    // 종료 확인창이 떠 있는 동안엔 아래쪽 화면(키오스크 진행 화면)으로
    // 키 입력이 새어나가지 않게 막습니다. (개발 중 키보드 테스트 안전장치)
    if (_showExitDialog) return;

    final now = DateTime.now();
    if (event.physicalKey == _lastKey &&
        _lastKeyTime != null &&
        now.difference(_lastKeyTime!) < _keyDebounce) {
      return; // 같은 키의 짧은 간격 반복 입력은 무시
    }
    _lastKey = event.physicalKey;
    _lastKeyTime = now;

    const backableSteps = {
      AppStep.mbtiChoice,
      AppStep.mbtiQuiz,
      AppStep.mbtiManual,
      AppStep.mbtiResult,
      AppStep.boardSelect,
      AppStep.axisSelect,
      AppStep.keycapFill,
    };

    if (event.physicalKey == PhysicalKeyboardKey.escape &&
        backableSteps.contains(_step)) {
      _goBack();
      return;
    }

    _direction = 1; // 여기서부터 아래는 전부 "앞으로" 이동이므로 미리 표시

    final isEnter = event.physicalKey == PhysicalKeyboardKey.enter ||
        event.physicalKey == PhysicalKeyboardKey.numpadEnter;

    switch (_step) {
      // case AppStep.home:
      //   if (isEnter) {
      //     setState(() => _step = AppStep.mbtiChoice);
      //   }
      //   break;
      case AppStep.home:
        // 대기열이 가득 찬 동안에는 Enter도 무시합니다.
        if (isEnter && !_stockLoading && _queueFull != true) {
          _startOrder();
        }
        break;
      case AppStep.mbtiChoice:
        if (event.physicalKey == PhysicalKeyboardKey.digit1) {
          await _selectMbtiChoice(1);
        } else if (event.physicalKey == PhysicalKeyboardKey.digit2) {
          await _selectMbtiChoice(2);
        }
        break;

      case AppStep.mbtiQuiz:
        await _handleQuizKey(event);
        break;

      case AppStep.mbtiManual:
        await _handleManualKey(event);
        break;

      case AppStep.mbtiResult:
        if (isEnter) {
          await _proceedFromMbtiResult();
        }
        break;

      case AppStep.boardSelect:
        final boardDigit = _digitMap[event.physicalKey];
        if (boardDigit != null) {
          await _selectBoardColor(boardDigit);
        }
        break;

      case AppStep.axisSelect:
        // [삭제] 축(스위치)은 제품에서 제외되어 이 화면에는 더 이상 진입하지 않습니다.
        break;

      case AppStep.keycapFill:
        await _handleKeycapFillKey(event);
        break;

      case AppStep.designConfirm:
        // 응답을 기다리는 동안에는 Enter/Esc 모두 무시 (중복 클릭 방지)
        if (_submittingOrder) break;
        if (isEnter) {
          await _confirmAndSubmitOrder();
        } else if (event.physicalKey == PhysicalKeyboardKey.escape) {
          _cancelDesignAndReset();
        }
        break;

      case AppStep.receipt:
        break; // 영수증 화면은 표시 전용 — 키 입력 무시

      case AppStep.complete:
        break; // [보류] 현재 플로우에서 쓰지 않음
    }
  }

  // (터치/키보드 공용) MBTI 결과 화면에서 다음으로 진행 -> 판 색상 선택(STEP 04)
  Future<void> _proceedFromMbtiResult() async {
    setState(() => _step = AppStep.boardSelect);
  }

  // (터치/키보드 공용) MBTI 아는지 선택: 1=몰라요(퀴즈), 2=알아요(직접입력)
  Future<void> _selectMbtiChoice(int digit) async {
    if (digit == 1) {
      await _moveToStep(AppStep.mbtiQuiz, beforeMove: _resetQuiz);
    } else if (digit == 2) {
      await _moveToStep(AppStep.mbtiManual, beforeMove: _resetManual);
    }
  }

  Future<void> _handleQuizKey(KeyEvent event) async {
    final digit = _digitMap[event.physicalKey];
    if (digit == null) return;
    await _selectQuizOption(digit);
  }

  // (터치/키보드 공용) 퀴즈 보기 선택
  // [버그 수정] 예전에는 재고를 새로고침하기도 전에 메모리에 남아있는
  // (오래됐을 수 있는) 재고로 먼저 품절 여부를 검사해서, 캐시가 낡은 경우
  // 실제로는 품절이 아닌데도 계속 막히는 문제가 있었습니다. 이제 항상
  // 최신 재고를 먼저 조회한 뒤에 품절 여부를 검사합니다.
  Future<void> _selectQuizOption(int digit) async {
    final options = _mbtiQuestions[_quizIndex]['options'] as List;
    final letter = options[digit - 1]['letter'] as String;

    final success = await _loadSoldOutStock(scope: 'keycap');

    if (!mounted || !success) return;

    // 선택한 E/I/N/S/F/T/J/P의 모든 색상이 품절이면 입력 무시
    if (_isLetterSoldOut(letter)) return;

    if (_quizIndex < _mbtiQuestions.length - 1) {
      setState(() {
        _quizAnswers[_quizIndex] = letter;
        _quizIndex++;
      });
    } else {
      setState(() {
        _quizAnswers[_quizIndex] = letter;
        _mbtiResult = _quizAnswers.map((e) => e!).join();
        _step = AppStep.mbtiResult;
      });
    }
  }

  Future<void> _handleManualKey(KeyEvent event) async {
    final letter = _keyCharMap[event.physicalKey];
    if (letter == null) return;
    await _selectManualLetter(letter);
  }

  // (터치/키보드 공용) 직접입력 화면에서 알파벳 하나 선택
  Future<void> _selectManualLetter(String letter) async {
    final pair = _manualPairs[_manualIndex];

    // 현재 단계의 글자가 아니면 무시
    if (letter != pair[0] && letter != pair[1]) return;

    // 화면 전환 직전에 최신 재고 확인
    final success = await _loadSoldOutStock(scope: 'keycap');

    if (!mounted || !success) return;

    // 최신 재고 기준으로 다시 품절 확인
    if (_isLetterSoldOut(letter)) return;

    setState(() {
      _manualAnswers[_manualIndex] = letter;

      if (_manualIndex < _manualPairs.length - 1) {
        _manualIndex++;
      } else {
        _mbtiResult = _manualAnswers.map((e) => e!).join();
        _step = AppStep.mbtiResult;
      }
    });
  }

  // (터치/키보드 공용) 판(케이스) 색상 선택. digit 1~4 = g/y/b/r
  // (keycap_fill_screen.dart의 색상 범례·숫자 배정과 동일한 순서)
  Future<void> _selectBoardColor(int digit) async {
    if (digit < 1 || digit > _pastelColorCycle.length) return;
    final selectedColor = _pastelColorCycle[digit - 1];

    // 화면 이동 직전에 최신 재고 조회
    final success = await _loadSoldOutStock(scope: 'board');

    if (!mounted || !success) return;

    // 최신 재고에서 품절된 판 색상이면 입력 무시
    if (_soldOutBoards.contains(selectedColor)) return;

    await _goToKeycapFillWithBoardColor(selectedColor);
  }

  // 판 색상을 정하고 키캡 채우기 화면으로 넘어가는 공통 로직.
  // 판 크기는 1×4로 고정됩니다(2×2 옵션은 팀 확정으로 제거됨).
  Future<void> _goToKeycapFillWithBoardColor(String colorCode) async {
    setState(() {
      _boardColorCode = colorCode;
      _boardShape = '1x4';
      _boardCount = 4;
      _letters = (_mbtiResult ?? '----').split('');
      _colorCodes = List<String?>.filled(_boardCount, null);
      _cursor = 0;
      _keycapMessage = null;

      _step = AppStep.keycapFill;
    });
    await _refreshKeycapStockForCursor();
  }

  Future<void> _handleKeycapFillKey(KeyEvent event) async {
    // 재고 조회 중에는 중복 입력 방지
    if (_stockLoading) return;

    final physicalKey = event.physicalKey;
    final digit = _digitMap[physicalKey];

    // --------------------------------------------------
    // 1~4 숫자키: 키캡 색상 선택 또는 변경
    // --------------------------------------------------
    if (digit != null) {
      await _selectKeycapColorDigit(digit);
      return;
    }

    // --------------------------------------------------
    // 화살표: 커서 이동 후 해당 글자의 재고를 다시 조회
    // --------------------------------------------------
    int? nextCursor;

    if (physicalKey == PhysicalKeyboardKey.arrowLeft) {
      nextCursor = (_cursor - 1).clamp(0, _boardCount - 1);
    } else if (physicalKey == PhysicalKeyboardKey.arrowRight) {
      nextCursor = (_cursor + 1).clamp(0, _boardCount - 1);
    } else if (physicalKey == PhysicalKeyboardKey.arrowUp &&
        _boardShape == '2x2') {
      nextCursor = (_cursor - 2).clamp(0, _boardCount - 1);
    } else if (physicalKey == PhysicalKeyboardKey.arrowDown &&
        _boardShape == '2x2') {
      nextCursor = (_cursor + 2).clamp(0, _boardCount - 1);
    }

    if (nextCursor != null) {
      await _selectKeycapCell(nextCursor);
      return;
    }

    // --------------------------------------------------
    // Backspace: 현재 칸의 색상 삭제
    // --------------------------------------------------
    if (physicalKey == PhysicalKeyboardKey.backspace) {
      setState(() {
        _colorCodes[_cursor] = null;
        _keycapMessage = '현재 키캡의 색상 선택을 삭제했습니다.';
      });
      return;
    }

    // --------------------------------------------------
    // ENTER: 전체 선택 여부 및 주문 직전 재고 검사
    // --------------------------------------------------
    final isEnter = physicalKey == PhysicalKeyboardKey.enter ||
        physicalKey == PhysicalKeyboardKey.numpadEnter;

    if (isEnter) {
      await _trySubmitKeycapFill();
    }
  }

  // (터치/키보드 공용) 커서 위치의 색을 digit(1~4)에 해당하는 색으로 선택.
  Future<void> _selectKeycapColorDigit(int digit) async {
    if (_stockLoading) return;

    final selectedColor = _pastelColorCycle[digit - 1];

    // 선택 직전 최신 재고 조회
    final success = await _loadSoldOutStock(scope: 'keycap');

    if (!mounted || !success) return;

    final selectedLetter = _letters[_cursor];
    final stockCode = '${selectedLetter}_$selectedColor';

    setState(() {
      // 기존 선택 중 새로 품절된 색상이 있으면 해제
      final invalidIndex = _removeInvalidColorSelections();

      if (invalidIndex != null) {
        _cursor = invalidIndex;
        _keycapMessage = '선택한 부품의 재고가 변경되었습니다. 색상을 다시 선택해 주세요.';
        return;
      }

      // 현재 글자의 모든 색상이 품절
      if (_isLetterSoldOut(selectedLetter)) {
        _keycapMessage = '선택 가능한 색상이 없습니다. 이전 단계로 돌아가 다른 MBTI를 선택해 주세요.';
        return;
      }

      // 사용자가 누른 색상이 품절이면 기존 선택 유지
      // 커서도 다음 칸으로 이동하지 않음
      if (_soldOutKeycaps.contains(stockCode)) {
        _keycapMessage = '$selectedLetter 키캡의 해당 색상은 재고가 없습니다.';
        return;
      }

      // 선택 가능한 색상이므로 저장
      _colorCodes[_cursor] = selectedColor;
      _keycapMessage = null;

      // 마지막 칸이 아닐 때만 다음 칸으로 자동 이동
      if (_cursor < _boardCount - 1) {
        _cursor++;
      }

      // 자동 이동한 글자의 모든 색상이 품절인지 검사
      if (_isLetterSoldOut(_letters[_cursor])) {
        _keycapMessage = '선택 가능한 색상이 없습니다. 이전 단계로 돌아가 다른 MBTI를 선택해 주세요.';
      }
    });
  }

  // (터치 전용) 키캡 칸을 직접 탭했을 때 커서를 그 칸으로 바로 이동.
  // 키보드의 화살표 이동과 달리 원하는 칸으로 한 번에 건너뜁니다.
  Future<void> _selectKeycapCell(int index) async {
    if (_stockLoading) return;

    final clamped = index.clamp(0, _boardCount - 1);

    setState(() {
      _cursor = clamped;
      _keycapMessage = null;
    });

    // 이동한 키캡 글자의 최신 재고 확인
    await _refreshKeycapStockForCursor();
  }

  // (터치/키보드 공용) 키캡 채우기 완료(=ENTER) 시도.
  // 다 채웠으면 디자인 확인 화면으로, 아니면 빈 칸으로 안내합니다.
  Future<void> _trySubmitKeycapFill() async {
    if (_stockLoading) return;

    // 색상을 선택하지 않은 첫 번째 칸 찾기
    final firstEmptyIndex = _colorCodes.indexWhere((color) => color == null);

    if (firstEmptyIndex != -1) {
      setState(() {
        _cursor = firstEmptyIndex;
        _keycapMessage = '색을 모두 선택하세요.';
      });

      await _refreshKeycapStockForCursor();
      return;
    }

    // 네 칸을 모두 선택했더라도 주문 직전 최신 재고 재조회
    final success = await _loadSoldOutStock(scope: 'keycap');

    if (!mounted || !success) return;

    int? firstInvalidIndex;

    setState(() {
      firstInvalidIndex = _removeInvalidColorSelections();

      if (firstInvalidIndex != null) {
        _cursor = firstInvalidIndex!;
        _keycapMessage = '선택한 부품의 재고가 변경되었습니다. 색상을 다시 선택해 주세요.';
      }
    });

    // 품절된 선택이 하나라도 있었다면 주문하지 않음
    if (firstInvalidIndex != null) {
      return;
    }

    // 현재 커서의 글자가 모든 색상 품절인지 마지막으로 검사
    if (_isLetterSoldOut(_letters[_cursor])) {
      setState(() {
        _keycapMessage = '선택 가능한 색상이 없습니다. 이전 단계로 돌아가 다른 MBTI를 선택해 주세요.';
      });
      return;
    }

    // [수정] 여기서 바로 주문을 보내지 않고, 먼저 디자인 확인 화면으로
    // 이동합니다. 실제 주문 전송은 그 화면에서 Enter를 눌러야만 일어납니다.
    _goToDesignConfirm();
  }

  // 색 선택을 모두 마친 뒤 STEP 06(디자인 확인)으로 이동.
  // 이 시점에는 아직 어떤 정보도 서버로 전송하지 않습니다.
  void _goToDesignConfirm() {
    setState(() {
      _step = AppStep.designConfirm;
      _keycapMessage = null;
      _designConfirmMessage = null; // [수정] 디자인 수정용
    });
  }

  // STEP 06(디자인 확인)애서 특정 키캡을 클릭하여 키캡의 색상 변경
  Future<bool> _changeDesignKeycapColor(
    int index,
    String colorCode,
  ) async {
    if (_submittingOrder || _stockLoading) return false;

    if (index < 0 || index >= _letters.length) return false;

    // 실제 변경 직전에 최신 재고 확인
    final success = await _loadSoldOutStock();

    if (!mounted || !success) {
      setState(() {
        _designConfirmMessage = '재고 정보를 확인하지 못했습니다.';
      });
      return false;
    }

    final letter = _letters[index];

    // 선택하려는 키캡 색상이 품절인지 확인
    if (_isKeycapColorSoldOut(letter, colorCode)) {
      setState(() {
        _designConfirmMessage = '$letter 키캡의 해당 색상은 현재 품절입니다.';
      });
      return false;
    }

    setState(() {
      _colorCodes[index] = colorCode;
      _designConfirmMessage = null;
    });

    return true;
  }

  // STEP 06(디자인 확인)에서 Esc를 눌렀을 때: 아무 정보도 전송하지 않고
  // 모든 선택값을 초기화한 뒤 STEP 01(MBTI를 아는지 선택)로 되돌아갑니다.
  void _cancelDesignAndReset() {
    setState(() {
      _direction = -1;
      _step = AppStep.mbtiChoice;
      _boardShape = null;
      _boardColorCode = null;
      _mbtiResult = null;
      _keycapMessage = null;
      _resetQuiz();
      _resetManual();
      _orderId = null;
      _orderStatus = null;
      _receiptOrderNumber = null;
      _receiptTime = null;
      _receiptQrBytes = null;
      _orderAttemptId = null; // 뒤로 가는 경우 = 다음 시도는 새 uuid
      _submittingOrder = false;
      _resetLetters();
    });
  }

  // STEP 06(디자인 확인)에서 Enter를 눌렀을 때 호출됩니다.
  // 이 시점에 비로소 실제 주문/제작 정보가 서버로 전송됩니다.
  Future<void> _confirmAndSubmitOrder() async {
    // 이미 응답을 기다리는 중이면 중복 실행 금지 (안전장치. 실제로는
    // _handleKey에서 이미 걸러지지만, 혹시 모를 재진입에 대비)
    if (_submittingOrder) return;

    // 접수 직전 마지막 재고 확인 (디자인 확인 화면에 머무는 동안 재고가
    // 바뀌었을 수 있으므로 다시 확인합니다)
    final success = await _loadSoldOutStock();

    if (!mounted || !success) return;

    int? invalidIndex;

    setState(() {
      invalidIndex = _removeInvalidColorSelections();

      if (invalidIndex != null) {
        _designConfirmMessage = '선택한 키캡의 재고가 변경되었습니다. 색상을 다시 선택해 주세요.';
      }
    });

    // 재고가 변경된 키캡이 있으면 주문 생성 금지 (키캡 화면으로 돌려보냄)
    if (invalidIndex != null) {
      return;
    }

    // 선택하지 않은 색상이 남아 있으면 주문 생성 금지
    final firstEmptyIndex = _colorCodes.indexWhere((color) => color == null);

    if (firstEmptyIndex != -1) {
      setState(() {
        _designConfirmMessage = '색상이 비어 있는 키캡이 있습니다. 해당 키캡을 눌러 색상을 선택해 주세요.';
      });

      return;
    }

    // [주문하기(Enter)를 누른 순간] 이번 "한 번의 주문 시도"에 쓸 idempotency
    // 키를 확보합니다. 이미 값이 있다면(=응답을 못 받아 자동 재시도하는 상황)
    // 그 값을 그대로 재사용하고, 없으면(=새 시도) 새로 만듭니다.
    final attemptId = _orderAttemptId ??= const Uuid().v4();

    setState(() {
      _submittingOrder = true; // Enter를 다시 눌러도 무시되도록 잠금
      _orderId = null;
      _orderStatus = null;
      _receiptOrderNumber = null;
      _receiptTime = _formatNowHHmm();
      _receiptQrBytes = null;
    });

    // [임시] API 연동이 꺼져있으면 실제 서버 대신 로컬에서 가짜 진행 상태를
    // 흘려보내서, 영수증 화면까지 백엔드 없이 확인할 수 있게 합니다.
    if (!kApiEnabled) {
      _orderAttemptId = null; // 이 시도는 여기서 끝남
      setState(() => _submittingOrder = false);
      _mockSubmitAndGoToReceipt();
      return;
    }

    const maxAttempts = 3;

    for (var attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        final colors = List.generate(_boardCount, _colorCode);

        final result = await _api.createOrder(
          board: _boardColorWord(_boardColorCode ?? 'g'),
          keycap: _letters.join(),
          colors: colors,
          idempotencyKey: attemptId,
        );

        if (!mounted) return;

        // 성공했으므로 이 "시도"는 끝. 다음 주문은 새 uuid를 씀.
        _orderAttemptId = null;

        setState(() {
          _submittingOrder = false;
          _orderId = result['order_id'] as String?;
          _orderStatus = result;
          _receiptOrderNumber = _formatOrderNumber(result);
          _step = AppStep.receipt;
        });

        // 영수증 화면 진입 직후부터 8초 뒤 자동으로 처음 화면으로 복귀
        _scheduleDoneRestart();

        // 조립대 배정 상태를 계속 조회해서 "배정 조립대" 박스 문구를
        // complete_screen.dart와 같은 방식(대기열 N번째 → N번 조립대로
        // 이동해주세요 → 제작 중 → 완료)으로 실시간 갱신합니다.
        _pollStatus();

        // 주문이 정상 생성됐으면 그 order_id로 실제 QR 이미지를 받아옵니다.
        // (실패해도 영수증 자체는 이미 떠 있으므로 조용히 자리표시자로 남겨둠)
        final orderId = _orderId;
        Uint8List? qrBytes;

        if (orderId != null) {
          try {
            // QR 서버가 응답하지 않아도 영수증 출력이 무한정 밀리지 않도록
            // 5초까지만 기다립니다. (화면은 8초 뒤 자동으로 처음으로 돌아갑니다)
            qrBytes = await _api
                .getOrderQr(orderId)
                .timeout(const Duration(seconds: 5));

            // 화면에 사용할 QR 저장
            if (mounted) {
              setState(() {
                _receiptQrBytes = qrBytes;
              });
            }
          } catch (e) {
            print('>>> QR 조회 실패: $e');
          }
        }

        // [수정] 영수증 화면이 뜨면 QR 조회가 성공했는지와 상관없이 영수증을
        // 출력합니다. QR이 없으면 프린터 서비스가 "QR 정보를 불러오지
        // 못했습니다." 문구를 대신 찍습니다.
        await _printReceipt(qrBytes: qrBytes);

        return; // 성공했으므로 재시도 루프 종료
      } on TimeoutException {
        // "응답을 못 받은" 경우 → 같은 idempotency 키로 자동 재시도
        print('>>> [주문] 응답 없음(타임아웃) — 재시도 $attempt/$maxAttempts');
        if (attempt == maxAttempts) {
          if (!mounted) return;
          setState(() {
            _submittingOrder = false;
            _step = AppStep.keycapFill;
            _keycapMessage = '서버 응답이 없습니다. 네트워크 상태를 확인하고 다시 시도해 주세요.';
          });
          return;
        }
        // 짧게 대기 후 같은 attemptId로 재시도 (루프의 다음 반복)
        await Future.delayed(const Duration(seconds: 1));
        continue;
      } on http.ClientException {
        // 이것도 "응답을 못 받은" 경우(연결 자체가 안 된 경우)이므로 동일하게 재시도
        print('>>> [주문] 네트워크 연결 실패 — 재시도 $attempt/$maxAttempts');
        if (attempt == maxAttempts) {
          if (!mounted) return;
          setState(() {
            _submittingOrder = false;
            _step = AppStep.keycapFill;
            _keycapMessage = '네트워크 연결에 실패했습니다. 연결 상태를 확인하고 다시 시도해 주세요.';
          });
          return;
        }
        await Future.delayed(const Duration(seconds: 1));
        continue;
      } catch (e) {
        // 서버가 실제로 응답을 준 경우(예: 4xx/5xx, "대기열이 가득 찼습니다" 등).
        // 이건 "응답을 못 받은" 상황이 아니라 결과가 확정된 것이므로 재시도하지 않습니다.
        if (!mounted) return;

        final message = e.toString();
        // [수정] 예전에는 서버가 실제로 뭐라고 응답했는지 콘솔에 안 남아서
        // 원인 파악이 어려웠습니다. 항상 원문을 출력합니다.
        print('>>> [주문] 서버 응답 오류: $message');

        // 이 시도는 결과가 확정되며 끝났으므로 idempotency 키를 버립니다.
        _orderAttemptId = null;

        // [수정] 재고/품절 관련 오류인지를 "먼저" 판별합니다 — 우리 쪽 사전
        // 점검(재고 조회) 시점과 실제 주문 생성 시점 사이에 재고가 바뀌었을 때
        // 서버가 뒤늦게 거절하는 경우입니다. 창고/디바이스 쪽에서 재고를
        // 만지고 있는 도중이라면 바로 이 케이스일 가능성이 높습니다.
        // 대기열 조회보다 앞에 두는 이유: 재고 때문에 거절당한 바로 그 순간에
        // 마침 대기열도 가득 차 있으면, 아래 full 판정이 이 건을 "대기열 가득"
        // 으로 잘못 분류해서 색 재선택 안내를 못 하게 되기 때문입니다.
        final isStockIssue = message.contains('재고') || message.contains('품절');

        // [수정] 대기열이 가득 찼는지는 더 이상 에러 문구를 문자열로 뒤지지
        // 않고, GET /queue/status 응답의 full 값 하나로 판단합니다.
        // (서버가 안내 문구를 바꿔도 프론트 분기가 따라 깨지지 않습니다.)
        // 조회 자체가 실패해서 null이 오면 판단할 근거가 없으므로, 그때만
        // 예전처럼 에러 문구로 폴백합니다.
        bool isQueueFull = false;
        if (!isStockIssue) {
          final full = await _api.isQueueFull();
          if (!mounted) return;
          isQueueFull = full ?? message.contains('대기열이 가득');
        }

        if (isQueueFull) {
          setState(() {
            _submittingOrder = false;
            _receiptOrderNumber = null;
            _receiptQrBytes = null; // 실제 주문이 생성되지 않았으므로 QR도 없음
            _step = AppStep.receipt;
          });
          _scheduleDoneRestart();

          // [임시/프린터 연동 확인용] 주문이 안 만들어졌어도 영수증 화면이
          // 뜨면 출력합니다. (kPrintReceiptWithoutOrder 설명 참고)
          if (kPrintReceiptWithoutOrder) {
            await _printReceipt();
          }
        } else if (isStockIssue) {
          // 재고 문제로 서버가 거절한 경우: 최신 재고를 다시 반영해서
          // 품절된 칸은 비워주고, 그 칸으로 커서를 옮겨 다시 고르게 함
          final refreshed = await _loadSoldOutStock(scope: 'keycap');
          if (!mounted) return;
          setState(() {
            final invalidIndex =
                refreshed ? _removeInvalidColorSelections() : null;
            _cursor = invalidIndex ?? 0;
            _submittingOrder = false;
            _step = AppStep.keycapFill;
            _keycapMessage = '선택하신 부품이 방금 품절되었습니다. 다른 색을 선택해 주세요.';
          });
        } else {
          // 그 외의 오류는 키캡 화면으로 돌려보내고 안내 문구로 표시합니다.
          setState(() {
            _submittingOrder = false;
            _step = AppStep.keycapFill;
            _keycapMessage = '주문 처리 중 오류가 발생했습니다. 다시 시도해 주세요.';
          });
        }
        return;
      }
    }
  }

  // [임시] API 연동이 꺼져있을 때, 접수 → 영수증까지의 흐름을 백엔드 없이
  // 확인할 수 있도록 하는 가짜 처리.
  void _mockSubmitAndGoToReceipt() {
    _orderId = 'mock-order-id';
    setState(() {
      _receiptOrderNumber =
          (DateTime.now().millisecondsSinceEpoch % 900 + 100).toString();
      _orderStatus = {'stage': 'queued', 'position_in_queue': 1};
      _receiptOrderNumber =
          (DateTime.now().millisecondsSinceEpoch % 900 + 100).toString();
      _orderStatus = {'status': 'waiting', 'position_in_queue': 1};
      _receiptQrBytes = null; // 목업 모드에서는 실제 QR 이미지가 없음(자리표시자로 표시)
      _step = AppStep.receipt;
    });
    _scheduleDoneRestart();
    _printReceipt(); // 서버 없이 프린터 연동만 확인할 수 있도록 목업에서도 출력

    // [임시] 실제 서버 폴링 대신, 2초 간격으로 waiting → assigned →
    // in_progress → done 상태를 흘려보내서 영수증 박스 애니메이션까지
    // 백엔드 없이 확인할 수 있게 합니다.
    final mockSteps = <Map<String, dynamic>>[
      {'status': 'waiting', 'position_in_queue': 1},
      {'status': 'assigned', 'station_id': '1'},
      {'status': 'in_progress', 'station_id': '1'},
      {'status': 'done', 'station_id': '1'},
    ];
    for (var i = 0; i < mockSteps.length; i++) {
      Future.delayed(Duration(seconds: 2 * (i + 1)), () {
        if (!mounted || _step != AppStep.receipt) return;
        setState(() => _orderStatus = mockSteps[i]);
      });
    }
  }

  // [영수증 출력] 영수증 화면이 뜬 직후에 호출됩니다.
  // qrBytes가 null이어도 출력합니다. 출력이 실패해도 화면 진행에는 영향이 없고
  // 콘솔(그리고 kShowPrintDebugOverlay가 켜져 있으면 화면 우측 상단)에
  // 실패 이유가 남습니다.
  Future<void> _printReceipt({Uint8List? qrBytes}) async {
    final job = ++_printJobSeq;
    _setPrintDebug(job, '출력 중...', null);

    try {
      await ReceiptPrinterService.printReceipt(
        orderNumber: _receiptOrderNumber ?? '-',
        time: _receiptTime ?? _formatNowHHmm(),
        mbti: _mbtiResult ?? '----',
        keycapLabels: List.generate(
          _boardCount,
          (i) => _keycapColorLabels[_colorCodes[i]] ?? '-',
        ),
        qrBytes: qrBytes,
      );

      debugPrint('>>> 영수증 출력 완료');
      // 프린터가 알려준 정보(이름/사용 가능 여부/실제 용지 크기)도 함께 표시
      final detail = ReceiptPrinterService.lastPrintInfo;
      _setPrintDebug(
        job,
        '출력 요청 성공\n'
        '${detail != null ? '$detail\n' : ''}'
        '프린터에서 용지가 나오는지 확인하세요',
        true,
      );
    } catch (e) {
      debugPrint('>>> 영수증 출력 실패: $e');
      final detail = ReceiptPrinterService.lastPrintInfo;
      final printers = await _installedPrinterNames();
      _setPrintDebug(
        job,
        '출력 실패\n${_cleanErrorText(e)}'
        '${detail != null ? '\n$detail' : ''}$printers',
        false,
      );
    }
  }

  // [프린터 점검용] 우측 상단 표시 내용을 바꿉니다. 스위치가 꺼져 있거나,
  // 그 사이 새 주문이 시작됐으면(작업 번호가 달라졌으면) 아무것도 하지 않습니다.
  void _setPrintDebug(int job, String message, bool? ok) {
    if (!kShowPrintDebugOverlay) return;
    if (!mounted || job != _printJobSeq) return;
    setState(() {
      _printDebugMessage = message;
      _printDebugOk = ok;
    });
  }

  // [프린터 점검용] 화면에 띄울 오류 문구. 앞의 "Exception: "을 떼고,
  // 혹시 섞여 있을 수 있는 주소(http…)는 가리고, 너무 길면 자릅니다.
  String _cleanErrorText(Object e) {
    var text = e.toString().replaceFirst('Exception: ', '');
    text = text.replaceAll(RegExp(r'https?://\S+'), '[주소 생략]');
    if (text.length > 160) text = '${text.substring(0, 160)}...';
    return text;
  }

  // [프린터 점검용] 출력이 실패했을 때 Windows에 실제로 어떤 이름의 프린터가
  // 설치돼 있는지 함께 보여줍니다. (프린터 이름이 달라서 못 찾는 경우 확인용)
  Future<String> _installedPrinterNames() async {
    if (!kShowPrintDebugOverlay) return '';
    try {
      final printers = await Printing.listPrinters();
      if (printers.isEmpty) return '\n설치된 프린터: 없음';
      var names = printers.map((p) => p.name).join(', ');
      if (names.length > 160) names = '${names.substring(0, 160)}...';
      return '\n설치된 프린터: $names';
    } catch (_) {
      return '';
    }
  }

  String _formatNowHHmm() {
    final now = DateTime.now();
    final hh = now.hour.toString().padLeft(2, '0');
    final mm = now.minute.toString().padLeft(2, '0');
    return '$hh:$mm';
  }

  // 백엔드가 order_status 응답에 "order_seq"(몇 번째 주문인지)를 함께 내려주면
  // 그 값을 그대로 영수증 주문번호로 사용합니다. 혹시 order_seq가 없는
  // 응답이 오는 예외 상황을 대비해 order_id 기반 대체값도 남겨둡니다.
  String _formatOrderNumber(Map<String, dynamic> result) {
    final seq = result['order_seq'];
    if (seq != null) {
      return seq.toString().padLeft(3, '0');
    }

    final orderId = result['order_id'];
    if (orderId == null) return '-';
    final str = orderId.toString();
    if (str.length > 3) return str.substring(str.length - 3).toUpperCase();
    return str;
  }

  // 완성 상태가 되면 일정 시간 뒤 자동으로 처음 화면으로 복귀
  void _scheduleDoneRestart() {
    _doneRestartTimer?.cancel();
    _doneRestartTimer = Timer(_autoRestartAfterDone, () {
      if (mounted) _restart();
    });
  }

  void _startAutoRestartTimer() {
    _autoRestartTimer?.cancel();
    _autoRestartTimer = Timer(_stuckTimeout, () {
      if (mounted && _orderStatus?['status'] != 'done') {
        _restart();
      }
    });
  }

  void _pollStatus() {
    if (_polling || _orderId == null) return;
    _polling = true;
    Future.doWhile(() async {
      await Future.delayed(const Duration(seconds: 2));
      if (!mounted || _orderId == null) return false;
      try {
        final status = await _api.getOrderStatus(_orderId!);
        if (!mounted) return false;
        setState(() => _orderStatus = status);
        // [수정] 영수증 화면의 8초 자동 복귀 타이머는 화면 진입 시 이미
        // 예약되어 있으므로 여기서 다시 예약하지 않습니다. (다시 예약하면
        // 화면에 보이는 카운트다운과 실제 복귀 시점이 어긋납니다)
        if (status['status'] == 'done') {
          return false;
        }
        return true;
      } catch (_) {
        return false;
      }
    }).whenComplete(() => _polling = false);
  }

  // 영수증 화면의 "배정 조립대" 박스에 표시할 큰 문구.
  // complete_screen.dart의 _statusText()와 같은 문구 방식을 따릅니다.
  String _receiptStatusHeadline() {
    // 대기열/조립대가 가득 차서 주문 자체가 생성되지 못한 경우
    if (_orderId == null) return '대기 중';

    final status = _orderStatus?['status'] as String?;
    switch (status) {
      case 'waiting':
        final pos = _orderStatus?['position_in_queue'];
        return pos != null ? '대기열 $pos번째' : '대기 중';
      case 'assigned':
        final st = _orderStatus?['station_id'];
        return st != null ? '$st번 조립대로 이동해주세요' : '조립대 배정됨';
      case 'in_progress':
        final st = _orderStatus?['station_id'];
        return st != null ? '$st번 조립대에서 제작 중' : '제작 중';
      case 'done':
        return '제작 완료!';
      default:
        return '접수 처리 중...';
    }
  }

  // 큰 문구 아래에 덧붙일 작은 보조 문구 (필요한 경우에만)
  String? _receiptStatusCaption() {
    if (_orderId == null) return '스태프가 안내해 드립니다';
    return null;
  }

  // 박스 배경색 결정: 실제로 조립대가 움직이고 있는 상태(배정/제작/완료)면
  // 진한 검정, 그 외(대기/접수 처리 중/미배정)에는 옅은 갈색으로 표시합니다.
  bool _receiptStatusIsActive() {
    if (_orderId == null) return false;

    final stage = _orderStatus?['stage'] as String?;
    const activeStages = {
      'assigned',
      'dispensed',
      'loaded',
      'arrived',
      'verified',
      'completed'
    };
    return activeStages.contains(stage);
    final status = _orderStatus?['status'] as String?;
    return status == 'assigned' || status == 'in_progress' || status == 'done';
  }

  void _restart() {
    _autoRestartTimer?.cancel();
    _doneRestartTimer?.cancel();
    setState(() {
      _direction = -1;
      _step = AppStep.home;
      _boardShape = null;
      _boardColorCode = null;
      _mbtiResult = null;
      _keycapMessage = null;
      _resetQuiz();
      _resetManual();
      _orderId = null;
      _orderStatus = null;
      _receiptOrderNumber = null;
      _receiptTime = null;
      _receiptQrBytes = null;
      _orderAttemptId = null;
      _submittingOrder = false;
      _resetLetters();
    });
  }

  @override
  void dispose() {
    _autoRestartTimer?.cancel();
    _doneRestartTimer?.cancel();
    _exitDialogTimeoutTimer?.cancel();
    _queuePollTimer?.cancel();
    _focusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    Widget screen;
    switch (_step) {
      case AppStep.home:
        screen = HomeScreen(
          onEnter: () => _startOrder(),
          // 재고 조회 중이거나 대기열이 가득 찬 동안에는 탭도 막습니다.
          enabled: !_stockLoading && _queueFull != true,
          queueFull: _queueFull == true,
        );
        break;

      case AppStep.mbtiChoice:
        screen =
            MbtiChoiceScreen(onSelect: (digit) => _selectMbtiChoice(digit));
        break;

      case AppStep.mbtiQuiz:
        final q = _mbtiQuestions[_quizIndex];
        final quizOptions = q['options'] as List;
        screen = MbtiQuizScreen(
          questionIndex: _quizIndex,
          totalQuestions: _mbtiQuestions.length,
          question: q['question'] as String,
          optionTexts: quizOptions.map((o) => o['text'] as String).toList(),
          // [신규] 각 보기의 글자가 품절이면 화면에 표시해서, 관람객이
          // 왜 안 눌리는지 알 수 있게 함
          optionSoldOut: quizOptions
              .map((o) => _isLetterSoldOut(o['letter'] as String))
              .toList(),
          onSelect: (digit) => _selectQuizOption(digit),
        );
        break;

      case AppStep.mbtiManual:
        final pair = _manualPairs[_manualIndex];

        screen = MbtiManualScreen(
          questionIndex: _manualIndex,
          totalQuestions: _manualPairs.length,
          letterA: pair[0],
          letterB: pair[1],
          letterASoldOut: _isLetterSoldOut(pair[0]),
          letterBSoldOut: _isLetterSoldOut(pair[1]),
          onSelect: (letter) => _selectManualLetter(letter),
        );
        break;

      case AppStep.mbtiResult:
        screen = MbtiResultScreen(
          mbti: _mbtiResult ?? '----',
          onNext: () => _proceedFromMbtiResult(),
        );
        break;

      case AppStep.boardSelect:
        screen = BoardSelectScreen(
          soldOutColors: _soldOutBoards,
          onSelect: (digit) => _selectBoardColor(digit),
        );
        break;

      case AppStep.axisSelect:
        // [삭제] 축(스위치)은 제품에서 제외되어 이 단계에는 더 이상 진입하지 않습니다.
        // (코드/파일은 보존하되 라우팅만 막아둠)
        screen = const SizedBox.shrink();
        break;

      case AppStep.keycapFill:
        screen = KeycapFillScreen(
          boardShape: _boardShape ?? '1x4',
          boardColor: BoardColors.of(_boardColorCode ?? 'g'),
          letters: _letters,
          cursor: _cursor,
          colorAt: _colorAt,

          // 현재 선택된 키캡 글자의 품절 색상
          soldOutColors: _soldOutColorsAtCursor(),

          // 화면 하단 안내 문구
          message: _keycapMessage,

          // 재고 조회 중 표시용
          stockLoading: _stockLoading,

          // 터치 지원
          onCellTap: (index) => _selectKeycapCell(index),
          onColorTap: (digit) => _selectKeycapColorDigit(digit),
          onSubmit: () => _trySubmitKeycapFill(),
        );
        break;

      case AppStep.complete:
        // [보류] 로봇 조립 애니메이션 — 현재 플로우에서는 진입하지 않지만
        // 코드는 그대로 보존합니다. (팀 논의 후 사용 여부 결정)
        screen = CompleteScreen(
          boardShape: _boardShape ?? '1x4',
          letters: _letters,
          colorAt: _colorAt,
          orderStatus: _orderStatus,
          onRestart: _restart,
        );
        break;

      case AppStep.designConfirm:
        screen = DesignConfirmScreen(
          boardShape: _boardShape ?? '1x4',
          boardColor: BoardColors.of(_boardColorCode ?? 'g'),
          letters: _letters,
          colorAt: _colorAt,
          colorCodes: _colorCodes, // 현재 키캡 색상 코드
          keycapColorOptions: _colorMap, // 선택 가능한 키캡 색상
          soldOutKeycaps: _soldOutKeycaps, // 품절 키캡
          onKeycapColorChanged: _changeDesignKeycapColor,
          stockLoading: _stockLoading,
          message: _designConfirmMessage,
          isSubmitting: _submittingOrder,
          onConfirm: () => _confirmAndSubmitOrder(),
          onCancel: _cancelDesignAndReset,
        );
        break;

      case AppStep.receipt:
        screen = ReceiptScreen(
          orderNumber: _receiptOrderNumber ?? '-',
          time: _receiptTime ?? _formatNowHHmm(),
          mbti: _mbtiResult ?? '----',
          keycapColors: List.generate(_boardCount, (i) => _colorAt(i)),
          keycapLabels: List.generate(
            _boardCount,
            (i) => _keycapColorLabels[_colorCodes[i]] ?? '-',
          ),
          statusHeadline: _receiptStatusHeadline(),
          statusCaption: _receiptStatusCaption(),
          isActive: _receiptStatusIsActive(),
          qrBytes: _receiptQrBytes,
        );
        break;
    }

    final showBackButton = _step != AppStep.home &&
        _step != AppStep.complete &&
        _step != AppStep.designConfirm &&
        _step != AppStep.receipt;

    // [디자인] 시안 상단 진행바 표시 여부. 화면 전환 로직과는 무관한
    // 순수 표시용 값입니다.
    final progressStep = _progressStepOf(_step);
    final showProgress = progressStep > 0;

    return KeyboardListener(
      focusNode: _focusNode,
      autofocus: true,
      onKeyEvent: _handleKey,
      child: GestureDetector(
        onTap: () => _focusNode.requestFocus(),
        behavior: HitTestBehavior.translucent,
        child: Scaffold(
          backgroundColor: AppColors.background,
          body: KioskScaler(
            child: Stack(
              children: [
                _KioskCardFrame(
                  child: LayoutBuilder(
                    builder: (context, constraints) {
                      return SingleChildScrollView(
                        padding: EdgeInsets.zero,
                        child: ConstrainedBox(
                          constraints: BoxConstraints(
                            minHeight: constraints.maxHeight,
                          ),
                          // 화면 전환 애니메이션 (슬라이드 + 페이드, 방향 인식)
                          // fillColor: Colors.transparent 로 지정해서 전환 중
                          // Scaffold의 AppColors.background 위에 별도 흰색 판이
                          // 덧씌워지지 않도록 함.
                          child: Center(
                            child: PageTransitionSwitcher(
                              duration: const Duration(milliseconds: 300),
                              reverse: _direction == -1,
                              transitionBuilder: (child, primaryAnimation,
                                  secondaryAnimation) {
                                return SharedAxisTransition(
                                  animation: primaryAnimation,
                                  secondaryAnimation: secondaryAnimation,
                                  transitionType:
                                      SharedAxisTransitionType.horizontal,
                                  fillColor: Colors.transparent,
                                  child: child,
                                );
                              },
                              child: KeyedSubtree(
                                key: ValueKey(_step),
                                child: screen,
                              ),
                            ),
                          ),
                        ),
                      );
                    },
                  ),
                ),
                // [디자인] 시안 상단 진행바
                if (showProgress)
                  Positioned(
                    top: KioskCanvas.margin + 58,
                    left: KioskCanvas.margin + 320,
                    right: KioskCanvas.margin + 72,
                    child: _StepProgress(current: progressStep),
                  ),
                // [디자인] 시안 하단 로고 푸터
                // [키오스크 종료용] 이 안의 "사물인터넷혁신융합대학사업단"
                // 로고+글씨가 종료 확인창을 여는 숨겨진 버튼입니다(5초 7회).
                Positioned(
                  left: KioskCanvas.margin,
                  right: KioskCanvas.margin,
                  bottom: KioskCanvas.margin + 40,
                  child: _BrandFooter(onSecretTap: _handleExitBrandTap),
                ),
                if (showBackButton)
                  Positioned(
                    top: KioskCanvas.margin + 44,
                    left: KioskCanvas.margin + 56,
                    child: _BackButton(onTap: _goBack),
                  ),
                // [임시] 서버에 연결하지 않는 테스트 모드(kApiEnabled = false)일 때만
                // 화면 위쪽 가운데에 표시합니다. 터치는 통과시킵니다.
                if (!kApiEnabled)
                  const Positioned(
                    top: KioskCanvas.margin + 6,
                    left: 0,
                    right: 0,
                    child: IgnorePointer(
                      child: Center(child: _OfflineModeBadge()),
                    ),
                  ),
                // [임시/프린터 연동 확인용] 출력 결과 표시. 터치는 통과시킵니다.
                if (kShowPrintDebugOverlay &&
                    _printDebugMessage != null &&
                    (_step == AppStep.receipt || _step == AppStep.home))
                  Positioned(
                    top: KioskCanvas.margin + 24,
                    right: KioskCanvas.margin + 24,
                    child: IgnorePointer(
                      child: _PrintDebugBadge(
                        message: _printDebugMessage!,
                        ok: _printDebugOk,
                      ),
                    ),
                  ),
                // [신규] 종료 확인창. 숨겨진 터치를 감지하면 이게 화면 전체를
                // 덮으며 나타나서, 그 아래 키오스크 진행 화면으로는 터치가
                // 전달되지 않습니다(전체를 덮는 GestureDetector가 가로챔).
                if (_showExitDialog)
                  _ExitConfirmDialog(
                    input: _exitPinInput,
                    errorText: _exitPinError,
                    onKeyTap: _onExitKeypadTap,
                    onCancel: _closeExitDialog,
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// [신규] 종료 확인창. 하단 사업단 로고 7회 터치(5초 이내)로 열립니다.
// 화면 전체를 어둡게 덮고, 가운데 카드에
// "종료하시겠습니까?" + 비밀번호 입력용 숫자 키패드 + "아니오" 버튼을 보여줌.
// '*' = 입력 지우기, '#' = 확인(비밀번호 검증), 숫자 = 입력.
class _ExitConfirmDialog extends StatelessWidget {
  final String input;
  final String? errorText;
  final void Function(String key) onKeyTap;
  final VoidCallback onCancel;

  const _ExitConfirmDialog({
    required this.input,
    required this.errorText,
    required this.onKeyTap,
    required this.onCancel,
  });

  static const _keys = [
    ['1', '2', '3'],
    ['4', '5', '6'],
    ['7', '8', '9'],
    ['*', '0', '#'],
  ];

  @override
  Widget build(BuildContext context) {
    return Positioned.fill(
      child: GestureDetector(
        // 뒤 배경을 눌러도 아무 화면으로도 안 새어나가게 전부 흡수
        behavior: HitTestBehavior.opaque,
        onTap: () {},
        child: Container(
          color: AppColors.ink.withValues(alpha: 0.55),
          alignment: Alignment.center,
          child: Container(
            width: 620,
            padding: const EdgeInsets.all(56),
            decoration: BoxDecoration(
              color: AppColors.surface,
              borderRadius: BorderRadius.circular(32),
              border: Border.all(color: AppColors.border, width: 2),
              boxShadow: [
                BoxShadow(
                  color: AppColors.ink.withValues(alpha: 0.18),
                  blurRadius: 48,
                  offset: const Offset(0, 20),
                ),
              ],
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                  '종료하시겠습니까?',
                  style: TextStyle(
                    fontSize: 36,
                    fontWeight: FontWeight.w800,
                    letterSpacing: -0.5,
                    color: AppColors.ink,
                  ),
                ),
                const SizedBox(height: 28),
                // 입력한 자리 수만큼 점(●)으로 마스킹해서 표시
                Container(
                  height: 72,
                  alignment: Alignment.center,
                  decoration: AppDeco.outlined(radius: 16),
                  child: Text(
                    input.isEmpty ? ' ' : '●' * input.length,
                    style: const TextStyle(
                      fontSize: 30,
                      letterSpacing: 10,
                      color: AppColors.ink,
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                SizedBox(
                  height: 32,
                  child: Text(
                    errorText ?? '',
                    style: const TextStyle(
                      color: AppColors.danger,
                      fontSize: 21,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                const SizedBox(height: 16),
                ..._keys.map(
                  (row) => Padding(
                    padding: const EdgeInsets.symmetric(vertical: 6),
                    child: Row(
                      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                      children: row
                          .map((k) => _ExitKeypadButton(
                              label: k, onTap: () => onKeyTap(k)))
                          .toList(),
                    ),
                  ),
                ),
                const SizedBox(height: 28),
                SizedBox(
                  width: double.infinity,
                  child: GestureDetector(
                    behavior: HitTestBehavior.opaque,
                    onTap: onCancel,
                    child: Container(
                      alignment: Alignment.center,
                      padding: const EdgeInsets.symmetric(vertical: 22),
                      decoration: AppDeco.outlined(
                        radius: 18,
                        borderColor: AppColors.ink,
                      ),
                      child: const Text(
                        '아니오',
                        style: TextStyle(
                          fontSize: 26,
                          fontWeight: FontWeight.w700,
                          color: AppColors.ink,
                        ),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _ExitKeypadButton extends StatelessWidget {
  final String label;
  final VoidCallback onTap;
  const _ExitKeypadButton({required this.label, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: Container(
        width: 138,
        height: 88,
        alignment: Alignment.center,
        decoration: AppDeco.outlined(radius: 16),
        child: Text(
          label,
          style: const TextStyle(
            fontSize: 30,
            fontWeight: FontWeight.w700,
            color: AppColors.ink,
          ),
        ),
      ),
    );
  }
}

/// [디자인] 시안의 좌측 상단 '← 이전' 버튼 (연한 테두리 pill)
class _BackButton extends StatelessWidget {
  final VoidCallback onTap;
  const _BackButton({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      behavior: HitTestBehavior.opaque,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 30, vertical: 16),
        decoration: AppDeco.outlined(radius: 16),
        child: const Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              '←',
              style: TextStyle(
                fontSize: 24,
                height: 1.0,
                fontWeight: FontWeight.w700,
                color: AppColors.muted,
              ),
            ),
            SizedBox(width: 12),
            Text(
              '이전',
              style: TextStyle(
                fontSize: 24,
                fontWeight: FontWeight.w600,
                color: AppColors.muted,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// ---------------------------------------------------------------------------
/// [디자인] 시안 공통 셸 — 흰 카드 / 상단 진행바 / 하단 로고 푸터
/// ---------------------------------------------------------------------------
/// 아래 위젯들은 화면을 "그리는" 역할만 합니다. 상태나 흐름을 바꾸지 않습니다.

/// 배경(#F5F6F8) 위에 얹히는 흰 카드. 모든 화면이 이 안에 들어갑니다.
class _KioskCardFrame extends StatelessWidget {
  final Widget child;
  const _KioskCardFrame({required this.child});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(KioskCanvas.margin),
      child: DecoratedBox(
        decoration: AppDeco.card,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(
            KioskCanvas.cardPaddingH,
            KioskCanvas.cardPaddingTop,
            KioskCanvas.cardPaddingH,
            KioskCanvas.cardPaddingBottom,
          ),
          child: child,
        ),
      ),
    );
  }
}

/// 시안 우측 상단의 진행 막대 (STEP n / 6)
class _StepProgress extends StatelessWidget {
  final int current;
  const _StepProgress({required this.current});

  @override
  Widget build(BuildContext context) {
    final ratio = (current / 6).clamp(0.0, 1.0);

    return Row(
      children: [
        Expanded(
          child: SizedBox(
            height: 12,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(999),
              child: Stack(
                children: [
                  const Positioned.fill(
                    child: ColoredBox(color: AppColors.border),
                  ),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: FractionallySizedBox(
                      widthFactor: ratio,
                      heightFactor: 1,
                      child: const ColoredBox(color: AppColors.ink),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
        const SizedBox(width: 22),
        Text(
          '$current / 6',
          style: const TextStyle(
            fontSize: 21,
            fontWeight: FontWeight.w700,
            letterSpacing: 2,
            color: AppColors.muted,
          ),
        ),
      ],
    );
  }
}

/// 시안 하단의 세종대학교 / 사물인터넷혁신융합대학사업단 로고 푸터
class _BrandFooter extends StatelessWidget {
  /// [키오스크 종료용] 사업단 로고/글씨를 탭할 때마다 호출됩니다.
  /// 겉모습은 평범한 로고 그대로이고, 5초 안에 7번 이상 눌렸는지는
  /// 호출받는 쪽(_handleExitBrandTap)이 셉니다.
  final VoidCallback? onSecretTap;

  const _BrandFooter({this.onSecretTap});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        const _FooterLogo(
          asset: 'assets/images/logo-sejong.png',
          label: '세종대학교',
          height: 66,
        ),
        Container(
          width: 2,
          height: 44,
          margin: const EdgeInsets.symmetric(horizontal: 36),
          color: AppColors.border,
        ),
        _FooterLogo(
          asset: 'assets/images/logo-iotcoss.png',
          label: '사물인터넷혁신융합대학사업단',
          height: 62,
          onTap: onSecretTap,
        ),
      ],
    );
  }
}

class _FooterLogo extends StatelessWidget {
  final String asset;
  final String label;
  final double height;

  /// [키오스크 종료용] 지정되면 로고+글씨 전체가 탭을 받습니다.
  /// 눌린 티(물결 효과 등)는 일부러 내지 않아서 관람객에게는 그냥 로고로 보입니다.
  final VoidCallback? onTap;

  const _FooterLogo({
    required this.asset,
    required this.label,
    required this.height,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final content = Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Image.asset(asset, height: height, fit: BoxFit.contain),
        const SizedBox(width: 16),
        Text(
          label,
          style: const TextStyle(
            fontSize: 21,
            fontWeight: FontWeight.w600,
            color: AppColors.muted,
          ),
        ),
      ],
    );

    if (onTap == null) return content;

    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: Padding(
        // 로고 글자에 딱 붙지 않아도 눌리도록 탭 범위를 살짝 넓힙니다.
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 12),
        child: content,
      ),
    );
  }
}

/// 현재 단계를 진행바용 1~6 숫자로 환산합니다(표시 전용).
/// 시작 화면과 영수증 화면은 6단계 밖이라 0(=진행바 숨김)입니다.
int _progressStepOf(AppStep step) {
  switch (step) {
    case AppStep.home:
    case AppStep.receipt:
      return 0;
    case AppStep.mbtiChoice:
      return 1;
    case AppStep.mbtiQuiz:
    case AppStep.mbtiManual:
      return 2;
    case AppStep.mbtiResult:
      return 3;
    case AppStep.boardSelect:
      return 4;
    case AppStep.axisSelect:
    case AppStep.keycapFill:
      return 5;
    case AppStep.complete:
    case AppStep.designConfirm:
      return 6;
  }
}

/// [임시/프린터 연동 확인용] 영수증 출력 결과를 화면 우측 상단에 보여주는 표시.
/// kShowPrintDebugOverlay를 false로 두면 화면에 나오지 않습니다.
/// 초록 테두리 = 성공, 분홍/빨강 테두리 = 실패, 회색 = 출력 중.
class _PrintDebugBadge extends StatelessWidget {
  final String message;
  final bool? ok;
  const _PrintDebugBadge({required this.message, required this.ok});

  @override
  Widget build(BuildContext context) {
    final color = ok == null
        ? AppColors.disabledLine
        : (ok! ? AppColors.accent : AppColors.danger);

    return Container(
      constraints: const BoxConstraints(maxWidth: 480),
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: color, width: 3),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            '프린터 점검용 (임시)',
            style: TextStyle(
              fontSize: 17,
              fontWeight: FontWeight.w700,
              color: AppColors.muted,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            message,
            style: const TextStyle(
              fontSize: 22,
              height: 1.4,
              fontWeight: FontWeight.w700,
              color: AppColors.ink,
            ),
          ),
        ],
      ),
    );
  }
}

/// [임시] 서버에 연결하지 않는 테스트 모드(kApiEnabled = false)임을 알리는 표시.
/// kApiEnabled를 true로 되돌리면 나타나지 않습니다.
class _OfflineModeBadge extends StatelessWidget {
  const _OfflineModeBadge();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 6),
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: AppColors.danger, width: 2),
      ),
      child: const Text(
        '서버 미연결 테스트 모드',
        style: TextStyle(
          fontSize: 19,
          fontWeight: FontWeight.w700,
          color: AppColors.danger,
        ),
      ),
    );
  }
}
