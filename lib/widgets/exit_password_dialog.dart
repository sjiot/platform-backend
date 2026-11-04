import 'package:flutter/material.dart';

import '../theme/app_colors.dart';

/// 관리자 종료 비밀번호.
/// 바꾸려면 이 상수만 고치면 된다.
const String kExitPassword = '202611';

/// 상단 왼쪽 로고를 2초 안에 7번 눌렀을 때 뜨는 숫자 비밀번호 입력창.
/// 업데이트 설치 전 관리자 확인에도 같은 창을 쓴다. ([title]만 바꿔서)
/// 비밀번호가 맞으면 true를 돌려주고, 그 외에는 false/null.
class ExitPasswordDialog extends StatefulWidget {
  /// 창 맨 위 작은 제목 (용도 표시)
  final String title;

  const ExitPasswordDialog({super.key, this.title = '관 리 자 종 료'});

  @override
  State<ExitPasswordDialog> createState() => _ExitPasswordDialogState();
}

class _ExitPasswordDialogState extends State<ExitPasswordDialog> {
  String _input = '';
  bool _hasError = false;

  void _appendDigit(String digit) {
    if (_input.length >= kExitPassword.length) return;

    setState(() {
      _input += digit;
      _hasError = false;
    });

    // 자릿수를 다 채우면 바로 확인
    if (_input.length == kExitPassword.length) {
      _submit();
    }
  }

  void _removeLastDigit() {
    if (_input.isEmpty) return;

    setState(() {
      _input = _input.substring(0, _input.length - 1);
      _hasError = false;
    });
  }

  void _submit() {
    if (_input == kExitPassword) {
      Navigator.of(context).pop(true);
      return;
    }

    setState(() {
      _input = '';
      _hasError = true;
    });
  }

  @override
  Widget build(BuildContext context) {
    // 바깥 터치·뒤로가기로는 닫히지 않는다.
    // [취소] 버튼 또는 비밀번호 통과로만 닫힌다.
    return PopScope(
      canPop: false,
      child: Dialog(
        backgroundColor: AppColors.surface,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(28)),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(120, 40, 120, 32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                widget.title,
                style: const TextStyle(
                  fontSize: 17,
                  fontWeight: FontWeight.w900,
                  color: AppColors.textSub,
                  letterSpacing: 5,
                ),
              ),

              const SizedBox(height: 14),

              const Text(
                '비밀번호를 입력하세요',
                style: TextStyle(
                  fontSize: 25,
                  fontWeight: FontWeight.w900,
                  color: AppColors.text,
                ),
              ),

              const SizedBox(height: 25),

              // 입력한 자릿수 표시
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  for (int i = 0; i < kExitPassword.length; i++)
                    Container(
                      width: 13,
                      height: 13,
                      margin: EdgeInsets.only(
                        right: i == kExitPassword.length - 1 ? 0 : 14,
                      ),
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: i < _input.length
                            ? AppColors.text
                            : AppColors.border,
                      ),
                    ),
                ],
              ),

              const SizedBox(height: 13),

              SizedBox(
                height: 28,
                child: _hasError
                    ? const Text(
                        '비밀번호가 올바르지 않습니다.',
                        style: TextStyle(
                          fontSize: 20,
                          fontWeight: FontWeight.w700,
                          color: AppColors.red,
                        ),
                      )
                    : const SizedBox.shrink(),
              ),

              const SizedBox(height: 10),

              // 숫자 키패드
              for (final row in const [
                ['1', '2', '3'],
                ['4', '5', '6'],
                ['7', '8', '9'],
              ])
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      for (final digit in row)
                        _KeypadButton(
                          label: digit,
                          onPressed: () => _appendDigit(digit),
                        ),
                    ],
                  ),
                ),

              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  _KeypadButton(
                    label: '지움',
                    fontSize: 17,
                    onPressed: _removeLastDigit,
                  ),
                  _KeypadButton(label: '0', onPressed: () => _appendDigit('0')),
                  _KeypadButton(
                    label: '확인',
                    fontSize: 17,
                    filled: true,
                    onPressed: _submit,
                  ),
                ],
              ),

              const SizedBox(height: 18),

              TextButton(
                onPressed: () => Navigator.of(context).pop(false),
                child: const Text(
                  '취소',
                  style: TextStyle(
                    fontSize: 17,
                    fontWeight: FontWeight.w700,
                    color: AppColors.textSub,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _KeypadButton extends StatelessWidget {
  final String label;
  final VoidCallback onPressed;
  final double fontSize;
  final bool filled;

  const _KeypadButton({
    required this.label,
    required this.onPressed,
    this.fontSize = 17,
    this.filled = false,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 6),
      child: SizedBox(
        width: 70,
        height: 40,
        child: Material(
          color: filled ? AppColors.text : AppColors.background,
          borderRadius: BorderRadius.circular(16),
          child: InkWell(
            borderRadius: BorderRadius.circular(16),
            onTap: onPressed,
            child: Center(
              child: Text(
                label,
                style: TextStyle(
                  fontSize: fontSize,
                  fontWeight: FontWeight.w900,
                  color: filled ? AppColors.surface : AppColors.text,
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
