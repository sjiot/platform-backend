import 'package:flutter/material.dart';

import '../models/device_step.dart';
import '../theme/app_colors.dart';

// 개발 중 각 화면을 직접 확인하기 위한 테스트 메뉴
// 디버그 빌드(flutter run)에서만 보인다. (device_app.dart의 kDebugMode 조건)

class DemoMenu extends StatelessWidget {
  final VoidCallback onWaiting;
  final VoidCallback onAuthenticated;
  final VoidCallback onAssembling;
  final VoidCallback onCompleted;
  final VoidCallback onInvalidQr;
  final VoidCallback onWrongWorkstation;
  final VoidCallback onNoShow;

  const DemoMenu({
    super.key,
    required this.onWaiting,
    required this.onAuthenticated,
    required this.onAssembling,
    required this.onCompleted,
    required this.onInvalidQr,
    required this.onWrongWorkstation,
    required this.onNoShow,
  });

  @override
  Widget build(BuildContext context) {
    return PopupMenuButton<DeviceStep>(
      tooltip: '화면 테스트',
      icon: const Icon(Icons.developer_mode, color: AppColors.textSub),
      onSelected: (step) {
        switch (step) {
          case DeviceStep.workstationSetup:
            break;

          case DeviceStep.orderCall:
            break;

          case DeviceStep.waiting:
            onWaiting();
            break;

          case DeviceStep.authenticated:
            onAuthenticated();
            break;

          case DeviceStep.assembling:
            onAssembling();
            break;

          case DeviceStep.completed:
            onCompleted();
            break;

          case DeviceStep.invalidQr:
            onInvalidQr();
            break;

          case DeviceStep.wrongWorkstation:
            onWrongWorkstation();
            break;

          case DeviceStep.noShow:
            onNoShow();
            break;
        }
      },
      itemBuilder: (context) {
        return const [
          PopupMenuItem(value: DeviceStep.waiting, child: Text('1. 대기 중')),
          PopupMenuItem(
            value: DeviceStep.authenticated,
            child: Text('2. 인증 완료 (테스트용)'),
          ),
          PopupMenuItem(value: DeviceStep.assembling, child: Text('3. 조립 중')),
          PopupMenuItem(value: DeviceStep.completed, child: Text('4. 조립 완료')),
          PopupMenuDivider(),
          PopupMenuItem(value: DeviceStep.invalidQr, child: Text('오류: 잘못된 QR')),
          PopupMenuItem(
            value: DeviceStep.wrongWorkstation,
            child: Text('오류: 잘못된 조립대'),
          ),
          PopupMenuItem(
            value: DeviceStep.noShow,
            child: Text('오류: 노쇼(호출 시간 초과)'),
          ),
        ];
      },
    );
  }
}
