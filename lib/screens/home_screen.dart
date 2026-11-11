import 'package:flutter/material.dart';
import '../theme/app_theme.dart';

/// STEP 00 — 시작 화면(인트로)
///
/// [디자인 교체] "딸깍 Keyring Studio" 시안 톤으로 다시 칠했습니다.
/// 애니메이션 타임라인(딜레이·지속시간·이동 픽셀)과 동작은 이전과 100% 동일하고,
/// 색·글꼴 크기·모양(둥근 모서리, 입체 키캡, 하드 섀도 버튼)만 바뀌었습니다.
///
/// 구현 방식(중요, v2):
/// 이전 버전은 Timer.delayed로 각 요소의 `visible` bool을 따로 켜고
/// AnimatedSlide(비율 offset)로 움직였습니다. 이 방식은
///   1) 이동량이 "고정 픽셀"이 아니라 "위젯 자기 크기에 대한 비율"이라 피그마와
///      실제 이동 거리가 달라지고,
///   2) 요소마다 독립된 Timer로 애니메이션을 "새로 시작"시키는 구조라
///      프레임이 어긋나면 슬라이드가 생략된 것처럼(= 갑자기 나타나는 것처럼) 보일 수 있었습니다.
///
/// 그래서 이번 버전은 **단일 AnimationController(하나의 타임라인)** 로 모든 요소를
/// 구동합니다. 피그마의 delay(초)·duration(초)을 그대로 밀리초 Interval로 옮기고,
/// 이동은 전부 **고정 픽셀** Tween(Transform.translate)으로 구현해 피그마와
/// 이동 거리·타이밍이 정확히 일치하도록 했습니다.
///
/// 피그마 delay/duration 매핑 (모두 opacity 0→1과 함께):
///   로고 타일 0~3 : delay 60/130/200/270ms, y −20px→0,  duration 300ms
///   서브타이틀     : delay 260ms,            (이동 없음), duration 300ms
///   구분선(scaleX) : delay 320ms,            scaleX 0→1, duration 400ms
///   본문 문구      : delay 380ms,            (이동 없음), duration 300ms
///   ENTER 배지 그룹: delay 460ms,            y   8px→0,  duration 300ms
///                    (배지 자체의 무한 펄스는 별도 컨트롤러로 계속 유지)
class HomeScreen extends StatefulWidget {
  final VoidCallback? onEnter; // 터치 지원: ENTER 배지를 탭해도 시작되도록
  final bool enabled; // 재고 조회 중일 때는 탭을 막기 위함
  // [신규] 대기열이 가득 찼는지. true면 시작하기 버튼을 흐리게 만들고
  // 펄스(깜빡임)를 멈춘 뒤 안내 문구를 띄웁니다. (main.dart가 GET
  // /queue/status 의 full 값을 1초마다 조회해서 내려줍니다)
  final bool queueFull;
  // [신규] 영수증 용지가 떨어졌는지. true면 대기열이 가득 찼을 때와 똑같은
  // 방식으로 시작하기 버튼을 흐리게 만들고 안내 문구를 띄웁니다.
  // (main.dart가 Windows 프린터 드라이버 상태를 2초마다 조회해서 내려줍니다)
  // queueFull과 동시에 true가 될 수도 있는데, 그 경우 대기열 안내를 먼저
  // 보여줍니다(대기열은 대부분 곧 풀리지만, 용지 부족은 스태프가 직접
  // 처리해줘야 해서 더 무거운 상태이긴 하나, 화면에 문구가 계속 바뀌는 것보다
  // 하나로 고정해서 보여주는 쪽이 손님 입장에서 덜 헷갈리기 때문입니다).
  final bool paperOut;

  const HomeScreen({
    super.key,
    this.onEnter,
    this.enabled = true,
    this.queueFull = false,
    this.paperOut = false,
  });

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with TickerProviderStateMixin {
  static const int _introDurationMs = 900;

  late final AnimationController _introController;

  // 각 요소의 opacity / 이동(px) 애니메이션
  late final List<Animation<double>> _tileOpacity;
  late final List<Animation<double>> _tileY;

  late final Animation<double> _subtitleOpacity;

  late final Animation<double> _dividerScaleX;

  late final Animation<double> _bodyOpacity;

  late final Animation<double> _enterOpacity;
  late final Animation<double> _enterY;

  // ENTER 배지 무한 펄스(깜빡임). 피그마: 1.8s 주기로 opacity 1 → 0.4 → 1 반복.
  late final AnimationController _pulseController;
  late final Animation<double> _pulseOpacity;

  @override
  void initState() {
    super.initState();

    _introController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: _introDurationMs),
    );

