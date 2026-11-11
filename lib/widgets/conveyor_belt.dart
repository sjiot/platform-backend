import 'dart:math';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../theme/app_colors.dart';

/// 부품이 흘러 들어오는 컨베이어 벨트
///
/// 벨트 위로 키캡이 왼쪽에서 들어와 오른쪽 끝에서 잘려 사라집니다.
class ConveyorBelt extends StatefulWidget {
  const ConveyorBelt({
    super.key,
    this.label = '부품 대기 중',
    this.letters = const ['E', 'N', 'F', 'P'],
    this.colors = const [
      AppColors.green,
      AppColors.yellow,
      AppColors.blue,
      AppColors.red,
    ],
  });

  final String label;
  final List<String> letters;
  final List<Color> colors;

  @override
  State<ConveyorBelt> createState() => _ConveyorBeltState();
}

class _ConveyorBeltState extends State<ConveyorBelt>
    with SingleTickerProviderStateMixin {
  /// 키캡 한 개가 벨트를 완전히 통과하는 시간
  static const Duration _cycle = Duration(milliseconds: 4400);

  /// 키캡 층 높이, 벨트 높이(24 + 11), 키캡이 벨트 위로 내려앉는 깊이
  static const double _capsHeight = 96, _beltHeight = 35, _capsDrop = 22;

  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: _cycle,
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, child) {
        final t = _controller.value;

        return Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // 벨트를 먼저 그리고 키캡을 그 위에 올려야 키캡이 벨트 앞에 보입니다.
            SizedBox(
              height: _capsHeight + _beltHeight,
              child: Stack(
                children: [
                  Positioned(
                    left: 0,
                    right: 0,
                    bottom: 0,
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(6),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Container(
                            height: 24,
                            color: AppColors.border,
                            child: CustomPaint(
                              size: Size.infinite,
                              // 스트라이프는 1초 주기로 흐릅니다.
                              painter: _BeltPainter((t * 4.4) % 1.0),
                            ),
                          ),
                          Container(height: 11, color: AppColors.boardEdge),
                        ],
                      ),
                    ),
                  ),
                  Positioned(
                    left: 0,
                    right: 0,
                    top: _capsDrop,
                    height: _capsHeight,
                    child: ClipRect(
                      child: CustomPaint(
                        size: Size.infinite,
                        painter: _CapsPainter(
                          t: t,
                          letters: widget.letters,
                          colors: widget.colors,
                          // 키캡의 가장 아래 꼭짓점이 층 바닥에 닿는 값입니다.
                          baseY: _capsHeight - 16,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),

            const SizedBox(height: 24),

            Center(
              child: _BouncingLabel(text: widget.label, t: t),
            ),
          ],
        );
      },
    );
  }
}

/// 벨트 스트라이프
class _BeltPainter extends CustomPainter {
  const _BeltPainter(this.t);

  final double t;

  static const double _pitch = 28, _width = 6;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()..color = AppColors.boardSide;
    final start = -_pitch - (t * _pitch);

    for (double x = start; x < size.width + _pitch; x += _pitch) {
      canvas.drawRect(Rect.fromLTWH(x, 0, _width, size.height), paint);
    }
  }

  @override
  bool shouldRepaint(_BeltPainter old) => old.t != t;
}

/// 벨트 위를 지나가는 키캡
class _CapsPainter extends CustomPainter {
  const _CapsPainter({
    required this.t,
    required this.letters,
    required this.colors,
    required this.baseY,
  });

  final double t;
  final List<String> letters;
  final List<Color> colors;

  /// 키캡 밑면 중심의 y 좌표
  final double baseY;

  static const double capBase = 52, capH = 40, inset = 7;

  /// 샘플과 동일: rotateX(62deg) rotateZ(-24deg)
  static Offset _project(double x, double y, double z) {
    const rz = -24 * pi / 180;
    const rx = 62 * pi / 180;
    final xr = x * cos(rz) - y * sin(rz);
    final yr = x * sin(rz) + y * cos(rz);
    return Offset(xr, yr * cos(rx) - z * sin(rx));
  }

  void _quad(Canvas canvas, List<Offset> points, Color color) {
    canvas.drawPath(
      Path()..addPolygon(points, true),
      Paint()
        ..color = color
        ..isAntiAlias = true,
    );
  }

