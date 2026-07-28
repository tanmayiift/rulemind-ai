import 'dart:ui' show FontVariation;

import 'package:flutter/material.dart';
import 'package:rulemind/rulemind.dart';

import 'state/app_state.dart';

const Color _rmPrimary = Color(0xFF005394);
const Color _rmPrimaryContainer = Color(0xFF2B6CB0);
const Color _rmSurface = Color(0xFFF7FAFC);
const Color _rmSurfaceLow = Color(0xFFF1F4F6);
const Color _rmSurfaceHigh = Color(0xFFE5E9EB);
const Color _rmOutline = Color(0xFFC1C7D2);
const Color _rmInk = Color(0xFF181C1E);
const Color _rmMuted = Color(0xFF6F6A73);

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const RuleMindStudioApp());
}

class RuleMindStudioApp extends StatefulWidget {
  const RuleMindStudioApp({super.key});

  @override
  State<RuleMindStudioApp> createState() => _RuleMindStudioAppState();
}

class _RuleMindStudioAppState extends State<RuleMindStudioApp> {
  final AppState _state = AppState();

  @override
  void initState() {
    super.initState();
    _state.loadSavedConfig();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _state,
      builder: (BuildContext context, Widget? child) {
        return MaterialApp(
          title: 'RuleMind Experience Studio',
          theme: ThemeData(
            useMaterial3: true,
            fontFamily: 'Inter',
            colorScheme: const ColorScheme.light(
              primary: _rmPrimary,
              onPrimary: Colors.white,
              primaryContainer: _rmPrimaryContainer,
              secondary: _rmPrimaryContainer,
              surface: _rmSurface,
              onSurface: _rmInk,
              onSurfaceVariant: _rmMuted,
              outlineVariant: _rmOutline,
              error: Color(0xFFBA1A1A),
            ),
            scaffoldBackgroundColor: _rmSurface,
            appBarTheme: const AppBarTheme(
              backgroundColor: _rmSurfaceLow,
              foregroundColor: _rmInk,
              elevation: 0,
            ),
            navigationBarTheme: const NavigationBarThemeData(
              backgroundColor: _rmSurfaceHigh,
              elevation: 0,
            ),
            cardTheme: CardThemeData(
              color: Colors.white,
              elevation: 0,
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(28)),
            ),
            textTheme: TextTheme(
              headlineMedium: const TextStyle(
                fontFamily: 'Manrope',
                fontSize: 30,
                height: 1.2,
                fontWeight: FontWeight.w800,
                color: _rmInk,
              ).copyWith(fontVariations: const <FontVariation>[FontVariation('wght', 800)]),
              headlineSmall: const TextStyle(
                fontFamily: 'Manrope',
                fontSize: 24,
                height: 1.25,
                fontWeight: FontWeight.w700,
                color: _rmInk,
              ).copyWith(fontVariations: const <FontVariation>[FontVariation('wght', 700)]),
              titleLarge: const TextStyle(fontFamily: 'Inter', fontSize: 22, height: 1.25, fontWeight: FontWeight.w700, color: _rmInk),
              titleMedium: const TextStyle(fontFamily: 'Inter', fontSize: 16, height: 1.35, fontWeight: FontWeight.w600, color: _rmInk),
              bodyLarge: const TextStyle(fontFamily: 'Inter', fontSize: 16, height: 1.5, fontWeight: FontWeight.w400, color: _rmInk),
              bodyMedium: const TextStyle(fontFamily: 'Inter', fontSize: 14, height: 1.55, fontWeight: FontWeight.w400, color: _rmInk),
              bodySmall: const TextStyle(fontFamily: 'Inter', fontSize: 12, height: 1.45, fontWeight: FontWeight.w400, color: _rmInk),
              labelMedium: const TextStyle(fontFamily: 'Inter', fontSize: 12, height: 1.25, fontWeight: FontWeight.w500, color: _rmMuted),
              labelSmall: const TextStyle(fontFamily: 'Inter', fontSize: 11, height: 1.2, fontWeight: FontWeight.w700, color: _rmMuted, letterSpacing: 0.8),
            ),
            inputDecorationTheme: InputDecorationTheme(
              filled: true,
              fillColor: _rmSurfaceHigh,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(20),
                borderSide: BorderSide(color: _rmOutline.withOpacity(0.15)),
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(20),
                borderSide: BorderSide(color: _rmOutline.withOpacity(0.15)),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(20),
                borderSide: BorderSide(color: _rmPrimary.withOpacity(0.35), width: 2),
              ),
            ),
          ),
          home: StudioShell(state: _state),
        );
      },
    );
  }
}

