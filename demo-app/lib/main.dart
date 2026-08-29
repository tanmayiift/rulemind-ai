import 'package:flutter/material.dart';
import 'package:rulemind_ui/rulemind_ui.dart';

import 'engine.dart';
import 'journeys.dart';

void main() => runApp(const RuleMindDemoApp());

const _brand = Color(0xFF4F46E5);

class RuleMindDemoApp extends StatelessWidget {
  const RuleMindDemoApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'RuleMind Journeys',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: _brand),
        scaffoldBackgroundColor: const Color(0xFFF7F7FB),
      ),
      darkTheme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: _brand, brightness: Brightness.dark),
      ),
      home: const HomeScreen(),
    );
  }
}

// ─────────────────────────────── HOME ───────────────────────────────
class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});
  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  final RuleMindClient _client = RuleMindClient();
  bool? _online;

  @override
  void initState() {
    super.initState();
    _client.ping().then((v) => mounted ? setState(() => _online = v) : null);
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    return Scaffold(
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(20, 20, 20, 32),
          children: [
            Row(
              children: [
                Container(
                  width: 42, height: 42,
                  decoration: BoxDecoration(color: _brand, borderRadius: BorderRadius.circular(12)),
                  child: const Icon(Icons.account_tree_outlined, color: Colors.white, size: 24),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('RuleMind Journeys',
                          style: t.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
                      Text('On-device & server decisioning demo',
                          style: t.textTheme.bodySmall?.copyWith(color: t.hintColor)),
                    ],
                  ),
                ),
                _StatusDot(online: _online),
              ],
            ),
            const SizedBox(height: 20),
            Text('Pick a customer journey',
                style: t.textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600)),
            const SizedBox(height: 4),
            Text(
              'Four non-lending policies running on the same rule engine. Each shows the '
              'decision, the score, and exactly which gate decided it.',
              style: t.textTheme.bodySmall?.copyWith(color: t.hintColor),
            ),
            const SizedBox(height: 16),
            for (final j in kJourneys) ...[
              _JourneyCard(
                journey: j,
                onTap: () => Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) => JourneyScreen(journey: j, client: _client),
                )),
              ),
              const SizedBox(height: 12),
            ],
            const SizedBox(height: 8),
            Center(
              child: Text('API: ${_client.baseUrl}',
                  style: t.textTheme.labelSmall?.copyWith(color: t.hintColor)),
            ),
          ],
        ),
      ),
    );
  }
}

class _StatusDot extends StatelessWidget {
  const _StatusDot({required this.online});
  final bool? online;

  @override
  Widget build(BuildContext context) {
    final (color, label) = switch (online) {
      true => (Colors.green, 'API up'),
      false => (Colors.red, 'API down'),
      _ => (Colors.orange, 'checking'),
    };
    return Row(mainAxisSize: MainAxisSize.min, children: [
      Container(width: 8, height: 8, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
      const SizedBox(width: 6),
      Text(label, style: Theme.of(context).textTheme.labelSmall),
    ]);
  }
}

class _JourneyCard extends StatelessWidget {
  const _JourneyCard({required this.journey, required this.onTap});
  final Journey journey;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    return Material(
      color: t.cardColor,
      borderRadius: BorderRadius.circular(16),
      child: InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: [
              Container(
                width: 46, height: 46,
                decoration: BoxDecoration(
                  color: journey.color.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Icon(journey.icon, color: journey.color, size: 24),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(journey.sector.toUpperCase(),
                        style: t.textTheme.labelSmall
                            ?.copyWith(color: journey.color, fontWeight: FontWeight.w700, letterSpacing: 0.6)),
                    const SizedBox(height: 2),
                    Text(journey.title,
                        style: t.textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600)),
                    const SizedBox(height: 4),
                    Text(journey.blurb,
                        style: t.textTheme.bodySmall?.copyWith(color: t.hintColor, height: 1.35)),
                  ],
                ),
              ),
              Icon(Icons.chevron_right, color: t.hintColor),
            ],
          ),
        ),
      ),
    );
  }
}

// ───────────────────────────── JOURNEY ─────────────────────────────
class JourneyScreen extends StatefulWidget {
  const JourneyScreen({super.key, required this.journey, required this.client});
  final Journey journey;
  final RuleMindClient client;

  @override
  State<JourneyScreen> createState() => _JourneyScreenState();
}

