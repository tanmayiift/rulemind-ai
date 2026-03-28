import 'package:flutter/material.dart';
import 'package:rulemind/rulemind.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    await RuleMind.initialize(
      RuleMindConfig(
        baseUrl: const String.fromEnvironment('RULEMIND_BASE_URL',
            defaultValue: 'https://api.rulemind.local'),
        apiKey: const String.fromEnvironment('RULEMIND_API_KEY',
            defaultValue: 'rm_live_your_key_here'),
      ),
    );
  } catch (e) {
    debugPrint('RuleMind init error: $e');
  }
  runApp(const RuleMindExampleApp());
}

class RuleMindExampleApp extends StatelessWidget {
  const RuleMindExampleApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'RuleMind SDK Demo',
      theme: ThemeData(
        colorSchemeSeed: const Color(0xFF3B82F6),
        useMaterial3: true,
      ),
      home: const DemoPage(),
    );
  }
}

class DemoPage extends StatefulWidget {
  const DemoPage({super.key});

  @override
  State<DemoPage> createState() => _DemoPageState();
}

class _DemoPageState extends State<DemoPage> {
  final List<String> _logs = ['SDK initialized.'];

  void _appendLog(String message) {
    setState(() => _logs.add(message));
  }

  Future<void> _syncBundle() async {
    _appendLog('Syncing bundle...');
    try {
      final bundle = await RuleMind.syncNow();
      if (bundle != null) {
        _appendLog(
            'Bundle synced: v${bundle.bundleVersion}, ${bundle.policies.length} policies, ${bundle.variables.length} variables');
      } else {
        _appendLog('No new bundle (already up to date or server unreachable)');
      }
    } catch (e) {
      _appendLog('ERROR syncing: $e');
    }
  }

  Future<void> _evaluatePolicy() async {
    _appendLog('Evaluating policy "policy_pl_underwriting"...');
    try {
      final decision = await RuleMind.evaluate(
        'policy_pl_underwriting',
        {
          'bureau': {'score': 720},
          'bank': {
            'summary': {'avgBalance': 45200}
          },
          'device': {'risk_score': 0},
        },
        userId: 'user_demo_123',
      );
      _appendLog(
          'Decision: outcome=${decision.outcome}, score=${decision.score}, latency=${decision.latencyMs}ms');
      _appendLog('  Variables: ${decision.variables}');
      _appendLog('  Rules: ${decision.ruleResults.length} evaluated');
      if (decision.serverOnlyStepsSkipped.isNotEmpty) {
        _appendLog(
            '  Server-only steps skipped: ${decision.serverOnlyStepsSkipped}');
      }
    } catch (e) {
      _appendLog('ERROR evaluating: $e');
    }
  }

  Future<void> _logEvent() async {
    try {
      await RuleMind.logEvent('sample_app.button_pressed', {
        'screen': 'main',
        'action': 'evaluate_demo',
      });
      _appendLog('Event queued: sample_app.button_pressed');
    } catch (e) {
      _appendLog('ERROR logging event: $e');
    }
  }

  Future<void> _clearCache() async {
    try {
      await RuleMind.clearCache();
      _appendLog('Decision cache cleared');
    } catch (e) {
      _appendLog('ERROR clearing cache: $e');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('RuleMind SDK Demo'),
        centerTitle: true,
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              alignment: WrapAlignment.center,
              children: [
                FilledButton.icon(
                  onPressed: _syncBundle,
                  icon: const Icon(Icons.sync),
                  label: const Text('Sync Bundle'),
                ),
                FilledButton.icon(
                  onPressed: _evaluatePolicy,
                  icon: const Icon(Icons.play_arrow),
                  label: const Text('Evaluate'),
                ),
                FilledButton.tonalIcon(
                  onPressed: _logEvent,
                  icon: const Icon(Icons.event_note),
                  label: const Text('Log Event'),
                ),
                OutlinedButton.icon(
                  onPressed: _clearCache,
                  icon: const Icon(Icons.delete_outline),
                  label: const Text('Clear Cache'),
                ),
              ],
            ),
          ),
          const Divider(),
          Expanded(
            child: ListView.builder(
              padding: const EdgeInsets.all(16),
              itemCount: _logs.length,
              itemBuilder: (context, index) {
                final log = _logs[index];
                final isError = log.startsWith('ERROR');
                return Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Text(
                    log,
                    style: TextStyle(
                      fontSize: 12,
                      fontFamily: 'monospace',
                      color: isError ? Colors.red : null,
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}
