import 'package:flutter/material.dart';
import 'package:rulemind/rulemind.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await RuleMind.initialize(
    RuleMindConfig(
      baseUrl: 'https://api.rulemind.local',
      apiKey: 'rm_live_your_key_here',
    ),
  );
  runApp(const RuleMindExampleApp());
}

class RuleMindExampleApp extends StatelessWidget {
  const RuleMindExampleApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: Scaffold(
        appBar: AppBar(title: const Text('RuleMind Example')),
        body: const Center(
          child: Padding(
            padding: EdgeInsets.all(24),
            child: Text(
              'RuleMind initialized. Call RuleMind.syncNow() and RuleMind.evaluate() from your feature flow.',
            ),
          ),
        ),
      ),
    );
  }
}
