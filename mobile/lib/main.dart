import 'package:flutter/material.dart';
import 'theme/app_theme.dart';
import 'screens/map_screen.dart';

void main() {
  runApp(const ParkVisionApp());
}

class ParkVisionApp extends StatelessWidget {
  const ParkVisionApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ParkVision',
      theme: AppTheme.darkTheme,
      debugShowCheckedModeBanner: false,
      home: const ParkVisionHome(),
    );
  }
}

class ParkVisionHome extends StatefulWidget {
  const ParkVisionHome({super.key});

  @override
  State<ParkVisionHome> createState() => _ParkVisionHomeState();
}

class _ParkVisionHomeState extends State<ParkVisionHome> {
  int _selectedIndex = 0;

  final List<Widget> _screens = [
    const MapScreen(),
    const Center(child: Text('Поиск', style: TextStyle(color: AppTheme.text2))),
    const Center(child: Text('Избранное', style: TextStyle(color: AppTheme.text2))),
    const Center(child: Text('Профиль', style: TextStyle(color: AppTheme.text2))),
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: _screens[_selectedIndex],
      bottomNavigationBar: Container(
        height: 86,
        decoration: const BoxDecoration(
          color: Color(0xF2101013),
          border: Border(
            top: BorderSide(color: AppTheme.line),
          ),
        ),
        child: SafeArea(
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceAround,
            children: [
              _buildNavItem(Icons.map_outlined, 'Карта', 0),
              _buildNavItem(Icons.search, 'Поиск', 1),
              const SizedBox(width: 54), // Место для FAB
              _buildNavItem(Icons.favorite_border, 'Избранное', 2),
              _buildNavItem(Icons.person_outline, 'Профиль', 3),
            ],
          ),
        ),
      ),
      floatingActionButton: Container(
        width: 54,
        height: 54,
        decoration: BoxDecoration(
          color: AppTheme.accent,
          shape: BoxShape.circle,
          border: Border.all(color: AppTheme.bg, width: 4),
          boxShadow: [
            BoxShadow(
              color: AppTheme.accent.withOpacity(0.45),
              blurRadius: 22,
              offset: const Offset(0, 8),
            ),
          ],
        ),
        child: const Icon(
          Icons.navigation,
          color: Colors.white,
          size: 25,
        ),
      ),
      floatingActionButtonLocation: FloatingActionButtonLocation.centerDocked,
    );
  }

  Widget _buildNavItem(IconData icon, String label, int index) {
    final isSelected = _selectedIndex == index;
    return InkWell(
      onTap: () => setState(() => _selectedIndex = index),
      child: SizedBox(
        width: 60,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              icon,
              size: 19,
              color: isSelected ? AppTheme.accent : AppTheme.text3,
            ),
            const SizedBox(height: 4),
            Text(
              label,
              style: TextStyle(
                fontSize: 10.5,
                fontWeight: FontWeight.w600,
                color: isSelected ? AppTheme.accent : AppTheme.text3,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
