import 'package:flutter/material.dart';

import '../theme/app_colors.dart';

/// 오른쪽 위 "새 버전" 알약 버튼.
///
/// 새 빌드를 받아 뒀고 주문을 처리하지 않는 동안에만 보인다.
/// 누르면 관리자 비밀번호를 거쳐 설치 화면으로 간다.
class UpdateBadge extends StatelessWidget {
  final String versionLabel;
  final VoidCallback onTap;

  const UpdateBadge({
    super.key,
    required this.versionLabel,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: AppColors.text,
      borderRadius: BorderRadius.circular(999),
      child: InkWell(
        borderRadius: BorderRadius.circular(999),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 11),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(
                Icons.system_update_alt,
                size: 20,
                color: AppColors.surface,
              ),
              const SizedBox(width: 10),
              Text(
                '새 버전 $versionLabel 업데이트',
                style: const TextStyle(
                  fontSize: 19,
                  fontWeight: FontWeight.w700,
                  height: 1,
                  color: AppColors.surface,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
