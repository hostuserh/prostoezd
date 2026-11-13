import 'package:flutter/material.dart';
import '../theme/app_theme.dart';
import '../services/api_service.dart';
import '../models/parking_lot.dart';

class MapScreen extends StatefulWidget {
  const MapScreen({super.key});

  @override
  State<MapScreen> createState() => _MapScreenState();
}

class _MapScreenState extends State<MapScreen> {
  final ApiService _apiService = ApiService();
  List<ParkingLot>? _parkings;
  bool _isLoading = true;

  @override
  void initState() {
    super.initState();
    _loadParkings();
  }

  Future<void> _loadParkings() async {
    try {
      final parkings = await _apiService.getParkings();
      setState(() {
        _parkings = parkings;
        _isLoading = false;
      });
    } catch (e) {
      setState(() {
        _isLoading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.bg,
      body: Stack(
        children: [
          // Заглушка карты
          Container(
            decoration: const BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [
                  Color(0xFF121214),
                  Color(0xFF1A1A1E),
                ],
              ),
            ),
            child: Center(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(
                    Icons.map_outlined,
                    size: 80,
                    color: AppTheme.text3,
                  ),
                  const SizedBox(height: 16),
                  Text(
                    'Карта парковок',
                    style: TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.w600,
                      color: AppTheme.text2,
                    ),
                  ),
                ],
              ),
            ),
          ),

          // Верхняя панель
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Row(
                children: [
                  Expanded(
                    child: Container(
                      height: 48,
                      decoration: BoxDecoration(
                        color: const Color(0xEE1E1E22),
                        border: Border.all(color: AppTheme.line),
                        borderRadius: BorderRadius.circular(AppTheme.rPill),
                      ),
                      child: Row(
                        children: [
                          const SizedBox(width: 16),
                          Icon(
                            Icons.search,
                            color: AppTheme.text2,
                            size: 20,
                          ),
                          const SizedBox(width: 10),
                          Text(
                            'Куда едем?',
                            style: TextStyle(
                              fontSize: 14,
                              fontWeight: FontWeight.w500,
                              color: AppTheme.text2,
                            ),
                          ),
                          const Spacer(),
                          Container(
                            width: 34,
                            height: 34,
                            margin: const EdgeInsets.only(right: 7),
                            decoration: BoxDecoration(
                              shape: BoxShape.circle,
                              gradient: const LinearGradient(
                                begin: Alignment.topLeft,
                                end: Alignment.bottomRight,
                                colors: [Color(0xFF3A3A42), Color(0xFF55555F)],
                              ),
                            ),
                            child: const Center(
                              child: Text(
                                '🧑',
                                style: TextStyle(fontSize: 15),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),

          // Нижняя панель с данными
          if (_parkings != null && _parkings!.isNotEmpty)
            Positioned(
              left: 0,
              right: 0,
              bottom: 0,
              child: Container(
                decoration: BoxDecoration(
                  color: AppTheme.surface,
                  borderRadius: const BorderRadius.vertical(
                    top: Radius.circular(AppTheme.rSheet),
                  ),
                  border: const Border(
                    top: BorderSide(color: AppTheme.line),
                  ),
                  boxShadow: [
                    BoxShadow(
                      color: Colors.black.withOpacity(0.6),
                      blurRadius: 44,
                      offset: const Offset(0, -16),
                    ),
                  ],
                ),
                padding: const EdgeInsets.fromLTRB(17, 10, 17, 34),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    // Grab handle
                    Container(
                      width: 38,
                      height: 4,
                      decoration: BoxDecoration(
                        color: const Color(0xFF34343B),
                        borderRadius: BorderRadius.circular(3),
                      ),
                    ),
                    const SizedBox(height: 14),

                    // Информация о парковке
                    _buildParkingInfo(_parkings!.first),
                  ],
                ),
              ),
            ),

          if (_isLoading)
            Container(
              color: AppTheme.bg.withOpacity(0.8),
              child: const Center(
                child: CircularProgressIndicator(
                  valueColor: AlwaysStoppedAnimation<Color>(AppTheme.accent),
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildParkingInfo(ParkingLot parking) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Expanded(
              child: Text(
                parking.name,
                style: const TextStyle(
                  fontSize: 21,
                  fontWeight: FontWeight.w700,
                  letterSpacing: -0.8,
                  color: AppTheme.text,
                ),
              ),
            ),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
              decoration: BoxDecoration(
                color: AppTheme.freeSoft,
                borderRadius: BorderRadius.circular(8),
              ),
              child: Text(
                '${parking.freeSpots} свободно',
                style: const TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w700,
                  color: AppTheme.free,
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),

        // Статистика
        Container(
          decoration: BoxDecoration(
            color: AppTheme.surface,
            border: Border.all(color: AppTheme.line),
            borderRadius: BorderRadius.circular(18),
          ),
          padding: const EdgeInsets.symmetric(vertical: 13, horizontal: 4),
          child: Row(
            children: [
              _buildStatItem('Свободно', parking.freeSpots, AppTheme.free),
              Container(
                width: 1,
                height: 40,
                color: AppTheme.line,
              ),
              _buildStatItem('Занято', parking.occupiedSpots, AppTheme.occupied),
              Container(
                width: 1,
                height: 40,
                color: AppTheme.line,
              ),
              _buildStatItem('Нет данных', parking.unknownSpots, AppTheme.unknown),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildStatItem(String label, int value, Color color) {
    return Expanded(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 11),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Container(
                  width: 16,
                  height: 16,
                  decoration: BoxDecoration(
                    color: color,
                    shape: BoxShape.circle,
                  ),
                  child: Center(
                    child: Text(
                      label == 'Свободно' ? '✓' : (label == 'Занято' ? '✕' : '?'),
                      style: const TextStyle(
                        fontSize: 9,
                        fontWeight: FontWeight.w800,
                        color: Color(0xFF0A0A0C),
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    label,
                    style: const TextStyle(
                      fontSize: 11.5,
                      fontWeight: FontWeight.w600,
                      color: AppTheme.text2,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Padding(
              padding: const EdgeInsets.only(left: 22),
              child: Text(
                value.toString(),
                style: const TextStyle(
                  fontSize: 19,
                  fontWeight: FontWeight.w700,
                  letterSpacing: -0.5,
                  color: AppTheme.text,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
