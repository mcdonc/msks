# Shells consent panel reference images

These images show the native Qt workspace composition with the consent panel.
The capture uses `QT_QPA_PLATFORM=offscreen` and `QWidget.grab()`.
The maintainer approved this native equivalent of browser capture.

The bridge is a test stub. A real `ConsentPresenter` receives fixed request
frames with a fixed clock. The terminal runs a local Python child that prints
a prompt and waits. It does not run curl or connect to SSH. The capture does
not use a daemon, network connection, or desktop window.

The capture checks destination and countdown labels before the pending image.
It checks the verdict error text before the failure image. It checks that the
panel is hidden before the empty image.

| State                                | Viewport   | Reference                             |
| ------------------------------------ | ---------- | ------------------------------------- |
| Two pending holds                    | 1280 × 720 | [Pending](shells-consent-pending.png) |
| Failed verdict, both holds retained  | 1280 × 720 | [Failure](shells-consent-failure.png) |
| Actions wrap below each destination  | 360 × 840  | [Narrow](shells-consent-narrow.png)   |
| Empty queue, terminal space restored | 1280 × 720 | [Empty](shells-consent-empty.png)     |

The native Qt palette and fonts take priority over the HTML planning sketch.
Menus and keyboard behavior use native QToolButton and QMenu controls.
Offscreen tests cover activation, cancellation, focus, and stale replies.
These images do not prove live-daemon or macOS desktop behavior.
