import 'package:flutter/material.dart';

class AppTheme {
  // Surfaces
  static const Color bg = Color(0xFF0A0A0C);
  static const Color surface = Color(0xFF151518);
  static const Color surface2 = Color(0xFF1C1C20);
  static const Color surface3 = Color(0xFF25252A);
  static const Color line = Color(0xFF2A2A30);
  static const Color lineSoft = Color(0xFF201F24);

  // Text
  static const Color text = Color(0xFFFFFFFF);
  static const Color text2 = Color(0xFF9A9AA4);
  static const Color text3 = Color(0xFF65656E);

  // Accent
  static const Color accent = Color(0xFF0A6BFF);
  static const Color accentPress = Color(0xFF0A58CC);
  static const Color accentSoft = Color(0x240A6BFF);

  // Data status
  static const Color free = Color(0xFF30D158);
  static const Color freeSoft = Color(0x2430D158);
  static const Color occupied = Color(0xFFFF453A);
  static const Color occupiedSoft = Color(0x24FF453A);
  static const Color unknown = Color(0xFF8E8E96);
  static const Color unknownSoft = Color(0x248E8E96);
  static const Color warn = Color(0xFFFF9F0A);
  static const Color warnSoft = Color(0x24FF9F0A);

  // Radii
  static const double rCard = 24.0;
  static const double rSheet = 28.0;
  static const double rBtn = 16.0;
  static const double rPill = 999.0;
  static const double rChip = 14.0;

  static ThemeData get darkTheme {
    return ThemeData(
      brightness: Brightness.dark,
      scaffoldBackgroundColor: bg,
      colorScheme: const ColorScheme.dark(
        primary: accent,
        surface: surface,
        background: bg,
      ),
      fontFamily: 'Inter',
      textTheme: const TextTheme(
        displayLarge: TextStyle(
          fontSize: 32,
          fontWeight: FontWeight.w700,
          letterSpacing: -1.4,
          color: text,
        ),
        displayMedium: TextStyle(
          fontSize: 28,
          fontWeight: FontWeight.w800,
          letterSpacing: -1.2,
          color: text,
        ),
        titleLarge: TextStyle(
          fontSize: 21,
          fontWeight: FontWeight.w700,
          letterSpacing: -0.8,
          color: text,
        ),
        titleMedium: TextStyle(
          fontSize: 16,
          fontWeight: FontWeight.w600,
          color: text,
        ),
        bodyLarge: TextStyle(
          fontSize: 14.5,
          fontWeight: FontWeight.w400,
          color: text,
        ),
        bodyMedium: TextStyle(
          fontSize: 12.5,
          fontWeight: FontWeight.w600,
          color: text,
        ),
        bodySmall: TextStyle(
          fontSize: 11.5,
          fontWeight: FontWeight.w500,
          color: text3,
        ),
      ),
    );
  }
}
