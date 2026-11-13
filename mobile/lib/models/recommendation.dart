import 'enums.dart';
import 'location.dart';

class Recommendation {
  final String spotId;
  final String parkingId;
  final String parkingName;
  final String status;
  final double confidence;
  final Location location;
  final Location approachPoint;
  final int walkingDistanceM;
  final int drivingEtaMin;
  final double score;
  final DateTime updatedAt;

  const Recommendation({
    required this.spotId,
    required this.parkingId,
    required this.parkingName,
    required this.status,
    required this.confidence,
    required this.location,
    required this.approachPoint,
    required this.walkingDistanceM,
    required this.drivingEtaMin,
    required this.score,
    required this.updatedAt,
  });

  factory Recommendation.fromJson(Map<String, dynamic> json) {
    return Recommendation(
      spotId: json['spot_id'] as String,
      parkingId: json['parking_id'] as String,
      parkingName: json['parking_name'] as String,
      status: json['status'] as String,
      confidence: (json['confidence'] as num).toDouble(),
      location: Location.fromJson(json['location'] as Map<String, dynamic>),
      approachPoint: Location.fromJson(json['approach_point'] as Map<String, dynamic>),
      walkingDistanceM: (json['walking_distance_m'] as num).round(),
      drivingEtaMin: (json['driving_eta_min'] as num).round(),
      score: (json['score'] as num).toDouble(),
      updatedAt: DateTime.parse(json['updated_at'] as String),
    );
  }
}

class RecommendationRequest {
  final double userLat;
  final double userLon;
  final double destinationLat;
  final double destinationLon;
  final SearchMode searchMode;
  final int? arrivalEtaMinutes;

  const RecommendationRequest({
    required this.userLat,
    required this.userLon,
    required this.destinationLat,
    required this.destinationLon,
    required this.searchMode,
    this.arrivalEtaMinutes,
  });

  Map<String, dynamic> toJson() => {
        'user_lat': userLat,
        'user_lon': userLon,
        'destination_lat': destinationLat,
        'destination_lon': destinationLon,
        'search_mode': searchMode.toJson(),
        if (arrivalEtaMinutes != null) 'arrival_eta_minutes': arrivalEtaMinutes,
      };
}
