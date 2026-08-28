import 'package:flutter/material.dart';
import 'package:rulemind_ui/rulemind_ui.dart';

import 'theme/theme.dart';
import 'theme/tokens.dart';
import 'widgets/section_header.dart';
import 'widgets/states.dart';

import 'engine.dart';
import 'journeys.dart';

void main() => runApp(const RuleMindDemoApp());


class RuleMindDemoApp extends StatelessWidget {
  const RuleMindDemoApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'RuleMind Journeys',
      debugShowCheckedModeBanner: false,
      theme: RmTheme.light,
      // Light only, and stated explicitly rather than left to the absence of a
      // darkTheme. The app previously shipped a dark ThemeData with no
      // themeMode, which meant it was *reachable* from the system setting --
      // and since every colour was light-tuned and that theme did not even set
      // a scaffold background, anyone with a dark phone saw a broken app.
      themeMode: ThemeMode.light,
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
          padding: RmScreenPadding.of(context, top: RmSpace.x5, extraBottom: RmSpace.x2),
          children: [
            Row(
              children: [
                Container(
                  width: 42, height: 42,
                  decoration: const BoxDecoration(color: RmC.accent, borderRadius: RmRadius.rMd),
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
                          style: t.textTheme.bodySmall?.copyWith(color: RmC.inkMuted)),
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
              style: t.textTheme.bodySmall?.copyWith(color: RmC.inkMuted),
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
                  style: t.textTheme.labelSmall?.copyWith(color: RmC.inkMuted)),
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
      true => (RmSem.approve.base, 'API up'),
      false => (RmSem.reject.base, 'API down'),
      // Neutral, not amber: 'checking' is not a warning, and the old amber
      // was a third one competing with the review verdict.
      _ => (RmC.inkFaint, 'checking'),
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
      borderRadius: RmRadius.rLg,
      child: InkWell(
        borderRadius: RmRadius.rLg,
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: [
              Container(
                width: 46, height: 46,
                decoration: BoxDecoration(
                  color: RmC.surfaceSunk,
                  borderRadius: RmRadius.rMd,
                ),
                child: Icon(journey.icon, color: journey.identity.mark, size: 24),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(journey.sector.toUpperCase(),
                        style: t.textTheme.labelSmall
                            ?.copyWith(color: journey.identity.mark, fontWeight: FontWeight.w700, letterSpacing: 0.6)),
                    const SizedBox(height: 2),
                    Text(journey.title,
                        style: t.textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600)),
                    const SizedBox(height: 4),
                    Text(journey.blurb,
                        style: t.textTheme.bodySmall?.copyWith(color: RmC.inkMuted, height: 1.35)),
                  ],
                ),
              ),
              const Icon(Icons.chevron_right, color: RmC.inkFaint),
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
    // The result region sits at the top of the list, so bring it into view.
    // The reason this exists is unchanged -- the verdict must not land where
    // the viewer cannot see it -- but the target moved with the region.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(
          0,
          duration: const Duration(milliseconds: 350),
          curve: Curves.easeOut,
        );
      }
    });
  }

  /// Empty, running, error or result — all in one always-present slot.
  Widget _resultRegion() {
    if (_busy) {
      return RmRunningState(
        label: _mode == EvalMode.server ? 'Running on server' : 'Running on device',
      );
    }
    final EvalResult? r = _result;
    if (r == null) return const RmResultPlaceholder();
    if (r.isError) {
      return RmErrorBand(
        message: r.error ?? 'Unknown error',
        onRetry: _evaluate,
      );
    }
    return ResultCard(result: r, expected: _active?.expected);
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
        backgroundColor: RmC.surface,
        foregroundColor: RmC.ink,
        // A hairline plus a 3dp identity rail. That rail is the entire
        // on-screen identity for the journey -- enough to say "you are in
        // Claims", not enough to compete with a verdict. The app bar itself
        // used to be a fully saturated journey colour, which made it the
        // largest colour field on the screen and the one place a
        // verdict-coloured fill is most confusing.
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(4),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              const Divider(height: 1, thickness: 1, color: RmC.line),
              Container(height: 3, color: j.identity.mark),
            ],
          ),
        ),
      ),
      body: ListView(
        controller: _scroll,
        padding: RmScreenPadding.of(context),
        children: [
          // The result region is first and always present, so a viewer can see
          // where the answer will appear before they ever press run, and the
          // region does not pop into existence and shove the form down.
          _resultRegion(),
          const SizedBox(height: RmSpace.x5),
          const RmSectionHeader('SCENARIOS'),
          const SizedBox(height: 8),
          // LayoutBuilder, not MediaQuery: the cap must come from the box these
          // chips are actually given. A scenario name is content-driven, and at
          // large text scales its intrinsic width can exceed the screen -- which
          // a widget test cannot detect, because tests render with a placeholder
          // font rather than the real one. Caught on an iOS simulator at 2.0x,
          // where "Serial claimant, new policy" was truncated by the screen edge.
          LayoutBuilder(
            builder: (BuildContext context, BoxConstraints constraints) {
              return Wrap(
                spacing: 8, runSpacing: 8,
                children: j.scenarios.map((s) {
                  final sel = _active?.name == s.name;
                  return ConstrainedBox(
                    constraints: BoxConstraints(maxWidth: constraints.maxWidth),
                    child: ChoiceChip(
                      label: Text(s.name, maxLines: 2, overflow: TextOverflow.ellipsis),
                      selected: sel,
                      onSelected: (_) => _apply(s),
                      selectedColor: RmC.accentSoft,
                      // copyWith on the theme style, not a bare TextStyle --
                      // a bare one inherits nothing, so the chip label silently
                      // opted out of the TextTheme entirely.
                      labelStyle: t.chipTheme.labelStyle?.copyWith(
                        color: sel ? RmC.accent : RmC.ink,
                        fontWeight: sel ? FontWeight.w700 : FontWeight.w600,
                      ),
                    ),
                  );
                }).toList(),
              );
            },
          ),
          if (_active != null) ...[
            const SizedBox(height: 8),
            Text(_active!.blurb, style: t.textTheme.bodySmall?.copyWith(color: RmC.inkMuted, height: 1.35)),
          ],
          const SizedBox(height: 18),
          RmLabeledRow(
            label: const RmSectionHeader('INPUTS'),
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
                      onChanged: (v) => setState(() {
                        _values[f.key] = v;
                        _result = null;
                      }),
                    )).toList(),
              ),
            ),
          ),
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
            // Colour comes from filledButtonTheme; there is no per-journey
            // override any more.
            style: null,
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
  const _FieldRow({required this.field, required this.value, required this.onChanged});
  final JourneyField field;
  final num value;
  final ValueChanged<num> onChanged;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    if (field.isToggle) {
      return SwitchListTile(
        contentPadding: EdgeInsets.zero,
        dense: true,
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
                style: t.textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w700, color: RmC.ink)),
          ]),
          SliderTheme(
            data: SliderTheme.of(context).copyWith(trackHeight: 3),
            child: Slider(
              value: value.toDouble().clamp(field.min.toDouble(), field.max.toDouble()),
              min: field.min.toDouble(),
              max: field.max.toDouble(),
              onChanged: (v) => onChanged(v.round()),
            ),
          ),
        ],
      ),
    );
  }
}

