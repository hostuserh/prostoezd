enum ParkingStatus {
  free,
  occupied,
  unknown;

  String toJson() => name;

  static ParkingStatus fromJson(String json) {
    return ParkingStatus.values.firstWhere((e) => e.name == json);
  }
}

enum ParkingType {
  free,
  paid,
  restricted;

  String toJson() => name;

  static ParkingType fromJson(String json) {
    return ParkingType.values.firstWhere((e) => e.name == json);
  }
}

enum SearchMode {
  closest,
  fastest;

  String toJson() => name;

  static SearchMode fromJson(String json) {
    return SearchMode.values.firstWhere((e) => e.name == json);
  }
}
