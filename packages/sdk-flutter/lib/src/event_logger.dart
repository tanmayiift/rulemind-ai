import "dart:convert";

import "package:hive/hive.dart";
import "package:http/http.dart" as http;

import "models.dart";

class EventLogger {
  EventLogger({
    required this.config,
    http.Client? httpClient,
  }) : _httpClient = httpClient ?? http.Client();

  final RuleMindConfig config;
  final http.Client _httpClient;
  Box<dynamic>? _box;

  Future<void> initialize() async {
    _box ??= await Hive.openBox<dynamic>("rulemind.events");
  }

  Future<void> queue(String type, Map<String, dynamic> data) async {
    await initialize();
    final events = ((await _box!.get("pending", defaultValue: <dynamic>[])) as List<dynamic>)
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
    events.add(SdkEvent(type: type, timestamp: DateTime.now().toUtc().toIso8601String(), data: data).toJson());
    await _box!.put("pending", events);
    if (events.length >= config.eventFlushBatchSize) {
      await flush();
    }
  }

  Future<void> flush() async {
    await initialize();
    final events = ((await _box!.get("pending", defaultValue: <dynamic>[])) as List<dynamic>)
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
    if (events.isEmpty) {
      return;
    }
    final response = await _httpClient.post(
      Uri.parse("${config.baseUrl.replaceAll(RegExp(r"/+$"), "")}/sdk/v1/events"),
      headers: <String, String>{
        "Content-Type": "application/json",
        "X-API-Key": config.apiKey,
        "X-SDK-Version": config.sdkVersion,
      },
      body: jsonEncode(<String, dynamic>{"events": events}),
    );
    if (response.statusCode >= 200 && response.statusCode < 300) {
      await _box!.put("pending", <dynamic>[]);
    }
  }
}