    Animation<double> fadeIn(double startMs, double endMs) {
      return CurvedAnimation(
        parent: _introController,
        curve: Interval(
          startMs / _introDurationMs,
          endMs / _introDurationMs,
          curve: Curves.easeOut,
        ),
      );
    }

    Animation<double> slideY(double startMs, double endMs, double fromPx) {
      return Tween<double>(begin: fromPx, end: 0).animate(
        CurvedAnimation(
          parent: _introController,
          curve: Interval(
            startMs / _introDurationMs,
            endMs / _introDurationMs,
            curve: Curves.easeOut,
          ),
        ),
      );
    }

    // 로고 타일: delay .06 + i*.07 (초) = 60,130,200,270ms / duration 300ms / y −20px→0
    _tileOpacity = [
      fadeIn(60, 360),
      fadeIn(130, 430),
      fadeIn(200, 500),
      fadeIn(270, 570),
    ];
    _tileY = [
      slideY(60, 360, -20),
      slideY(130, 430, -20),
      slideY(200, 500, -20),
      slideY(270, 570, -20),
    ];

    // 서브타이틀: delay .26 / duration 300ms / 이동 없음(opacity만)
    _subtitleOpacity = fadeIn(260, 560);

    // 구분선: delay .32 / duration 400ms / scaleX 0→1
    _dividerScaleX = CurvedAnimation(
      parent: _introController,
      curve: Interval(
        320 / _introDurationMs,
        720 / _introDurationMs,
        curve: Curves.easeOut,
      ),
    );

    // 본문: delay .38 / duration 300ms / 이동 없음
    _bodyOpacity = fadeIn(380, 680);

    // ENTER 배지 그룹: delay .46 / duration 300ms / y 8px→0
    _enterOpacity = fadeIn(460, 760);
    _enterY = slideY(460, 760, 8);

    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 900), // 0.9s 왕복 = 1.8s 주기
    )..repeat(reverse: true);
    _pulseOpacity = Tween<double>(begin: 1.0, end: 0.4).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );

    // 전체 타임라인 시작 (요소별 지연은 위 Interval들이 각자 알아서 처리)
    _introController.forward();
  }

  @override
  void dispose() {
    _introController.dispose();
    _pulseController.dispose();
    super.dispose();
  }

  // [신규] 시작하기 버튼 본체. 평소(펄스)와 대기열 가득/용지 부족(흐림) 경우에서
  // 같은 모양을 써야 해서 따로 뺐습니다. enabled가 false면 눌리지 않습니다.
  Widget _enterButton() {
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: widget.enabled ? widget.onEnter : null,
      child: Container(
        padding: const EdgeInsets.symmetric(
          horizontal: 76,
          vertical: 28,
        ),
        decoration: AppDeco.pushButton(),
        child: const Text(
          '시작하기',
          style: TextStyle(
            fontSize: 30,
            fontWeight: FontWeight.w800,
            color: AppColors.ink,
          ),
        ),
      ),
    );
  }

  // [신규] 대기열 가득 참 / 영수증 용지 부족 두 경우 모두 같은 모양(흐린 버튼 +
  // 제목 + 설명)으로 보여주기 위한 공용 안내 카드.
  Widget _blockedNotice({required String title, required String body}) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Opacity(opacity: 0.35, child: _enterButton()),
        const SizedBox(height: 28),
        Text(
          title,
          style: const TextStyle(
            fontSize: 28,
            fontWeight: FontWeight.w800,
            color: AppColors.ink,
          ),
        ),
        const SizedBox(height: 12),
        Text(body, style: AppTextStyles.body),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    // [수정] 대기열이 가득 찬 동안에는 펄스를 멈추고 버튼을 흐리게 만든 뒤
    // 아래에 안내 문구를 붙입니다(탭/Enter는 main.dart에서 이미 막혀 있습니다).
    // [신규] 영수증 용지가 떨어진 경우도 같은 방식으로 안내합니다. 두 상태가
    // 동시에 true면 대기열 안내를 우선 보여줍니다.
    final Widget enterArea;
    if (widget.queueFull) {
      enterArea = _blockedNotice(
        title: '지금은 대기열이 가득 찼습니다',
        body: '앞 손님 제작이 끝나면 자동으로 다시 시작할 수 있습니다',
      );
    } else if (widget.paperOut) {
      enterArea = _blockedNotice(
        title: '영수증 용지가 부족합니다',
        body: '스태프에게 문의해주세요. 용지를 채우면 자동으로 다시 시작할 수 있습니다',
      );
    } else {
      enterArea = AnimatedBuilder(
        animation: _pulseOpacity,
        builder: (context, child) =>
            Opacity(opacity: _pulseOpacity.value, child: child),
        child: _enterButton(),
      );
    }

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            _FadeSlide(
              opacity: _tileOpacity[0],
              y: _tileY[0],
              child: const _LogoTile(color: KeycapColors.green, label: 'M'),
            ),
            const SizedBox(width: _LogoTile.gap),
            _FadeSlide(
              opacity: _tileOpacity[1],
              y: _tileY[1],
              child: const _LogoTile(color: KeycapColors.yellow, label: 'B'),
            ),
            const SizedBox(width: _LogoTile.gap),
            _FadeSlide(
              opacity: _tileOpacity[2],
              y: _tileY[2],
              child: const _LogoTile(color: KeycapColors.blue, label: 'T'),
            ),
            const SizedBox(width: _LogoTile.gap),
            _FadeSlide(
              opacity: _tileOpacity[3],
              y: _tileY[3],
              child: const _LogoTile(color: KeycapColors.red, label: 'I'),
            ),
          ],
        ),
        const SizedBox(height: 44),
        AnimatedBuilder(
          animation: _subtitleOpacity,
          builder: (context, child) =>
              Opacity(opacity: _subtitleOpacity.value, child: child),
          child: const Text(
            'CLICKY KEYRING STUDIO',
            style: TextStyle(
              fontSize: 24,
              letterSpacing: 9,
              color: AppColors.muted,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
        const SizedBox(height: 22),
        AnimatedBuilder(
          animation: _dividerScaleX,
          builder: (context, child) => Transform.scale(
            alignment: Alignment.center,
            scaleX: _dividerScaleX.value,
            child: child,
          ),
          child: Container(
            width: 360,
            height: 3,
            decoration: BoxDecoration(
              color: AppColors.border,
              borderRadius: BorderRadius.circular(999),
            ),
          ),
        ),
        const SizedBox(height: 34),
        AnimatedBuilder(
          animation: _bodyOpacity,
          builder: (context, child) =>
              Opacity(opacity: _bodyOpacity.value, child: child),
          child: const Text(
            '나만의 MBTI 키링을 만들어 보세요',
            style: AppTextStyles.body,
          ),
        ),
        const SizedBox(height: 54),
        _FadeSlide(
          opacity: _enterOpacity,
          y: _enterY,
          child: enterArea,
        ),
      ],
    );
  }
}