class StudioShell extends StatelessWidget {
  const StudioShell({super.key, required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text(_titleForRoute(state), overflow: TextOverflow.ellipsis),
            if (state.connectionStatus != null)
              Text(
                state.connectionStatus!,
                key: const ValueKey<String>('status_connection'),
                style: Theme.of(context).textTheme.labelSmall?.copyWith(color: Theme.of(context).colorScheme.onSurfaceVariant),
              ),
          ],
        ),
        leading: state.route == 'access'
            ? null
            : IconButton(
                icon: const Icon(Icons.arrow_back),
                onPressed: () => _handleBack(),
              ),
        actions: <Widget>[
          if (state.mode != null)
            IconButton(
              icon: const Icon(Icons.logout),
              onPressed: () => state.logout(),
            ),
        ],
      ),
      bottomNavigationBar: state.mode != null && !<String>{'access', 'experience_selection'}.contains(state.route)
          ? NavigationBar(
              selectedIndex: _selectedIndex(),
              onDestinationSelected: (int index) {
                final List<String> routes = <String>['home_dashboard', 'scenario_hub', 'logs_explainability', 'admin_console'];
                state.goTo(routes[index], journeyId: state.currentJourneyId);
              },
              destinations: const <NavigationDestination>[
                NavigationDestination(icon: Icon(Icons.home), label: 'Home', tooltip: 'nav_home_dashboard'),
                NavigationDestination(icon: Icon(Icons.science), label: 'Scenarios', tooltip: 'nav_scenario_hub'),
                NavigationDestination(icon: Icon(Icons.terminal), label: 'Logs', tooltip: 'nav_logs_explainability'),
                NavigationDestination(icon: Icon(Icons.settings), label: 'Admin', tooltip: 'nav_admin_console'),
              ],
            )
          : null,
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: _buildBody(context),
        ),
      ),
    );
  }

  int _selectedIndex() {
    switch (state.route) {
      case 'scenario_hub':
        return 1;
      case 'logs_explainability':
        return 2;
      case 'admin_console':
        return 3;
      default:
        return 0;
    }
  }

  String _titleForRoute(AppState state) {
    switch (state.route) {
      case 'access':
        return 'RuleMind Studio';
      case 'experience_selection':
        return 'Experience Selection';
      case 'home_dashboard':
        return 'Home Dashboard';
      case 'scenario_hub':
        return 'Scenario Hub';
      case 'logs_explainability':
        return 'Logs & Explainability';
      case 'admin_console':
        return 'Admin Console';
      case 'sandbox':
        return 'Dynamic Sandbox';
      default:
        final Map<String, dynamic>? journey = state.journeys.firstWhere(
          (Map<String, dynamic> item) => (item['screens'] as List<dynamic>).any((dynamic screen) => (screen as Map<String, dynamic>)['id'] == state.route),
          orElse: () => <String, dynamic>{},
        );
        return (journey?['title'] ?? 'RuleMind Experience Studio').toString();
    }
  }

  void _handleBack() {
    switch (state.route) {
      case 'experience_selection':
        state.goTo('access');
        break;
      case 'home_dashboard':
      case 'scenario_hub':
      case 'logs_explainability':
      case 'admin_console':
      case 'sandbox':
        state.goTo('experience_selection');
        break;
      default:
        final Map<String, dynamic>? journey = state.journeys.firstWhere(
          (Map<String, dynamic> item) => (item['screens'] as List<dynamic>).any((dynamic screen) => (screen as Map<String, dynamic>)['id'] == state.route),
          orElse: () => <String, dynamic>{},
        );
        final List<dynamic> screens = journey?['screens'] as List<dynamic>? ?? const <dynamic>[];
        final int index = screens.indexWhere((dynamic item) => (item as Map<String, dynamic>)['id'] == state.route);
        if (index > 0) {
          state.goTo((screens[index - 1] as Map<String, dynamic>)['id'] as String, journeyId: journey?['id']?.toString());
        } else {
          state.goTo('home_dashboard');
        }
    }
  }

  Widget _buildBody(BuildContext context) {
    switch (state.route) {
      case 'access':
        return _AccessView(state: state);
      case 'experience_selection':
        return _ExperienceSelectionView(state: state);
      case 'home_dashboard':
        return _HomeDashboardView(state: state);
      case 'scenario_hub':
        return _ScenarioHubView(state: state);
      case 'logs_explainability':
        return _LogsView(state: state);
      case 'admin_console':
        return _AdminView(state: state);
      case 'sandbox':
        return _SandboxView(state: state);
      default:
        return _JourneyView(state: state);
    }
  }
}

class _AccessView extends StatelessWidget {
  const _AccessView({required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const ValueKey<String>('route_access'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _hero(context, 'RuleMind Experience Studio', 'A single super-app for travel, loan, and SME decision journeys with explainability, workflow orchestration, and mobile authoring.'),
        const SizedBox(height: 16),
        _card(
          context,
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('Offline Demo Studio', style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              const Text('Use the bundled multi-journey experience locally, including scenarios, callbacks, review pauses, and audit views.'),
              const SizedBox(height: 16),
              FilledButton(
                key: const ValueKey<String>('action_enter_demo'),
                onPressed: state.isLoading
                    ? null
                    : () async {
                        await state.startDemo();
                      },
                child: const Text('Enter Demo Studio'),
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),
        _card(
          context,
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('Admin Live Mode', style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey<String>('input_server_url'),
                initialValue: state.baseUrl,
                onChanged: state.updateBaseUrl,
                decoration: InputDecoration(
                  labelText: 'Server URL',
                  errorText: state.loginErrors['url'],
                ),
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey<String>('input_email'),
                initialValue: state.email,
                onChanged: state.updateEmail,
                decoration: InputDecoration(
                  labelText: 'Email',
                  errorText: state.loginErrors['email'],
                ),
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey<String>('input_password'),
                initialValue: state.password,
                onChanged: state.updatePassword,
                decoration: const InputDecoration(labelText: 'Password'),
                obscureText: true,
              ),
              const SizedBox(height: 16),
              FilledButton(
                key: const ValueKey<String>('action_connect_live'),
                onPressed: state.isLoading ? null : () async => state.loginLive(),
                child: state.isLoading ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2)) : const Text('Connect To Live Studio'),
              ),
            ],
          ),
        ),
        if (state.error != null) ...<Widget>[
          const SizedBox(height: 16),
          Text(state.error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
        ],
      ],
    );
  }
}

class _ExperienceSelectionView extends StatelessWidget {
  const _ExperienceSelectionView({required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const ValueKey<String>('route_experience_selection'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _hero(context, 'Choose A Journey', state.content['tagline']?.toString() ?? ''),
        const SizedBox(height: 12),
        FilledButton(
          key: const ValueKey<String>('action_open_home_dashboard'),
          onPressed: () => state.goTo('home_dashboard'),
          child: const Text('Open Home Dashboard'),
        ),
        const SizedBox(height: 16),
        for (final Map<String, dynamic> journey in state.journeys.cast<Map<String, dynamic>>()) ...<Widget>[
          _journeyCard(
            context,
            journey,
            onTap: () {
              state.currentJourneyId = journey['id'] as String;
              state.goTo(((journey['screens'] as List<dynamic>).first as Map<String, dynamic>)['id'] as String, journeyId: journey['id'] as String);
            },
          ),
          const SizedBox(height: 16),
        ],
      ],
    );
  }
}

