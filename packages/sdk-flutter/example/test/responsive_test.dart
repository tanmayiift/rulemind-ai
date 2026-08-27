import 'package:flutter/material.dart';
import 'package:rulemind_example/main.dart';
import 'package:rulemind_example/state/app_state.dart';
import 'package:rulemind_ui/testing.dart';

/// Drives the shell straight to a route, bypassing the access screen.
Widget _shellAt(String route, {String? mode}) {
  final AppState state = AppState();
  state.mode = mode ?? 'edge';
  state.route = route;
  return MaterialApp(home: StudioShell(state: state));
}

void main() {
  // The routes with the densest horizontal layouts. 'sandbox' holds the worst
  // row in either app: two Expanded TextFormFields plus an unconstrained
  // DropdownButton plus an IconButton, all in one Row.
  const List<String> routes = <String>[
    'access',
    'experience_selection',
    'home_dashboard',
    'scenario_hub',
    'logs_explainability',
    'admin_console',
    'sandbox',
  ];

  for (final String route in routes) {
    rmResponsiveMatrix(
      'StudioShell/$route',
      build: () => _shellAt(route),
      // The access screen is reached before a mode is chosen.
      surfaces: RmSurfaces.all,
    );
  }
}
