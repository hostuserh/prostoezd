import 'enums.dart';
import 'location.dart';

class ParkingSpot {
  final String spotId;
  final String parkingId;
  final ParkingStatus status;
  final double confidence;
  final Location location;
  final Location approachPoint;
  final DateTime updatedAt;
  final bool forecastAvailable;

  const ParkingSpot({
    required this.spotId,
    required this.parkingId,
    required this.status,
    required this.confidence,
    required this.location,
    required this.approachPoint,
    required this.updatedAt,
    required this.forecastAvailable,
  });

  factory ParkingSpot.fromJson(Map<String, dynamic> json) {
    return ParkingSpot(
      spotId: json['spot_id'] as String,
      parkingId: json['parking_id'] as String,
      status: ParkingStatus.fromJson(json['status'] as String),
      confidence: (json['confidence'] as num).toDouble(),
      location: Location.fromJson(json['location'] as Map<String, dynamic>),
      approachPoint: Location.fromJson(json['approach_point'] as Map<String, dynamic>),
      updatedAt: DateTime.parse(json['updated_at'] as String),
      forecastAvailable: json['forecast_available'] as bool,
    );
  }

  bool get isFresh {
    final age = DateTime.now().difference(updatedAt);
    return age.inSeconds <= 60;
  }

  String get ageDescription {
    final age = DateTime.now().difference(updatedAt);
    if (age.inSeconds < 60) {
      return 'обновлено ${age.inSeconds} секунд назад';
    } else if (age.inMinutes < 60) {
      return 'обновлено ${age.inMinutes} минут назад';
    } else {
      return 'данные устарели · ${age.inHours}:${(age.inMinutes % 60).toString().padLeft(2, '0')} назад';
    }
  }
}