class _JourneyScreenState extends State<JourneyScreen> {
  final ScrollController _scroll = ScrollController();
  late Map<String, num> _values;
  EvalMode _mode = EvalMode.server;
  EvalResult? _result;
  Scenario? _active;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _apply(widget.journey.scenarios.first);
  }

  void _apply(Scenario s) {
    setState(() {
      _active = s;
      _values = Map<String, num>.from(s.values);
      _result = null;
    });
  }

  Future<void> _evaluate() async {
    setState(() => _busy = true);
    final r = await widget.client.decide(
      _mode,
      widget.journey.policyId,
      widget.journey.payloadFor(_values),
      userId: 'demo-${widget.journey.id}-${DateTime.now().millisecondsSinceEpoch}',
    );
    if (!mounted) return;
    setState(() {
      _result = r;
      _busy = false;
    });
    // The form is taller than the viewport, so the verdict lands below the fold.
    // Without this the button just stops spinning and nothing visibly happens.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(
          _scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 350),
          curve: Curves.easeOut,
        );
      }
    });
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final j = widget.journey;
    final t = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(j.title),
        backgroundColor: j.color,
        foregroundColor: Colors.white,
      ),
      body: ListView(
        controller: _scroll,
        padding: RmScreenPadding.of(context),
        children: [
          Text('SCENARIOS', style: t.textTheme.labelSmall?.copyWith(letterSpacing: 0.8, color: t.hintColor)),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8, runSpacing: 8,
            children: j.scenarios.map((s) {
              final sel = _active?.name == s.name;
              return ChoiceChip(
                label: Text(s.name),
                selected: sel,
                onSelected: (_) => _apply(s),
                selectedColor: j.color.withValues(alpha: 0.18),
                labelStyle: TextStyle(
                  color: sel ? j.color : null,
                  fontWeight: sel ? FontWeight.w600 : FontWeight.w400,
                ),
              );
            }).toList(),
          ),
          if (_active != null) ...[
            const SizedBox(height: 8),
            Text(_active!.blurb, style: t.textTheme.bodySmall?.copyWith(color: t.hintColor, height: 1.35)),
          ],
          const SizedBox(height: 18),
          RmLabeledRow(
            label: Text('INPUTS',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: t.textTheme.labelSmall?.copyWith(letterSpacing: 0.8, color: t.hintColor)),
            trailing: SegmentedButton<EvalMode>(
              style: const ButtonStyle(visualDensity: VisualDensity.compact),
              // Icons are dropped once text is scaled up: at that point the two
              // labels alone consume the available width.
              showSelectedIcon: false,
              segments: <ButtonSegment<EvalMode>>[
                ButtonSegment(
                  value: EvalMode.server,
                  label: const Text('Server'),
                  icon: context.rmIsLargeText ? null : const Icon(Icons.cloud_outlined, size: 15),
                ),
                ButtonSegment(
                  value: EvalMode.onDevice,
                  label: const Text('On-device'),
                  icon: context.rmIsLargeText ? null : const Icon(Icons.smartphone, size: 15),
                ),
              ],
              selected: <EvalMode>{_mode},
              onSelectionChanged: (Set<EvalMode> s) => setState(() {
                _mode = s.first;
                _result = null;
              }),
            ),
          ),
          const SizedBox(height: 8),
          Card(
            elevation: 0,
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
              child: Column(
                children: j.fields.map((f) => _FieldRow(
                      field: f,
                      value: _values[f.key] ?? f.min,
                      color: j.color,
                      onChanged: (v) => setState(() {
                        _values[f.key] = v;
                        _result = null;
                      }),
                    )).toList(),
              ),
            ),
          ),
          const SizedBox(height: 16),
          if (_result != null) ResultCard(result: _result!, expected: _active?.expected, color: j.color),
        ],
      ),
      // A bottomNavigationBar rather than a floatingActionButton: Scaffold then
      // reserves the space itself, so the body's bottom padding no longer needs
      // a hand-tuned constant that was only ever right on one device. It also
      // makes the button a real hit target -- as a centre-floating FAB over a
      // scrolling body, synthesised taps were absorbed by the list.
      bottomNavigationBar: SafeArea(
        minimum: const EdgeInsets.fromLTRB(16, 0, 16, 12),
        child: SizedBox(
          width: double.infinity,
          child: FilledButton.icon(
            onPressed: _busy ? null : _evaluate,
            style: FilledButton.styleFrom(
              backgroundColor: j.color,
              padding: const EdgeInsets.symmetric(vertical: 16),
            ),
            icon: _busy
                ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                : const Icon(Icons.play_arrow_rounded),
            label: Text(_busy ? 'Deciding…' : 'Run decision'),
          ),
        ),
      ),
    );
  }
}

class _FieldRow extends StatelessWidget {
  const _FieldRow({required this.field, required this.value, required this.onChanged, required this.color});
  final JourneyField field;
  final num value;
  final ValueChanged<num> onChanged;
  final Color color;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    if (field.isToggle) {
      return SwitchListTile(
        contentPadding: EdgeInsets.zero,
        dense: true,
        activeThumbColor: color,
        title: Text(field.label, style: t.textTheme.bodyMedium),
        value: value == 1,
        onChanged: (v) => onChanged(v ? 1 : 0),
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(children: [
            Expanded(child: Text(field.label, style: t.textTheme.bodyMedium)),
            Text('${field.suffix == '₹' ? '₹' : ''}${value.toInt()}${field.suffix != null && field.suffix != '₹' ? ' ${field.suffix}' : ''}',
                style: t.textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w700, color: color)),
          ]),
          SliderTheme(
            data: SliderTheme.of(context).copyWith(trackHeight: 3),
            child: Slider(
              value: value.toDouble().clamp(field.min.toDouble(), field.max.toDouble()),
              min: field.min.toDouble(),
              max: field.max.toDouble(),
              activeColor: color,
              onChanged: (v) => onChanged(v.round()),
            ),
          ),
        ],
      ),
    );
  }
}