class ResultCard extends StatelessWidget {
  const ResultCard({super.key, required this.result, required this.expected});
  final EvalResult result;
  final String? expected;


  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    if (result.isError) {
      return Card(
        color: RmSem.reject.soft,
        elevation: 0,
        child: ListTile(
          leading: Icon(Icons.error_outline, color: RmSem.reject.base),
          title: const Text('Evaluation failed'),
          subtitle: Text(result.error!, style: t.textTheme.bodySmall),
        ),
      );
    }
    final c = RmSem.forOutcome(result.outcome);
    final matches = expected == null || expected == result.outcome;
    // An on-device verdict that skipped workflow-only steps is not comparable
    // to a server verdict, so it must not be reported as "as expected".
    final partial = result.serverOnlyStepsSkipped.isNotEmpty;

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
                  decoration: BoxDecoration(color: c.soft, borderRadius: RmRadius.rSm),
                  child: Text(result.outcome.toUpperCase(),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: t.textTheme.titleSmall?.copyWith(color: c.ink, letterSpacing: 0.5)),
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
                  color: RmC.inkFaint,
                  style: t.textTheme.labelSmall,
                ),
                if (result.latencyMs != null)
                  _MetaChip(
                    icon: Icons.timer_outlined,
                    label: '${result.latencyMs} ms',
                    color: RmC.inkFaint,
                    style: t.textTheme.labelSmall,
                  ),
                if (expected != null)
                  _MetaChip(
                    icon: partial
                        ? Icons.info_outline
                        : (matches ? Icons.check_circle : Icons.warning_amber_rounded),
                    label: partial
                        ? 'partial — not comparable'
                        : (matches ? 'as expected' : 'expected $expected'),
                    color: partial
                        ? RmSem.info.ink
                        : (matches ? RmSem.approve.ink : RmSem.review.ink),
                    style: t.textTheme.labelSmall,
                  ),
                if (result.pendingSyncCount != null)
                  _MetaChip(
                    icon: result.pendingSyncCount! == 0
                        ? Icons.cloud_done_outlined
                        : Icons.cloud_upload_outlined,
                    label: result.pendingSyncCount! == 0
                        ? 'synced'
                        : '${result.pendingSyncCount} queued',
                    color: result.pendingSyncCount! == 0
                        ? RmSem.approve.ink
                        : RmSem.review.ink,
                    style: t.textTheme.labelSmall,
                  ),
              ],
            ),
            if (partial) ...[
              const SizedBox(height: 12),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: RmSem.info.soft,
                  borderRadius: RmRadius.rSm,
                  border: Border.all(color: RmSem.info.base),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(children: [
                      Icon(Icons.info_outline, size: 15, color: RmSem.info.base),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          'Partial decision — ${result.serverOnlyStepsSkipped.length} '
                          'server-only step${result.serverOnlyStepsSkipped.length == 1 ? '' : 's'} skipped',
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: t.textTheme.labelMedium?.copyWith(
                              color: RmSem.info.ink, fontWeight: FontWeight.w700),
                        ),
                      ),
                    ]),
                    const SizedBox(height: 6),
                    Text(
                      'On-device mode evaluates variables, rules, scorecards and '
                      'policies. These steps need the server: '
                      '${result.serverOnlyStepsSkipped.join(', ')}.',
                      style: t.textTheme.bodySmall?.copyWith(color: RmSem.info.ink, height: 1.35),
                    ),
                  ],
                ),
              ),
            ],
            const Divider(height: 24),
            const RmSectionHeader('WHY'),
            const SizedBox(height: 6),
            if (result.gates.isEmpty)
              Text('No rule gates reported.', style: t.textTheme.bodySmall?.copyWith(color: RmC.inkMuted))
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
    final c = gate.passed ? RmSem.approve.base : RmSem.reject.base;
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
                    color: cond.passed ? RmC.inkMuted : RmSem.reject.ink,
                    fontFeatures: kTabular),
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
