"""ghostling-qt: a minimal terminal built on libghostty-vt and
PySide6, the Qt counterpart of Ghostty's Ghostling demo.

Run from this directory:

    python main.py [command...]

With no arguments the interactive $SHELL runs; passing a command runs
that instead, e.g. ``python main.py vim``.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QFont, QFontInfo, QImage, QPainter, QRawFont
from PySide6.QtWidgets import QApplication, QMainWindow
from terminal import TerminalWidget


def main() -> int:
    argv = sys.argv[1:] or None
    app = QApplication(sys.argv)
    window = QMainWindow()
    window.setWindowTitle("ghostling-qt")
    widget = TerminalWidget(argv)
    widget.title_changed.connect(window.setWindowTitle)
    window.setCentralWidget(widget)
    window.resize(widget.sizeHint())
    window.show()
    widget.setFocus()
    screen = window.screen()
    print(
        f"ghostling-qt: platform={app.platformName()} "
        f"font={widget.font.family()} "
        f"pointSize={widget.font.pointSize()} "
        f"pixelSize={widget.font.pixelSize()} "
        f"cell={widget.cell_width}x{widget.cell_height} "
        f"ascent={widget._ascent} "
        f"dpr={screen.devicePixelRatio():.2f} "
        f"logicalDpi={screen.logicalDotsPerInch():.0f}",
        flush=True,
    )

    def self_dump() -> None:
        """Save what the widget actually rendered, after placement."""

        scr = widget.screen()
        pixmap = widget.grab()
        pixmap.save("/tmp/ghostling-self.png")
        raw = QRawFont.fromFont(widget.font)
        print(
            f"self-dump: screen={scr.name()} "
            f"dpr={scr.devicePixelRatio():.2f} "
            f"widget_dpr={widget.devicePixelRatioF():.2f} "
            f"pixmap={pixmap.width()}x{pixmap.height()} "
            f"cell={widget.cell_width}x{widget.cell_height}",
            flush=True,
        )
        print(
            f"self-dump font: requested={widget.font.family()} "
            f"qfontinfo={QFontInfo(widget.font).family()} "
            f"rawfont={raw.familyName()} style={raw.styleName()}",
            flush=True,
        )

        def probe_height(font) -> int:
            probe_img = QImage(90, 72, QImage.Format_RGB32)
            probe_img.setDevicePixelRatio(1.5)
            probe_img.fill(QColor(0, 0, 0))
            pp = QPainter(probe_img)
            pp.setFont(font)
            pp.setPen(QColor(255, 255, 255))
            pp.drawText(QPointF(4, 30), "M")
            pp.end()
            probe_rows = [
                y
                for y in range(probe_img.height())
                if any(
                    probe_img.pixelColor(x, y).lightness() > 100
                    for x in range(probe_img.width())
                )
            ]
            return (probe_rows[-1] - probe_rows[0] + 1) if probe_rows else 0

        fresh_font = QFont(widget.font.family())
        fresh_font.setPointSize(18)
        print(
            f"self-dump probe: fresh="
            f"{probe_height(fresh_font)} "
            f"widget_self={probe_height(widget.font)} "
            f"widget_normal={probe_height(widget._fonts['normal'])} "
            f"(scaled ≈22, unscaled ≈15)",
            flush=True,
        )

    from PySide6.QtCore import QTimer

    QTimer.singleShot(4000, self_dump)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