class ResultCard extends StatelessWidget {
  const ResultCard({super.key, required this.result, required this.expected, required this.color});
  final EvalResult result;
  final String? expected;
  final Color color;

  static const _palette = {
    'approve': Color(0xFF15803D),
    'reject': Color(0xFFB91C1C),
    'review': Color(0xFFB45309),
    'error': Color(0xFF6B7280),
  };

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    if (result.isError) {
      return Card(
        color: const Color(0xFFFEF2F2),
        elevation: 0,
        child: ListTile(
          leading: const Icon(Icons.error_outline, color: Color(0xFFB91C1C)),
          title: const Text('Evaluation failed'),
          subtitle: Text(result.error!, style: t.textTheme.bodySmall),
        ),
      );
    }
    final c = _palette[result.outcome] ?? _palette['error']!;
    final matches = expected == null || expected == result.outcome;

    return Card(
      elevation: 0,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Wrap, not Row+Spacer: the outcome chip and the score are both
            // variable-width, so there is no slack to give a Spacer.
            Wrap(
              spacing: 12,
              runSpacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: <Widget>[
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                  decoration: BoxDecoration(color: c.withValues(alpha: 0.12), borderRadius: BorderRadius.circular(8)),
                  child: Text(result.outcome.toUpperCase(),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: t.textTheme.titleSmall?.copyWith(color: c, fontWeight: FontWeight.w800, letterSpacing: 0.5)),
                ),
                if (result.score != null)
                  Text('Score ${result.score}',
                      maxLines: 1,
                      style: t.textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
              ],
            ),
            const SizedBox(height: 8),
            // Each metadatum is an indivisible icon+label pair, so they wrap as
            // units rather than being torn apart mid-pair.
            Wrap(
              spacing: 12,
              runSpacing: 6,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: <Widget>[
                _MetaChip(
                  icon: result.mode == EvalMode.server ? Icons.cloud_outlined : Icons.smartphone,
                  label: result.mode == EvalMode.server ? 'Server' : 'On-device',
                  color: t.hintColor,
                  style: t.textTheme.labelSmall,
                ),
                if (result.latencyMs != null)
                  _MetaChip(
                    icon: Icons.timer_outlined,
                    label: '${result.latencyMs} ms',
                    color: t.hintColor,
                    style: t.textTheme.labelSmall,
                  ),
                if (expected != null)
                  _MetaChip(
                    icon: matches ? Icons.check_circle : Icons.warning_amber_rounded,
                    label: matches ? 'as expected' : 'expected $expected',
                    color: matches ? const Color(0xFF15803D) : const Color(0xFFB45309),
                    style: t.textTheme.labelSmall,
                  ),
              ],
            ),
            const Divider(height: 24),
            Text('WHY', style: t.textTheme.labelSmall?.copyWith(letterSpacing: 0.8, color: t.hintColor)),
            const SizedBox(height: 6),
            if (result.gates.isEmpty)
              Text('No rule gates reported.', style: t.textTheme.bodySmall?.copyWith(color: t.hintColor))
            else
              ...result.gates.map((g) => GateTile(gate: g)),
          ],
        ),
      ),
    );
  }
}

class GateTile extends StatelessWidget {
  const GateTile({super.key, required this.gate});
  final GateResult gate;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    final c = gate.passed ? const Color(0xFF15803D) : const Color(0xFFB91C1C);
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(children: [
            Icon(gate.passed ? Icons.check_circle_outline : Icons.cancel_outlined, size: 16, color: c),
            const SizedBox(width: 6),
            Expanded(
              child: Text(gate.label,
                  style: t.textTheme.bodyMedium?.copyWith(
                      fontWeight: gate.passed ? FontWeight.w400 : FontWeight.w700, color: gate.passed ? null : c)),
            ),
          ]),
          for (final cond in gate.conditions)
            Padding(
              padding: const EdgeInsets.only(left: 22, top: 3),
              child: Text(
                '${cond.variable} ${cond.operator} ${cond.threshold}  ·  actual ${cond.value}',
                style: t.textTheme.labelSmall?.copyWith(
                    color: cond.passed ? t.hintColor : const Color(0xFFB91C1C),
                    fontFeatures: const []),
              ),
            ),
        ],
      ),
    );
  }
}

/// An icon + label pair that stays together when its parent wraps.
class _MetaChip extends StatelessWidget {
  const _MetaChip({
    required this.icon,
    required this.label,
    required this.color,
    this.style,
  });

  final IconData icon;
  final String label;
  final Color? color;
  final TextStyle? style;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Icon(icon, size: 14, color: color),
        const SizedBox(width: 4),
        Flexible(
          child: Text(
            label,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: style?.copyWith(color: color),
          ),
        ),
      ],
    );
  }
}
