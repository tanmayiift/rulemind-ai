import 'package:flutter/material.dart';

import 'breakpoints.dart';

/// A [Row] that degrades to a [Column] when there is not enough horizontal room.
///
/// This is the fix for the dominant layout defect in both apps: a `Row` holding
/// a label, a [Spacer], and a variable-width trailing control. That shape has no
/// slack — the trailing control takes its intrinsic width, the label takes its
/// intrinsic width, and when their sum exceeds the parent the row overflows.
///
/// Rather than guess at widths, this switches axis when the window is compact or
/// the user has scaled text up.
class RmLabeledRow extends StatelessWidget {
  const RmLabeledRow({
    super.key,
    required this.label,
    required this.trailing,
    this.spacing = 12,
    this.forceColumn,
  });

  /// Typically a short caption. Ellipsised when horizontal.
  final Widget label;

  /// The control. Takes its natural width in both axes.
  final Widget trailing;

  final double spacing;

  /// Overrides the automatic decision. Only for tests and rare special cases.
  final bool? forceColumn;

  @override
  Widget build(BuildContext context) {
    final bool stack =
        forceColumn ?? (context.rmIsCompact || context.rmIsLargeText);

    if (stack) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          label,
          SizedBox(height: spacing / 2),
          // Align the control to the leading edge so it does not look stranded.
          Align(alignment: AlignmentDirectional.centerStart, child: trailing),
        ],
      );
    }

    return Row(
      children: <Widget>[
        // Expanded, not Spacer: the label is the flexible party, so it absorbs
        // the slack and ellipsises rather than pushing the control off-screen.
        Expanded(child: label),
        SizedBox(width: spacing),
        trailing,
      ],
    );
  }
}

/// A width-driven grid, replacing hand-chunked fixed-column rows.
///
/// Column count comes from the box this is handed — via [LayoutBuilder], not
/// [MediaQuery] — so it is correct inside panels and split views. Falls to a
/// single column whenever text is scaled up, at any width.
class RmAdaptiveGrid extends StatelessWidget {
  const RmAdaptiveGrid({
    super.key,
    required this.children,
    this.spacing = 12,
    this.minTileWidth = 170,
    this.maxColumns = 4,
  });

  final List<Widget> children;
  final double spacing;

  /// A tile narrower than this is unreadable, so the grid drops a column first.
  final double minTileWidth;
  final int maxColumns;

  @override
  Widget build(BuildContext context) {
    if (children.isEmpty) return const SizedBox.shrink();

    return LayoutBuilder(
      builder: (BuildContext context, BoxConstraints constraints) {
        final double width = constraints.maxWidth;
        int columns;
        if (context.rmIsLargeText || !width.isFinite) {
          columns = 1;
        } else {
          columns = ((width + spacing) / (minTileWidth + spacing)).floor();
          columns = columns.clamp(1, maxColumns);
          if (columns > children.length) columns = children.length;
        }

        final List<Widget> rows = <Widget>[];
        for (int i = 0; i < children.length; i += columns) {
          final List<Widget> slice = children.sublist(
            i,
            (i + columns).clamp(0, children.length),
          );
          rows.add(
            // IntrinsicHeight gives the Row a bounded height, without which
            // CrossAxisAlignment.stretch asserts inside a scrolling column.
            // Stretch is what keeps tiles in a row the same height.
            IntrinsicHeight(
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: <Widget>[
                  for (int j = 0; j < columns; j++) ...<Widget>[
                    if (j > 0) SizedBox(width: spacing),
                    // Pad the final row with empty cells so a lone tile keeps
                    // its column width instead of stretching full-bleed.
                    Expanded(
                      child: j < slice.length ? slice[j] : const SizedBox.shrink(),
                    ),
                  ],
                ],
              ),
            ),
          );
          if (i + columns < children.length) {
            rows.add(SizedBox(height: spacing));
          }
        }

        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: rows,
        );
      },
    );
  }
}

/// Screen padding that folds the window gutter with the device's bottom inset.
///
/// Replaces hand-tuned magic constants (e.g. a literal 120 to clear a floating
/// action button), which are wrong on every device but the one they were
/// measured on, and wrong again at large text scale.
abstract final class RmScreenPadding {
  static EdgeInsets of(
    BuildContext context, {
    double top = 16,
    double extraBottom = 0,
  }) {
    final double gutter = context.rmGutter;
    return EdgeInsets.fromLTRB(
      gutter,
      top,
      gutter,
      MediaQuery.viewPaddingOf(context).bottom + extraBottom + gutter,
    );
  }
}
