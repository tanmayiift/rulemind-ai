import "dart:convert";

import "package:flutter_secure_storage/flutter_secure_storage.dart";
import "package:http/http.dart" as http;

import "crypto_manager.dart";
import "models.dart";

class BundleManager {
  BundleManager({
    required this.config,
    FlutterSecureStorage? secureStorage,
    CryptoManager? cryptoManager,
    http.Client? httpClient,
  })  : _secureStorage = secureStorage ?? const FlutterSecureStorage(),
        _cryptoManager = cryptoManager ?? CryptoManager(config),
        _httpClient = httpClient ?? http.Client();

  final RuleMindConfig config;
  final FlutterSecureStorage _secureStorage;
  final CryptoManager _cryptoManager;
  final http.Client _httpClient;
  Bundle? _bundle;

  Future<Bundle?> currentBundle() async {
    _bundle ??= await _loadPersisted();
    return _bundle;
  }

  Future<Bundle?> syncNow() async {
    final current = await currentBundle();
    final response = await _httpClient.get(
      Uri.parse("${config.baseUrl.replaceAll(RegExp(r"/+$"), "")}/sdk/v1/bundle"),
      headers: <String, String>{
        "X-API-Key": config.apiKey,
        "X-SDK-Version": config.sdkVersion,
        "X-Bundle-Version": (current?.bundleVersion ?? 0).toString(),
        "X-Client-Public-Key": _cryptoManager.publicKeyBase64(),
      },
    );
    if (response.statusCode == 304) {
      return current;
    }
    if (response.statusCode != 200) {
      throw StateError("Bundle sync failed with HTTP ${response.statusCode}");
    }
    final envelope = BundleEnvelope.fromJson(jsonDecode(response.body) as Map<String, dynamic>);
    final decrypted = await _cryptoManager.decryptBundle(envelope);
    if (!_cryptoManager.verifySignature(decrypted, envelope.signature)) {
      throw StateError("Invalid bundle signature");
    }
    final parsed = Bundle.fromJson(jsonDecode(decrypted) as Map<String, dynamic>);
    await _persist(parsed, decrypted);
    _bundle = parsed;
    return parsed;
  }

  Future<void> clear() async {
    _bundle = null;
    await _secureStorage.delete(key: _bundleKey);
  }

  Future<void> _persist(Bundle bundle, String rawJson) async {
    await _secureStorage.write(key: _bundleKey, value: rawJson);
  }

  Future<Bundle?> _loadPersisted() async {
    final raw = await _secureStorage.read(key: _bundleKey);
    if (raw == null || raw.isEmpty) {
      return null;
    }
    return Bundle.fromJson(jsonDecode(raw) as Map<String, dynamic>);
  }

  static const String _bundleKey = "rulemind.bundle.json";
}
