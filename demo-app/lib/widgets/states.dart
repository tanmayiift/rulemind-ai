import 'package:flutter/material.dart';

import '../theme/tokens.dart';

/// Shown before the first run.
///
/// The app previously had no empty state at all: the screen was a form with a
/// button and nothing indicating a result would ever appear, so a first-time
/// viewer had no idea what they were about to get.
class RmResultPlaceholder extends StatelessWidget {
  const RmResultPlaceholder({super.key});

  @override
  Widget build(BuildContext context) {
    final TextTheme t = Theme.of(context).textTheme;
    return _Well(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const Icon(Icons.timeline, color: RmC.inkFaint, size: 22),
          const SizedBox(height: RmSpace.x2),
          Text('No decision yet', style: t.titleMedium),
          const SizedBox(height: RmSpace.x1),
          Text(
            'Pick a scenario, adjust the inputs, then run.',
            style: t.bodySmall,
          ),
        ],
      ),
    );
  }
}

/// Shown while an evaluation is in flight.
///
/// Deliberately has **no** artificial minimum duration: an on-device decision
/// that returns in two milliseconds *should* look instant — that is the
/// product's point — and the latency readout carries the message.
class RmRunningState extends StatelessWidget {
  const RmRunningState({super.key, required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    final TextTheme t = Theme.of(context).textTheme;
    return _Well(
      showProgress: true,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const SizedBox(height: RmSpace.x1),
          Text(label, style: t.titleMedium),
          const SizedBox(height: RmSpace.x1),
          Text('Evaluating the policy…', style: t.bodySmall),
        ],
      ),
    );
  }
}

/// Shown when an evaluation could not complete.
///
/// The retry control is a [TextButton], not a [FilledButton]: the screen keeps
/// exactly one FilledButton (the run action), which
/// `integration_test/journeys_test.dart` depends on.
class RmErrorBand extends StatelessWidget {
  const RmErrorBand({super.key, required this.message, this.onRetry});

  final String message;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    final TextTheme t = Theme.of(context).textTheme;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(RmSpace.x4),
      decoration: BoxDecoration(
        color: RmSem.reject.soft,
        borderRadius: RmRadius.rLg,
        border: Border.all(color: RmSem.reject.base),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            children: <Widget>[
              Icon(Icons.error_outline, size: 18, color: RmSem.reject.base),
              const SizedBox(width: RmSpace.x2),
              Expanded(
                child: Text(
                  'Evaluation failed',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: t.titleSmall?.copyWith(color: RmSem.reject.ink),
                ),
              ),
            ],
          ),
          const SizedBox(height: RmSpace.x2),
          Text(
            message,
            maxLines: 4,
            overflow: TextOverflow.ellipsis,
            style: t.bodySmall?.copyWith(color: RmSem.reject.ink),
          ),
          if (onRetry != null)
            Align(
              alignment: AlignmentDirectional.centerStart,
              child: TextButton(onPressed: onRetry, child: const Text('Retry')),
            ),
        ],
      ),
    );
  }
}

/// The recessed container the empty and running states share, so the region
/// keeps the same shape as a decision lands in it.
class _Well extends StatelessWidget {
  const _Well({required this.child, this.showProgress = false});

  final Widget child;
  final bool showProgress;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: RmC.surfaceSunk,
        borderRadius: RmRadius.rLg,
        border: Border.all(color: RmC.line),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          if (showProgress)
            const ClipRRect(
              borderRadius: BorderRadius.vertical(top: Radius.circular(RmRadius.lg)),
              child: LinearProgressIndicator(minHeight: 2),
            ),
          Padding(padding: const EdgeInsets.all(RmSpace.x4), child: child),
        ],
      ),
    );
  }
}