/// opacity(0→1) + 고정 픽셀 y이동을 함께 처리하는 공용 래퍼.
/// 상위에서 만든 Animation<double>(opacity)와 Animation<double>(y, px)을
/// 그대로 받아 AnimatedBuilder 하나로 렌더링합니다. 두 애니메이션이 같은
/// _introController를 parent로 공유하므로 완전히 같은 프레임에서 갱신됩니다.
class _FadeSlide extends StatelessWidget {
  final Animation<double> opacity;
  final Animation<double> y; // 픽셀 단위, 목표값은 항상 0
  final Widget child;

  const _FadeSlide({
    required this.opacity,
    required this.y,
    required this.child,
  });

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: Listenable.merge([opacity, y]),
      builder: (context, child) {
        return Opacity(
          opacity: opacity.value.clamp(0.0, 1.0),
          child: Transform.translate(
            offset: Offset(0, y.value),
            child: child,
          ),
        );
      },
      child: child,
    );
  }
}

/// [디자인] 시안의 입체 키캡을 그대로 쓴 로고 타일.
///
/// 크기는 시안 원본(116 x 130, 글자 40, 간격 20)에 [scale]을 곱해서 정합니다.
/// 타일·글자·간격이 모두 같은 배율로 커지므로 비율은 시안과 동일하게 유지됩니다.
class _LogoTile extends StatelessWidget {
  static const double scale = 1.3;

  static const double width = 116 * scale;
  static const double height = 130 * scale;
  static const double fontSize = 40 * scale;
  static const double fontSizeMulti = 26 * scale; // 두 글자 이상일 때
  static const double gap = 20 * scale;

  final Color color;
  final String label;
  const _LogoTile({required this.color, required this.label});

  @override
  Widget build(BuildContext context) {
    return Keycap(
      color: color,
      letter: label,
      width: width,
      height: height,
      fontSize: label.length > 1 ? fontSizeMulti : fontSize,
    );
  }
}
