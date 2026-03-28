import 'dart:convert';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:pointycastle/export.dart';

import 'models.dart';

class CryptoManager {
  CryptoManager(this.config);

  final RuleMindConfig config;

  String publicKeyBase64() {
    return config.serverPublicKeyPem ?? '';
  }

  Future<String> decryptBundle(BundleEnvelope envelope) async {
    if (envelope.encryptedKey.isEmpty) {
      // Development mode: bundle is base64-encoded plaintext (no encryption key provided)
      return utf8.decode(base64.decode(envelope.encryptedBundle));
    }

    // Production mode: AES-256-GCM decryption with server-provided key
    final encryptedBytes = base64.decode(envelope.encryptedBundle);
    if (encryptedBytes.length <= 12) {
      throw FormatException('Invalid encrypted bundle: payload too short');
    }

    final iv = encryptedBytes.sublist(0, 12);
    final ciphertext = encryptedBytes.sublist(12);

    // The encrypted key is base64-encoded AES key (in production, this would
    // be RSA-decrypted first using the device keypair)
    final aesKeyBytes = base64.decode(envelope.encryptedKey);
    final key = KeyParameter(aesKeyBytes);
    final params = AEADParameters(key, 128, iv, Uint8List(0));

    final cipher = GCMBlockCipher(AESEngine());
    cipher.init(false, params);

    final plaintext = Uint8List(cipher.getOutputSize(ciphertext.length));
    final length = cipher.processBytes(ciphertext, 0, ciphertext.length, plaintext, 0);
    cipher.doFinal(plaintext, length);

    return utf8.decode(plaintext);
  }

  bool verifySignature(String payload, String signature) {
    if (signature.isEmpty) {
      // No signature provided — allow in development mode only
      return config.serverPublicKeyPem == null;
    }

    final expected = sha256.convert(utf8.encode(payload)).toString();
    if (signature == expected) {
      // SHA-256 checksum match (development mode without RSA signing)
      return true;
    }

    if (config.serverPublicKeyPem == null) {
      // No public key configured — cannot verify RSA signature
      return false;
    }

    // RSA signature verification with server public key
    try {
      final publicKeyPem = config.serverPublicKeyPem!
          .replaceAll('-----BEGIN PUBLIC KEY-----', '')
          .replaceAll('-----END PUBLIC KEY-----', '')
          .replaceAll(RegExp(r'\s+'), '');
      final publicKeyBytes = base64.decode(publicKeyPem);

      final rsaParser = RSAKeyParser();
      final publicKey = rsaParser.parse(publicKeyBytes);

      final signer = Signer('SHA-256/RSA');
      signer.init(false, PublicKeyParameter<RSAPublicKey>(publicKey));

      final signatureBytes = base64.decode(signature);
      return signer.verifySignature(
        Uint8List.fromList(utf8.encode(payload)),
        RSASignature(signatureBytes),
      );
    } catch (_) {
      return false;
    }
  }
}

class RSAKeyParser {
  RSAPublicKey parse(Uint8List bytes) {
    final asn1Parser = ASN1Parser(bytes);
    final topLevelSeq = asn1Parser.nextObject() as ASN1Sequence;

    final publicKeyBitString = topLevelSeq.elements![1] as ASN1BitString;
    final publicKeyAsn = ASN1Parser(publicKeyBitString.valueBytes!);
    final publicKeySeq = publicKeyAsn.nextObject() as ASN1Sequence;

    final modulus = (publicKeySeq.elements![0] as ASN1Integer).integer!;
    final exponent = (publicKeySeq.elements![1] as ASN1Integer).integer!;

    return RSAPublicKey(modulus, exponent);
  }
}
