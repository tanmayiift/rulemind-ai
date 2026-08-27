import 'package:flutter/widgets.dart';

/// Material 3 window size classes.
///
/// These are the standard boundaries, not invented ones — see
/// https://m3.material.io/foundations/layout/applying-layout/window-size-classes
enum RmWindowClass {
  /// Phones in portrait, and any resized window under 600dp — including iPad
  /// Split View and Stage Manager, which can hand an app as little as 320dp.
  compact,

  /// Large phones in landscape, small tablets, half-screen iPad.
  medium,

  /// Tablets in landscape and desktop.
  expanded,
}

/// Width breakpoints, in logical pixels.
abstract final class RmBreakpoints {
  static const double compact = 600;
  static const double medium = 840;

  static RmWindowClass classify(double width) {
    if (width < compact) return RmWindowClass.compact;
    if (width < medium) return RmWindowClass.medium;
    return RmWindowClass.expanded;
  }
}

/// Responsive helpers hung off [BuildContext].
///
/// Use these for *page-level* decisions — one column or two. For
/// *component-level* decisions (does this card's row fit the box it was handed)
/// use a [LayoutBuilder] instead: a component that reads the window width will
/// lay itself out wrongly inside a side panel or a narrow parent.
extension RmContext on BuildContext {
  /// The window's size class.
  ///
  /// On iOS Split View and Stage Manager, Flutter's [MediaQuery] reports the
  /// *app window*, not the physical screen, so this is already correct there
  /// with no special case.
  RmWindowClass get rmWindow =>
      RmBreakpoints.classify(MediaQuery.sizeOf(this).width);

  bool get rmIsCompact => rmWindow == RmWindowClass.compact;

  /// Screen-edge gutter appropriate to the window class.
  double get rmGutter => switch (rmWindow) {
        RmWindowClass.compact => 16,
        RmWindowClass.medium => 24,
        RmWindowClass.expanded => 32,
      };

  /// The user's text scale as a plain multiplier.
  ///
  /// [TextScaler] is non-linear on modern platforms, so this probes it at a
  /// representative body size rather than reading a raw factor.
  double get rmTextScale => MediaQuery.textScalerOf(this).scale(14) / 14;

  /// True when text is scaled far enough that horizontal layouts stop fitting.
  ///
  /// Past roughly 1.3x, a row that fits at default size will not, regardless of
  /// how wide the window is — so this is checked *alongside* width, never
  /// instead of it.
  bool get rmIsLargeText => rmTextScale > 1.3;
}