  void _cap(Canvas canvas, Offset anchor, Color color, String letter) {
    final origin = anchor - _project(capBase / 2, capBase / 2, 0);
    Offset p(double a, double b, double k) => origin + _project(a, b, k);

    // 보이는 면은 앞면(+y) · 왼쪽 끝면(-x) · 윗면 세 개뿐입니다.
    _quad(canvas, [
      p(0, capBase, 0),
      p(capBase, capBase, 0),
      p(capBase - inset, capBase - inset, capH),
      p(inset, capBase - inset, capH),
    ], color);
    _quad(canvas, [
      p(0, 0, 0),
      p(0, capBase, 0),
      p(inset, capBase - inset, capH),
      p(inset, inset, capH),
    ], color);
    final top = <Offset>[
      p(inset, inset, capH),
      p(capBase - inset, inset, capH),
      p(capBase - inset, capBase - inset, capH),
      p(inset, capBase - inset, capH),
    ];
    _quad(canvas, top, color);
    canvas.drawPath(
      Path()..addPolygon(top, true),
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2
        ..strokeJoin = StrokeJoin.round
        ..color = Colors.white.withValues(alpha: 0.55)
        ..isAntiAlias = true,
    );
    final painter = TextPainter(
      text: TextSpan(
        text: letter,
        style: const TextStyle(
          fontSize: 26,
          fontWeight: FontWeight.w900,
          color: AppColors.text,
        ),
      ),
      textDirection: TextDirection.ltr,
    )..layout();

    final cx = capBase / 2;
    final cy = capBase / 2;

    // 키캡 윗면의 중심점
    final center = p(cx, cy, capH);

    // 키캡 윗면에서 x축이 향하는 방향
    final px = p(cx + 1, cy, capH);

    // 키캡 윗면에서 y축이 향하는 방향
    final py = p(cx, cy + 1, capH);

    final xAxis = px - center;
    final yAxis = py - center;

    canvas.save();

    // 글자의 중심을 키캡 윗면 중심으로 이동
    canvas.translate(center.dx, center.dy);

    // 글자의 가로/세로 축을 키캡 윗면 축에 맞춤
    canvas.transform(
      Float64List.fromList([
        xAxis.dx,
        xAxis.dy,
        0,
        0,

        yAxis.dx,
        yAxis.dy,
        0,
        0,

        0,
        0,
        1,
        0,

        0,
        0,
        0,
        1,
      ]),
    );

    // 변환된 좌표계 기준으로 글자를 중앙에 그림
    painter.paint(canvas, Offset(-painter.width / 2, -painter.height / 2));

    canvas.restore();
  }

  @override
  void paint(Canvas canvas, Size size) {
    final travel = size.width + 120;

    for (var i = 0; i < 4; i++) {
      final phase = (t + i * 0.25) % 1.0;
      final x = -60 + phase * travel;
      _cap(
        canvas,
        Offset(x, baseY),
        colors[i % colors.length],
        letters[i % letters.length],
      );
    }
  }

  @override
  bool shouldRepaint(_CapsPainter old) => old.t != t || old.baseY != baseY;
}

/// 글자가 한 글자씩 튀는 문구
class _BouncingLabel extends StatelessWidget {
  const _BouncingLabel({required this.text, required this.t});

  final String text;
  final double t;

  @override
  Widget build(BuildContext context) {
    final chars = text.split('');

    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: List.generate(chars.length, (i) {
        if (chars[i] == ' ') {
          return const SizedBox(width: 8);
        }

        // 1.2초 주기, 글자마다 0.1초씩 지연
        final phase = ((t * 4400 / 1200) - i * 0.083) % 1.0;
        final lift = phase < 0.4 ? sin(phase / 0.4 * pi) : 0.0;

        return Transform.translate(
          offset: Offset(0, -7 * lift),
          child: Opacity(
            opacity: 0.4 + 0.6 * lift,
            child: Text(
              chars[i],
              style: const TextStyle(
                fontSize: 22,
                fontWeight: FontWeight.w800,
                letterSpacing: 3,
                color: AppColors.textSub,
              ),
            ),
          ),
        );
      }),
    );
  }
}
