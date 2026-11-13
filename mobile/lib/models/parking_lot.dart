import 'enums.dart';
import 'location.dart';

class ParkingLot {
  final String id;
  final String name;
  final double latitude;
  final double longitude;
  final int capacity;
  final int freeSpots;
  final int occupiedSpots;
  final int unknownSpots;
  final ParkingType parkingType;
  final String? accessRules;
  final Location? entrance;
  final DateTime lastUpdate;

  const ParkingLot({
    required this.id,
    required this.name,
    required this.latitude,
    required this.longitude,
    required this.capacity,
    required this.freeSpots,
    required this.occupiedSpots,
    required this.unknownSpots,
    required this.parkingType,
    this.accessRules,
    this.entrance,
    required this.lastUpdate,
  });

  factory ParkingLot.fromJson(Map<String, dynamic> json) {
    return ParkingLot(
      id: json['id'] as String,
      name: json['name'] as String,
      latitude: (json['latitude'] as num).toDouble(),
      longitude: (json['longitude'] as num).toDouble(),
      capacity: json['capacity'] as int,
      freeSpots: json['free_spots'] as int,
      occupiedSpots: json['occupied_spots'] as int,
      unknownSpots: json['unknown_spots'] as int,
      parkingType: ParkingType.fromJson(json['parking_type'] as String),
      accessRules: json['access_rules'] as String?,
      entrance: json['entrance'] != null
          ? Location.fromJson(json['entrance'] as Map<String, dynamic>)
          : null,
      lastUpdate: DateTime.parse(json['last_update'] as String),
    );
  }
}
