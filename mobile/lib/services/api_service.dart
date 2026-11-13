import 'dart:convert';
import 'package:http/http.dart' as http;
import '../config/app_config.dart';
import '../models/parking_lot.dart';
import '../models/parking_spot.dart';
import '../models/recommendation.dart';
import '../models/enums.dart';
import '../models/location.dart';

class ApiService {
  final String baseUrl;

  ApiService({String? baseUrl}) : baseUrl = baseUrl ?? AppConfig.apiBaseUrl;

  // Заглушка: список парковок
  Future<List<ParkingLot>> getParkings() async {
    // TODO: Заменить на реальный API вызов
    await Future.delayed(const Duration(milliseconds: 300));

    return [
      ParkingLot(
        id: 'P-001',
        name: 'Парковка у ТЦ Clover House',
        latitude: 43.115141,
        longitude: 131.885341,
        capacity: 20,
        freeSpots: 7,
        occupiedSpots: 11,
        unknownSpots: 2,
        parkingType: ParkingType.free,
        accessRules: 'круглосуточно',
        entrance: Location(lat: 43.115200, lon: 131.885400),
        lastUpdate: DateTime.now().subtract(const Duration(seconds: 15)),
      ),
    ];
  }

  // Заглушка: места на парковке
  Future<List<ParkingSpot>> getParkingSpots(String parkingId) async {
    // TODO: Заменить на реальный API вызов
    await Future.delayed(const Duration(milliseconds: 300));

    final now = DateTime.now();
    final spots = <ParkingSpot>[];

    // Генерируем 20 мест в сетке 5x4
    for (int i = 1; i <= 20; i++) {
      final spotId = 'S-${i.toString().padLeft(3, '0')}';
      final row = ((i - 1) ~/ 5);
      final col = ((i - 1) % 5);

      // Координаты в сетке
      final lat = 43.115141 + (row * 0.00003);
      final lon = 131.885341 + (col * 0.00004);

      // Статусы: большинство занято, несколько свободных
      ParkingStatus status;
      if ([2, 5, 7, 13, 15, 17].contains(i)) {
        status = ParkingStatus.free;
      } else if ([9, 19].contains(i)) {
        status = ParkingStatus.unknown;
      } else {
        status = ParkingStatus.occupied;
      }

      spots.add(ParkingSpot(
        spotId: spotId,
        parkingId: parkingId,
        status: status,
        confidence: status == ParkingStatus.unknown ? 0.5 : 0.95,
        location: Location(lat: lat, lon: lon),
        approachPoint: Location(lat: 43.115200, lon: 131.885400),
        updatedAt: now.subtract(Duration(seconds: i * 2)),
        forecastAvailable: false,
      ));
    }

    return spots;
  }

  // Заглушка: рекомендации
  Future<List<Recommendation>> getRecommendations(
    RecommendationRequest request,
  ) async {
    // TODO: Заменить на реальный API вызов
    await Future.delayed(const Duration(milliseconds: 400));

    final now = DateTime.now();

    return [
      Recommendation(
        spotId: 'S-007',
        parkingId: 'P-001',
        parkingName: 'Парковка у ТЦ Clover House',
        status: 'free',
        confidence: 0.97,
        location: Location(lat: 43.115141, lon: 131.885341),
        approachPoint: Location(lat: 43.115200, lon: 131.885400),
        walkingDistanceM: 120,
        drivingEtaMin: 8,
        score: 0.92,
        updatedAt: now.subtract(const Duration(seconds: 12)),
      ),
      Recommendation(
        spotId: 'S-013',
        parkingId: 'P-001',
        parkingName: 'Парковка у ТЦ Clover House',
        status: 'free',
        confidence: 0.95,
        location: Location(lat: 43.115150, lon: 131.885350),
        approachPoint: Location(lat: 43.115200, lon: 131.885400),
        walkingDistanceM: 140,
        drivingEtaMin: 8,
        score: 0.88,
        updatedAt: now.subtract(const Duration(seconds: 18)),
      ),
    ];
  }
}
