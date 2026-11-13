import 'package:flutter/material.dart';
import '../theme/app_theme.dart';

class FreshnessIndicator extends StatelessWidget {
  final DateTime updatedAt;
  final bool isFresh;

  const FreshnessIndicator({
    super.key,
    required this.updatedAt,
    required this.isFresh,
  });

  String get _ageText {
    final age = DateTime.now().difference(updatedAt);
    if (age.inSeconds < 60) {
      return 'обновлено ${age.inSeconds} секунд назад';
    } else if (age.inMinutes < 60) {
      return 'данные устарели · ${age.inMinutes}:${(age.inSeconds % 60).toString().padLeft(2, '0')} назад';
    } else {
      return 'данные устарели · ${age.inHours}:${(age.inMinutes % 60).toString().padLeft(2, '0')} назад';
    }
  }

  @override
  Widget build(BuildContext context) {
    final color = isFresh ? AppTheme.free : AppTheme.warn;

    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 6,
          height: 6,
          decoration: BoxDecoration(
            color: color,
            shape: BoxShape.circle,
          ),
        ),
        const SizedBox(width: 6),
        Text(
          _ageText,
          style: TextStyle(
            fontSize: 11.5,
            fontWeight: FontWeight.w620,
            color: color,
          ),
        ),
      ],
    );
  }
}
