import 'package:flutter/material.dart';
import '../models/enums.dart';
import '../theme/app_theme.dart';

class StatusChip extends StatelessWidget {
  final ParkingStatus status;
  final double? confidence;

  const StatusChip({
    super.key,
    required this.status,
    this.confidence,
  });

  @override
  Widget build(BuildContext context) {
    Color bgColor;
    Color textColor;
    Widget dot;
    String label;

    switch (status) {
      case ParkingStatus.free:
        bgColor = AppTheme.freeSoft;
        textColor = AppTheme.free;
        dot = Container(
          width: 7,
          height: 7,
          decoration: const BoxDecoration(
            color: AppTheme.free,
            shape: BoxShape.circle,
          ),
        );
        label = 'Свободно';
        break;
      case ParkingStatus.occupied:
        bgColor = AppTheme.occupiedSoft;
        textColor = AppTheme.occupied;
        dot = Container(
          width: 7,
          height: 7,
          decoration: BoxDecoration(
            color: AppTheme.occupied,
            borderRadius: BorderRadius.circular(1),
          ),
        );
        label = 'Занято';
        break;
      case ParkingStatus.unknown:
        bgColor = AppTheme.unknownSoft;
        textColor = const Color(0xFFAFAFB8);
        dot = Container(
          width: 7,
          height: 7,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            border: Border.all(color: AppTheme.unknown, width: 1.6),
          ),
        );
        label = 'Нет данных';
        break;
    }

    return Container(
      height: 26,
      padding: const EdgeInsets.symmetric(horizontal: 10),
      decoration: BoxDecoration(
        color: bgColor,
        borderRadius: BorderRadius.circular(9),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          dot,
          const SizedBox(width: 6),
          Text(
            label,
            style: TextStyle(
              fontSize: 11.5,
              fontWeight: FontWeight.w680,
              color: textColor,
            ),
          ),
        ],
      ),
    );
  }
}