class _HomeDashboardView extends StatelessWidget {
  const _HomeDashboardView({required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const ValueKey<String>('route_home_dashboard'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _hero(context, 'Decision Journeys In Motion', 'Navigate live or demo decisioning across travel, lending, and SME underwriting with explainability and admin controls in one shell.'),
        const SizedBox(height: 12),
        Row(
          children: <Widget>[
            Expanded(
              child: FilledButton.icon(
                key: const ValueKey<String>('action_sync_bundle'),
                onPressed: () => state.syncBundle(),
                icon: const Icon(Icons.refresh),
                label: const Text('Sync'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: OutlinedButton.icon(
                key: const ValueKey<String>('action_open_scenarios'),
                onPressed: () => state.goTo('scenario_hub'),
                icon: const Icon(Icons.science),
                label: const Text('Scenarios'),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        SizedBox(
          width: double.infinity,
          child: OutlinedButton.icon(
            key: const ValueKey<String>('action_open_sandbox'),
            onPressed: () => state.goTo('sandbox'),
            icon: const Icon(Icons.science),
            label: const Text('Open Sandbox'),
          ),
        ),
        const SizedBox(height: 16),
        for (final List<Map<String, dynamic>> row in _chunk(state.logMetrics.cast<Map<String, dynamic>>(), 2)) ...<Widget>[
          Row(
            children: <Widget>[
              for (final Map<String, dynamic> metric in row) ...<Widget>[
                Expanded(child: _metricCard(context, metric)),
                if (metric != row.last) const SizedBox(width: 12),
              ],
              if (row.length == 1) const Expanded(child: SizedBox.shrink()),
            ],
          ),
          const SizedBox(height: 12),
        ],
        const SizedBox(height: 8),
        for (final Map<String, dynamic> journey in state.journeys.cast<Map<String, dynamic>>()) ...<Widget>[
          _journeyDashboardCard(context, state, journey),
          const SizedBox(height: 16),
        ],
      ],
    );
  }
}

class _ScenarioHubView extends StatelessWidget {
  const _ScenarioHubView({required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const ValueKey<String>('route_scenario_hub'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _hero(context, 'Deterministic Personas', 'Run seeded scenarios across all journeys, including review pauses and callback queues.'),
        const SizedBox(height: 16),
        for (final Map<String, dynamic> journey in state.journeys.cast<Map<String, dynamic>>()) ...<Widget>[
          Text(journey['title'] as String, style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 12),
          for (final Map<String, dynamic> scenario in state.scenarios.cast<Map<String, dynamic>>().where((Map<String, dynamic> item) => item['journeyId'] == journey['id'])) ...<Widget>[
            _card(
              context,
              Column(
                key: ValueKey<String>('scenario_card_${scenario['id']}'),
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Row(
                    children: <Widget>[
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: <Widget>[
                            Text(scenario['title'] as String, style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                            Text('${scenario['badge']} • expects ${scenario['expectedOutcome']}/${scenario['expectedStatus']}'),
                          ],
                        ),
                      ),
                      Chip(label: Text(scenario['badge'] as String)),
                    ],
                  ),
                  const SizedBox(height: 12),
                  FilledButton.icon(
                    key: ValueKey<String>('action_run_scenario_${scenario['id']}'),
                    onPressed: () async {
                      await state.runScenario(scenario['id'] as String);
                      state.currentJourneyId = journey['id'] as String;
                      state.goTo(_journeyOutcomeRoute(journey), journeyId: journey['id'] as String);
                    },
                    icon: const Icon(Icons.play_arrow),
                    label: const Text('Run Scenario'),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 12),
          ],
          const SizedBox(height: 12),
        ],
      ],
    );
  }
}

class _LogsView extends StatelessWidget {
  const _LogsView({required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const ValueKey<String>('route_logs_explainability'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _hero(context, 'Logs & Explainability', 'Audit every execution, rule result, scorecard, and callback from the mobile shell.'),
        const SizedBox(height: 16),
        for (final Map<String, dynamic> item in state.history) ...<Widget>[
          _card(
            context,
            Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text('${_journeyTitle(state, item['journeyId'] as String)} • ${(item['outcome'] as String).toUpperCase()}', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                Text('${item['status']} • ${formatNumber((item['score'] as num?)?.toDouble())} • ${item['timestamp']}'),
                if (item['executionId'] != null) Text('Execution ID: ${item['executionId']}', style: Theme.of(context).textTheme.labelSmall),
              ],
            ),
          ),
          const SizedBox(height: 12),
        ],
        const SizedBox(height: 8),
        for (final LogEntry entry in state.logs.reversed) ...<Widget>[
          Text(
            '${entry.timestamp}  ${entry.message}',
            style: TextStyle(color: entry.isError ? Theme.of(context).colorScheme.error : Theme.of(context).colorScheme.onSurface),
          ),
          const SizedBox(height: 8),
        ],
      ],
    );
  }
}

class _AdminView extends StatelessWidget {
  const _AdminView({required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    final Map<String, dynamic>? selectedSchema = state.selectedEntityId == null
        ? null
        : state.adminEntities.cast<Map<String, dynamic>>().firstWhere(
              (Map<String, dynamic> item) => item['id'] == state.selectedEntityId,
              orElse: () => <String, dynamic>{},
            );
    return Column(
      key: const ValueKey<String>('route_admin_console'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _hero(context, 'Admin Console', 'Mobile-safe authoring and controls for live RuleMind environments.'),
        if (state.availableTenants.isNotEmpty) ...<Widget>[
          Text('Tenant Context', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: state.availableTenants.map((Map<String, dynamic> tenant) {
              return ChoiceChip(
                key: ValueKey<String>('tenant_${tenant['id']}'),
                label: Text(tenant['name']?.toString() ?? tenant['id']?.toString() ?? ''),
                selected: tenant['id']?.toString() == state.tenantId,
                onSelected: (_) => state.switchTenant(tenant['id']?.toString() ?? ''),
              );
            }).toList(),
          ),
          const SizedBox(height: 16),
        ],
        Text('Tools', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
        const SizedBox(height: 12),
        for (final Map<String, dynamic> tool in state.adminTools.cast<Map<String, dynamic>>()) ...<Widget>[
          _card(
            context,
            Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(tool['title'] as String, style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                Text(tool['description'] as String),
              ],
            ),
          ),
          const SizedBox(height: 12),
        ],
        Text('Entities', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
        const SizedBox(height: 12),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: state.adminEntities.cast<Map<String, dynamic>>().map((Map<String, dynamic> entity) {
            return ChoiceChip(
              key: ValueKey<String>('admin_entity_${entity['id']}'),
              label: Text(entity['title'] as String),
              selected: entity['id'] == state.selectedEntityId,
              onSelected: (_) {
                state.selectAdminEntity(entity['id'] as String);
                state.loadAdminRecords(entity['id'] as String);
              },
            );
          }).toList(),
        ),
        const SizedBox(height: 16),
        if (state.mode != 'live')
          _card(
            context,
            const Text('Live sign-in unlocks create, update, and delete actions against the backend. Demo mode keeps the surfaces visible but read-only.'),
          ),
        if (state.adminStatus != null) ...<Widget>[
          Text(state.adminStatus!, style: TextStyle(color: Theme.of(context).colorScheme.primary)),
          const SizedBox(height: 12),
        ],
        if (selectedSchema != null) ...<Widget>[
          Row(
            children: <Widget>[
              Expanded(
                child: FilledButton(
                  key: const ValueKey<String>('action_admin_create'),
                  onPressed: selectedSchema['supportsCreate'] == true && state.mode == 'live' ? state.startAdminCreate : null,
                  child: Text('Create ${selectedSchema['title'].toString().replaceAll(RegExp(r's$'), '')}'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: OutlinedButton(
                  key: const ValueKey<String>('action_admin_refresh'),
                  onPressed: () => state.loadAdminRecords(selectedSchema['id'] as String),
                  child: const Text('Refresh'),
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
          for (final Map<String, dynamic> record in state.adminRecords[selectedSchema['id']] ?? <Map<String, dynamic>>[]) ...<Widget>[
            _card(
              context,
              Column(
                key: ValueKey<String>('admin_record_${record['id'] ?? 'settings'}'),
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text((record['name'] ?? record['title'] ?? record['policy_id'] ?? record['id']).toString(), style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                  Text((record['id'] ?? 'settings').toString(), style: Theme.of(context).textTheme.labelSmall),
                  const SizedBox(height: 12),
                  Row(
                    children: <Widget>[
                      Expanded(
                        child: FilledButton(
                          key: ValueKey<String>('action_admin_edit_${record['id'] ?? 'settings'}'),
                          onPressed: () => state.editAdminRecord(record),
                          child: const Text('Edit'),
                        ),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: OutlinedButton(
                          key: ValueKey<String>('action_admin_delete_${record['id'] ?? 'settings'}'),
                          onPressed: selectedSchema['supportsDelete'] == true && state.mode == 'live' ? () => state.deleteAdminRecord((record['id'] ?? 'settings').toString()) : null,
                          child: const Text('Delete'),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
            const SizedBox(height: 12),
          ],
          if (state.isCreatingRecord || state.editingRecordId != null) _adminEditor(context, state, selectedSchema),
        ],
      ],
    );
  }
}

class _JourneyView extends StatelessWidget {
  const _JourneyView({required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    final Map<String, dynamic> journey = state.journeys.cast<Map<String, dynamic>>().firstWhere(
          (Map<String, dynamic> item) => (item['screens'] as List<dynamic>).any((dynamic screen) => (screen as Map<String, dynamic>)['id'] == state.route),
          orElse: () => <String, dynamic>{},
        );
    final Map<String, dynamic> screen = (journey['screens'] as List<dynamic>).cast<Map<String, dynamic>>().firstWhere(
          (Map<String, dynamic> item) => item['id'] == state.route,
          orElse: () => <String, dynamic>{},
        );
    final Decision? decision = state.decisions[journey['id']];
    final Map<String, dynamic> draft = Map<String, dynamic>.from(state.drafts[journey['id']] ?? <String, dynamic>{});
    return Column(
      key: ValueKey<String>('route_${screen['id']}'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _card(
          context,
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(journey['title']?.toString() ?? '', style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 4),
              Text(screen['title']?.toString() ?? '', style: Theme.of(context).textTheme.headlineSmall),
              const SizedBox(height: 4),
              Text(screen['subtitle']?.toString() ?? ''),
            ],
          ),
        ),
        const SizedBox(height: 16),
        switch (screen['type']) {
          'landing' => Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                for (final String bullet in (screen['bullets'] as List<dynamic>? ?? const <dynamic>[]).cast<String>()) ...<Widget>[
                  Row(
                    children: <Widget>[
                      const CircleAvatar(radius: 4, backgroundColor: _rmPrimary),
                      const SizedBox(width: 12),
                      Expanded(child: Text(bullet)),
                    ],
                  ),
                  const SizedBox(height: 10),
                ],
                FilledButton(
                  key: ValueKey<String>('action_begin_${journey['id']}'),
                  onPressed: () => state.goTo(((journey['screens'] as List<dynamic>)[1] as Map<String, dynamic>)['id'] as String, journeyId: journey['id'] as String),
                  child: Text('Begin ${journey['title']}'),
                ),
              ],
            ),
          'form' => Column(
              children: <Widget>[
                for (final Map<String, dynamic> field in (screen['fields'] as List<dynamic>).cast<Map<String, dynamic>>()) ...<Widget>[
                  _journeyField(context, state, journey['id'] as String, field, draft[field['id'] as String]),
                  const SizedBox(height: 12),
                ],
                Row(
                  children: <Widget>[
                    Expanded(
                      child: OutlinedButton(
                        key: const ValueKey<String>('action_back'),
                        onPressed: () => _goBackInJourney(state, journey, screen),
                        child: const Text('Back'),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: FilledButton(
                        key: const ValueKey<String>('action_continue'),
                        onPressed: () => _goForwardInJourney(state, journey, screen),
                        child: const Text('Continue'),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          'processing' => Column(
              children: <Widget>[
                _card(
                  context,
                  Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text(screen['title'] as String, style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      Text(screen['subtitle'] as String),
                      const SizedBox(height: 12),
                      Text(decision == null ? 'No execution has been run yet for this journey.' : 'Outcome: ${decision.outcome.toUpperCase()} • Status: ${decision.status} • callbacks: ${decision.pendingOperations.where((Map<String, dynamic> item) => item['status'] != 'delivered').length}'),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                FilledButton(
                  key: const ValueKey<String>('action_run_decision'),
                  onPressed: () => state.evaluateJourney(journey['id'] as String),
                  child: Text(decision == null ? 'Run RuleMind Decision' : 'Re-run Decision'),
                ),
                const SizedBox(height: 12),
                OutlinedButton(
                  key: const ValueKey<String>('action_continue_after_processing'),
                  onPressed: () => _goForwardInJourney(state, journey, screen),
                  child: const Text('Continue To Next Step'),
                ),
              ],
            ),
          'outcome' => _OutcomeView(state: state, journey: journey, decision: decision),
          _ => _AuditView(state: state, decision: decision, journeyId: journey['id'] as String),
        },
        if (state.error != null) ...<Widget>[
          const SizedBox(height: 16),
          Text(state.error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
        ],
      ],
    );
  }
}

class _OutcomeView extends StatelessWidget {
  const _OutcomeView({required this.state, required this.journey, required this.decision});

  final AppState state;
  final Map<String, dynamic> journey;
  final Decision? decision;

  @override
  Widget build(BuildContext context) {
    if (decision == null) {
      return _card(
        context,
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text('No decision yet', style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
            const SizedBox(height: 8),
            const Text('Complete the forms and run the decision to populate outcome, callbacks, and explainability.'),
            const SizedBox(height: 12),
            FilledButton(
              key: const ValueKey<String>('action_run_decision'),
              onPressed: () => state.evaluateJourney(journey['id'] as String),
              child: const Text('Run Decision'),
            ),
          ],
        ),
      );
    }
    return Column(
      children: <Widget>[
        _card(
          context,
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(20),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(24),
              gradient: LinearGradient(
                colors: <Color>[
                  _outcomeColor(decision!.outcome),
                  _outcomeColor(decision!.outcome).withOpacity(0.72),
                ],
              ),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(decision!.outcome.toUpperCase(), style: Theme.of(context).textTheme.headlineSmall?.copyWith(color: Colors.white, fontWeight: FontWeight.w800)),
                const SizedBox(height: 4),
                Text('Evaluated in ${decision!.latencyMs}ms', style: const TextStyle(color: Colors.white70)),
              ],
            ),
          ),
        ),
        const SizedBox(height: 16),
        if (decision!.score != null)
          _card(
            context,
            Row(
              children: <Widget>[
                SizedBox(
                  width: 120,
                  height: 120,
                  child: Stack(
                    alignment: Alignment.center,
                    children: <Widget>[
                      CircularProgressIndicator(
                        value: (decision!.score! / 900).clamp(0.0, 1.0),
                        strokeWidth: 10,
                        backgroundColor: const Color(0xFFE6EBF0),
                      ),
                      Column(
                        mainAxisSize: MainAxisSize.min,
                        children: <Widget>[
                          Text(formatNumber(decision!.score), style: Theme.of(context).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w800)),
                          const Text('/ 900'),
                        ],
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 16),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text('Journey Score', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      if (journey['id'] == 'instant_personal_loan') ...<Widget>[
                        Text('Bureau Score: ${decision!.variables['loan_bureau_score'] ?? '—'}'),
                        const SizedBox(height: 4),
                        const Text('Risk Score is the scorecard result, not the applicant bureau input.'),
                      ] else
                        Text('Trace steps: ${decision!.trace.length} • queued callbacks: ${decision!.pendingOperations.where((Map<String, dynamic> item) => item['status'] != 'delivered').length}'),
                    ],
                  ),
                ),
              ],
            ),
          ),
        const SizedBox(height: 16),
        _card(
          context,
          Column(
            key: const ValueKey<String>('decision_summary'),
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('Decision Summary', style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              Text('Outcome: ${decision!.outcome.toUpperCase()} • Status: ${decision!.status}'),
              Text('Source: ${decision!.source ?? 'local'}'),
              Text('Execution ID: ${decision!.executionId ?? 'Not assigned'}', key: const ValueKey<String>('execution_id'), style: Theme.of(context).textTheme.labelSmall),
              Text('Request ID: ${decision!.requestId ?? '—'}', key: const ValueKey<String>('request_id'), style: Theme.of(context).textTheme.labelSmall),
              if (decision!.variables.isNotEmpty) ...<Widget>[
                const SizedBox(height: 12),
                Text('Computed Variables', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                const SizedBox(height: 8),
                for (final MapEntry<String, dynamic> entry in decision!.variables.entries.take(8)) Text('${entry.key}: ${entry.value}'),
              ],
            ],
          ),
        ),
        const SizedBox(height: 12),
        if (decision!.pendingOperations.any((Map<String, dynamic> item) => item['status'] != 'delivered'))
          FilledButton.icon(
            key: const ValueKey<String>('action_flush_pending_callbacks'),
            onPressed: () => state.flushPendingOperations(journey['id'] as String),
            icon: const Icon(Icons.refresh),
            label: const Text('Flush Pending Callbacks'),
          ),
        if (decision!.status == 'paused') ...<Widget>[
          const SizedBox(height: 12),
          Row(
            children: <Widget>[
              Expanded(
                child: FilledButton.icon(
                  key: const ValueKey<String>('action_review_approve'),
                  onPressed: () => state.resumeReview(journey['id'] as String, true),
                  icon: const Icon(Icons.check),
                  label: const Text('Approve Review'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: OutlinedButton(
                  key: const ValueKey<String>('action_review_reject'),
                  onPressed: () => state.resumeReview(journey['id'] as String, false),
                  child: const Text('Reject Review'),
                ),
              ),
            ],
          ),
        ],
        const SizedBox(height: 12),
        OutlinedButton(
          key: const ValueKey<String>('action_open_audit'),
          onPressed: () => state.goTo(_journeyAuditRoute(journey), journeyId: journey['id'] as String),
          child: const Text('Open Explainability & Audit'),
        ),
      ],
    );
  }
}

class _AuditView extends StatelessWidget {
  const _AuditView({required this.state, required this.decision, required this.journeyId});

  final AppState state;
  final Decision? decision;
  final String journeyId;

  @override
  Widget build(BuildContext context) {
    if (decision == null) {
      return _card(context, const Text('Run a decision to unlock explainability.', key: ValueKey<String>('trace_empty')));
    }
    return Column(
      children: <Widget>[
        _card(
          context,
          Column(
            key: const ValueKey<String>('audit_summary'),
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('Decision Summary', style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              Text('Outcome: ${decision!.outcome.toUpperCase()} • Status: ${decision!.status}'),
              Text('Trace steps: ${decision!.trace.length}'),
            ],
          ),
        ),
        const SizedBox(height: 12),
        for (int i = 0; i < decision!.trace.length; i++) ...<Widget>[
          _card(
            context,
            Column(
              key: ValueKey<String>('trace_item_$i'),
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text('${i + 1}. ${((decision!.trace[i]['step'] as Map<String, dynamic>?)?['label'] ?? (decision!.trace[i]['step'] as Map<String, dynamic>?)?['type'] ?? 'Step').toString()}', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                Text('Type: ${((decision!.trace[i]['step'] as Map<String, dynamic>?)?['type'] ?? 'unknown').toString()}'),
                const SizedBox(height: 6),
                Text(decision!.trace[i]['skipped'] == true ? (decision!.trace[i]['reason']?.toString() ?? 'Skipped') : (decision!.trace[i]['result']?.toString() ?? 'Executed')),
              ],
            ),
          ),
          const SizedBox(height: 12),
        ],
        if (decision!.pendingOperations.any((Map<String, dynamic> item) => item['status'] != 'delivered'))
          FilledButton(
            key: const ValueKey<String>('action_deliver_pending_operations'),
            onPressed: () => state.flushPendingOperations(journeyId),
            child: const Text('Deliver Pending Operations'),
          ),
      ],
    );
  }
}

class _SandboxView extends StatelessWidget {
  const _SandboxView({required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    final List<dynamic> policies = state.bundle?.policies ?? [];
    return Column(
      key: const ValueKey<String>('route_sandbox'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _hero(context, 'Dynamic Sandbox', 'Select a policy, add key-value fields, choose types, and run the rule engine offline.'),
        const SizedBox(height: 16),
        _card(
          context,
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('Policy', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              DropdownButtonFormField<String>(
                key: const ValueKey<String>('sandbox_policy_select'),
                value: state.sandboxPolicyId,
                decoration: const InputDecoration(labelText: 'Select a policy'),
                items: policies.map((dynamic p) {
                  final String id = (p is Map) ? (p['id']?.toString() ?? p.toString()) : p.toString();
                  return DropdownMenuItem<String>(value: id, child: Text(id));
                }).toList(),
                onChanged: (String? next) {
                  if (next != null) state.selectSandboxPolicy(next);
                },
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),
        _card(
          context,
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('Input Fields', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              for (int i = 0; i < state.sandboxFields.length; i++) ...<Widget>[
                Row(
                  children: <Widget>[
                    Expanded(
                      child: TextFormField(
                        key: ValueKey<String>('sandbox_key_$i'),
                        initialValue: state.sandboxFields[i]['key'],
                        onChanged: (String v) => state.updateSandboxField(i, key: v),
                        decoration: const InputDecoration(labelText: 'Key'),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: TextFormField(
                        key: ValueKey<String>('sandbox_value_$i'),
                        initialValue: state.sandboxFields[i]['value'],
                        onChanged: (String v) => state.updateSandboxField(i, value: v),
                        decoration: const InputDecoration(labelText: 'Value'),
                      ),
                    ),
                    const SizedBox(width: 8),
                    DropdownButton<String>(
                      key: ValueKey<String>('sandbox_type_$i'),
                      value: state.sandboxFields[i]['type'] ?? 'string',
                      items: const <DropdownMenuItem<String>>[
                        DropdownMenuItem<String>(value: 'string', child: Text('string')),
                        DropdownMenuItem<String>(value: 'number', child: Text('number')),
                        DropdownMenuItem<String>(value: 'boolean', child: Text('boolean')),
                      ],
                      onChanged: (String? v) {
                        if (v != null) state.updateSandboxField(i, type: v);
                      },
                    ),
                    IconButton(
                      key: ValueKey<String>('sandbox_remove_$i'),
                      icon: const Icon(Icons.clear),
                      onPressed: () => state.removeSandboxField(i),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
              ],
              OutlinedButton.icon(
                key: const ValueKey<String>('sandbox_add_field'),
                onPressed: state.addSandboxField,
                icon: const Icon(Icons.add),
                label: const Text('Add Field'),
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),
        Row(
          children: <Widget>[
            Expanded(
              child: FilledButton.icon(
                key: const ValueKey<String>('sandbox_evaluate'),
                onPressed: () => state.evaluateSandbox(),
                icon: const Icon(Icons.play_arrow),
                label: const Text('Run Evaluation'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: OutlinedButton(
                key: const ValueKey<String>('sandbox_clear'),
                onPressed: state.clearSandbox,
                child: const Text('Clear'),
              ),
            ),
          ],
        ),
        if (state.sandboxError != null) ...<Widget>[
          const SizedBox(height: 12),
          Text(state.sandboxError!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
        ],
        if (state.sandboxResult != null) ...<Widget>[
          const SizedBox(height: 16),
          _card(
            context,
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(20),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(24),
                gradient: LinearGradient(
                  colors: <Color>[
                    _outcomeColor(state.sandboxResult!.outcome),
                    _outcomeColor(state.sandboxResult!.outcome).withOpacity(0.72),
                  ],
                ),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text(state.sandboxResult!.outcome.toUpperCase(), style: Theme.of(context).textTheme.headlineSmall?.copyWith(color: Colors.white, fontWeight: FontWeight.w800)),
                  const SizedBox(height: 4),
                  Text('Evaluated in ${state.sandboxResult!.latencyMs}ms', style: const TextStyle(color: Colors.white70)),
                ],
              ),
            ),
          ),
          if (state.sandboxResult!.score != null) ...<Widget>[
            const SizedBox(height: 12),
            _card(context, Text('Score: ${formatNumber(state.sandboxResult!.score)}', style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700))),
          ],
          const SizedBox(height: 12),
          _card(
            context,
            Column(
              key: const ValueKey<String>('sandbox_decision_summary'),
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text('Decision Summary', style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
                const SizedBox(height: 8),
                Text('Outcome: ${state.sandboxResult!.outcome.toUpperCase()} • Status: ${state.sandboxResult!.status}'),
                Text('Source: sandbox'),
                Text('Trace steps: ${state.sandboxResult!.trace.length}'),
                if (state.sandboxResult!.variables.isNotEmpty) ...<Widget>[
                  const SizedBox(height: 8),
                  Text('Variables', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                  for (final MapEntry<String, dynamic> entry in state.sandboxResult!.variables.entries.take(8)) Text('${entry.key}: ${entry.value}'),
                ],
              ],
            ),
          ),
        ],
      ],
    );
  }
}

Widget _adminEditor(BuildContext context, AppState state, Map<String, dynamic> schema) {
  return _card(
    context,
    Column(
      key: const ValueKey<String>('admin_editor'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(
          state.isCreatingRecord ? 'Create ${schema['title'].toString().replaceAll(RegExp(r's$'), '')}' : 'Edit ${schema['title'].toString().replaceAll(RegExp(r's$'), '')}',
          style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700),
        ),
        const SizedBox(height: 12),
        for (final Map<String, dynamic> field in (schema['fields'] as List<dynamic>).cast<Map<String, dynamic>>()) ...<Widget>[
          _journeyField(context, state, '__admin__', field, state.adminEditorRawValues[field['id'] as String]),
          const SizedBox(height: 12),
        ],
        Row(
          children: <Widget>[
            Expanded(
              child: OutlinedButton(
                key: const ValueKey<String>('action_admin_cancel'),
                onPressed: state.cancelAdminEdit,
                child: const Text('Cancel'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: FilledButton(
                key: const ValueKey<String>('action_admin_save'),
                onPressed: state.saveAdminRecord,
                child: const Text('Save'),
              ),
            ),
          ],
        ),
      ],
    ),
  );
}

Widget _journeyField(BuildContext context, AppState state, String journeyId, Map<String, dynamic> field, dynamic value) {
  final String kind = field['kind'] as String? ?? 'text';
  final String? fieldError = journeyId != '__admin__' ? (state.fieldErrors[journeyId] ?? const <String, String?>{})[field['id'] as String] : null;
  if (kind == 'choice' && field['multi'] != true) {
    return DropdownButtonFormField<String>(
      key: ValueKey<String>('field_${field['id']}_${journeyId == '__admin__' ? state.editingRecordId ?? 'create' : state.route}'),
      value: value?.toString().isEmpty ?? true ? null : value.toString(),
      decoration: InputDecoration(
        labelText: field['label'] as String,
        errorText: fieldError,
      ),
      items: ((field['options'] as List<dynamic>? ?? const <dynamic>[]).cast<String>())
          .map((String option) => DropdownMenuItem<String>(value: option, child: Text(option)))
          .toList(),
      onChanged: (String? next) {
        if (journeyId == '__admin__') {
          state.updateAdminField(field, next ?? '');
        } else {
          state.updateDraftField(journeyId, field, next ?? '');
        }
      },
    );
  }
  return TextFormField(
    key: ValueKey<String>('field_${field['id']}_${journeyId == '__admin__' ? state.editingRecordId ?? 'create' : state.route}'),
    initialValue: value is List ? value.join(', ') : value?.toString() ?? '',
    onChanged: (String next) {
      if (journeyId == '__admin__') {
        state.updateAdminField(field, next);
      } else {
        state.updateDraftField(journeyId, field, next);
      }
    },
    keyboardType: kind == 'number' ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.text,
    maxLines: kind == 'json' || kind == 'code' ? 5 : 1,
    decoration: InputDecoration(
      labelText: field['label'] as String,
      errorText: fieldError,
      helperText: fieldError == null
          ? <String>[
              if (field['required'] == true) 'Required',
              if ((field['hint'] as String? ?? '').isNotEmpty) field['hint'] as String,
            ].join(' • ')
          : null,
    ),
  );
}

Widget _journeyCard(BuildContext context, Map<String, dynamic> journey, {required VoidCallback onTap}) {
  return _card(
    context,
    Column(
      key: ValueKey<String>('journey_card_${journey['id']}'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(journey['title'] as String, style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
        const SizedBox(height: 4),
        Text(journey['category'] as String, style: TextStyle(color: Theme.of(context).colorScheme.primary)),
        const SizedBox(height: 8),
        Text('${journey['stepCount']} curated screens powered by the same rule engine, callbacks, and explainability contract.'),
        const SizedBox(height: 12),
        FilledButton(key: ValueKey<String>('action_open_journey_${journey['id']}'), onPressed: onTap, child: const Text('Open Journey')),
      ],
    ),
  );
}

Widget _journeyDashboardCard(BuildContext context, AppState state, Map<String, dynamic> journey) {
  final Decision? decision = state.decisions[journey['id']];
  return _card(
    context,
    Column(
      key: ValueKey<String>('dashboard_card_${journey['id']}'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(journey['title'] as String, style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
        const SizedBox(height: 4),
        Text(journey['category'] as String),
        if (decision != null) ...<Widget>[
          const SizedBox(height: 8),
          Text('Latest: ${decision.outcome.toUpperCase()} • ${decision.status}', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          Text('Score: ${formatNumber(decision.score)}'),
        ],
        const SizedBox(height: 12),
        Row(
          children: <Widget>[
            Expanded(
              child: FilledButton(
                key: ValueKey<String>('action_dashboard_open_${journey['id']}'),
                onPressed: () {
                  state.currentJourneyId = journey['id'] as String;
                  state.goTo(((journey['screens'] as List<dynamic>).first as Map<String, dynamic>)['id'] as String, journeyId: journey['id'] as String);
                },
                child: Text(decision == null ? 'Start' : 'Open'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: OutlinedButton(
                key: ValueKey<String>('action_dashboard_resume_${journey['id']}'),
                onPressed: () {
                  state.currentJourneyId = journey['id'] as String;
                  state.goTo(((journey['screens'] as List<dynamic>).first as Map<String, dynamic>)['id'] as String, journeyId: journey['id'] as String);
                },
                child: const Text('Resume'),
              ),
            ),
          ],
        ),
      ],
    ),
  );
}

Widget _hero(BuildContext context, String title, String subtitle) {
  return _card(
    context,
    Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text('RULEMIND EXPERIENCE STUDIO', style: Theme.of(context).textTheme.labelSmall?.copyWith(color: Theme.of(context).colorScheme.primary, fontWeight: FontWeight.w700)),
        const SizedBox(height: 10),
        Text(title, style: Theme.of(context).textTheme.headlineMedium?.copyWith(fontWeight: FontWeight.w800)),
        const SizedBox(height: 10),
        Text(subtitle, style: Theme.of(context).textTheme.bodyMedium?.copyWith(color: Theme.of(context).colorScheme.onSurfaceVariant)),
      ],
    ),
  );
}

Widget _metricCard(BuildContext context, Map<String, dynamic> metric) {
  return _card(
    context,
    Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(metric['label'] as String, style: Theme.of(context).textTheme.labelMedium),
        const SizedBox(height: 6),
        Text(metric['value'] as String, style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w800)),
        const SizedBox(height: 4),
        Text(metric['trend'] as String, style: TextStyle(color: Theme.of(context).colorScheme.primary)),
      ],
    ),
  );
}

Widget _card(BuildContext context, Widget child) {
  return Card(
    child: Padding(
      padding: const EdgeInsets.all(20),
      child: child,
    ),
  );
}

Color _outcomeColor(String outcome) {
  switch (outcome) {
    case 'approve':
      return const Color(0xFF2D8A5C);
    case 'reject':
      return const Color(0xFFB84A4A);
    default:
      return const Color(0xFFF0A020);
  }
}

String _journeyTitle(AppState state, String journeyId) {
  return state.journeys.cast<Map<String, dynamic>>().firstWhere((Map<String, dynamic> item) => item['id'] == journeyId)['title'] as String;
}

String _journeyOutcomeRoute(Map<String, dynamic> journey) {
  return ((journey['screens'] as List<dynamic>).firstWhere((dynamic item) => (item as Map<String, dynamic>)['type'] == 'outcome') as Map<String, dynamic>)['id'] as String;
}

String _journeyAuditRoute(Map<String, dynamic> journey) {
  return ((journey['screens'] as List<dynamic>).firstWhere((dynamic item) => (item as Map<String, dynamic>)['type'] == 'audit') as Map<String, dynamic>)['id'] as String;
}

void _goBackInJourney(AppState state, Map<String, dynamic> journey, Map<String, dynamic> screen) {
  final List<Map<String, dynamic>> screens = (journey['screens'] as List<dynamic>).cast<Map<String, dynamic>>();
  final int index = screens.indexWhere((Map<String, dynamic> item) => item['id'] == screen['id']);
  if (index > 0) {
    state.goTo(screens[index - 1]['id'] as String, journeyId: journey['id'] as String);
  } else {
    state.goTo('home_dashboard');
  }
}

void _goForwardInJourney(AppState state, Map<String, dynamic> journey, Map<String, dynamic> screen) {
  final List<Map<String, dynamic>> screens = (journey['screens'] as List<dynamic>).cast<Map<String, dynamic>>();
  final int index = screens.indexWhere((Map<String, dynamic> item) => item['id'] == screen['id']);
  if (index >= 0 && index + 1 < screens.length) {
    state.goTo(screens[index + 1]['id'] as String, journeyId: journey['id'] as String);
  }
}

List<List<T>> _chunk<T>(List<T> items, int size) {
  final List<List<T>> chunks = <List<T>>[];
  for (int i = 0; i < items.length; i += size) {
    chunks.add(items.sublist(i, i + size > items.length ? items.length : i + size));
  }
  return chunks;
}

extension<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}
