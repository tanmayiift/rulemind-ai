import 'package:flutter/material.dart';

import '../theme/tokens.dart';

/// The uppercase section rule used for SCENARIOS / INPUTS / WHY.
///
/// The idiom (`labelSmall` + `letterSpacing: 0.8` + `hintColor`) was repeated
/// verbatim at three call sites and never extracted, which is part of why
/// `labelSmall` ended up doing four unrelated jobs.
///
/// The hairline rule to the right of the label is what turns three floating
/// captions into visible page structure.
///
/// [label] must already be uppercase and is rendered as a plain [Text] with
/// exactly that string. Do **not** uppercase inside this widget and do **not**
/// use `Text.rich`: `integration_test/journeys_test.dart` asserts
/// `find.text('WHY')` matches exactly one widget.
class RmSectionHeader extends StatelessWidget {
  const RmSectionHeader(this.label, {super.key, this.trailing});

  final String label;

  /// Optional control aligned to the far end of the rule.
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final TextStyle? style = Theme.of(context).textTheme.labelMedium?.copyWith(
          letterSpacing: 1.0,
          fontWeight: FontWeight.w700,
          color: RmC.inkFaint,
        );

    return Row(
      children: <Widget>[
        // Flexible, not fixed: a long section label at 2.0x text must be able
        // to give way rather than push the rule off-screen.
        Flexible(
          child: Text(label, maxLines: 1, overflow: TextOverflow.ellipsis, style: style),
        ),
        const SizedBox(width: RmSpace.x3),
        const Expanded(child: Divider(color: RmC.line, height: 1, thickness: 1)),
        if (trailing != null) ...<Widget>[
          const SizedBox(width: RmSpace.x3),
          trailing!,
        ],
      ],
    );
  }
}
