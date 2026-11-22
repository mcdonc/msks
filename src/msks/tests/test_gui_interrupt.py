"""The Ctrl-C wiring's suite (#1037): the starting terminal's
SIGINT quitting the window's event loop through the quit the
wiring takes, the 130 the terminal reads back, and the unwind
that hands the interpreter's signal state back as the arming
found it."""

import importlib.util
import os
import signal

import pytest


def test_a_ctrl_c_quits_the_event_loop() -> None:
    """The tripped SIGINT ends the loop (#1037): the wakeup byte
    wakes the notifier, the wake slot quits, and the settle
    answers with the terminal's own 130. A real SIGINT —
    ``os.kill`` on this process — drives the whole path, the C
    handler and the wakeup fd included, against the real
    offscreen loop; a guard timer quits the loop instead of
    letting a broken wiring hang the worker, and the assert on
    the settle names the wrong run."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("PySide6 is not installed on this host")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtWidgets

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-gui-test"]
    )
    # The interpreter's own signal state, pinned at a known
    # start: no wakeup fd standing, the default SIGINT handler.
    assert signal.set_wakeup_fd(-1) == -1
    standing = signal.getsignal(signal.SIGINT)
    from msks.client.gui import interrupt

    settle = interrupt.arm_sigint(qapp)

    def trip() -> None:
        """The terminal's Ctrl-C, delivered as the kernel
        delivers it: the signal to this process, nothing
        written to the socket by hand."""
        os.kill(os.getpid(), signal.SIGINT)

    QtCore.QTimer.singleShot(0, trip)
    # The guard: a wiring that never wakes must fail the
    # settle's assert below, not outlast the runner's timeout —
    # and a guard that never fires stops before it can quit a
    # later test's loop on this shared app.
    guard = QtCore.QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(qapp.quit)
    guard.start(30000)
    try:
        assert qapp.exec() == 0  # the quit the wake slot took
        assert settle(0) == interrupt.INTERRUPT_EXIT == 130
        # The unwind (#1037): the wakeup fd and the handler both
        # handed back as the arming found them.
        assert signal.set_wakeup_fd(-1) == -1
        assert signal.getsignal(signal.SIGINT) is standing
    finally:
        # A failed assert must not leak the wiring into the
        # worker's rest of the run: the fd off and the handler
        # back, unconditionally.
        signal.set_wakeup_fd(-1)
        signal.signal(signal.SIGINT, standing)
    guard.stop()


def test_a_plain_end_keeps_the_loop_code() -> None:
    """A loop that ends on its own — the close button, not the
    terminal (#1037): the settle unwinds the same wiring and
    answers with the loop's own code, not the interrupt's."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("PySide6 is not installed on this host")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtWidgets

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-gui-test"]
    )
    assert signal.set_wakeup_fd(-1) == -1
    standing = signal.getsignal(signal.SIGINT)
    from msks.client.gui import interrupt

    settle = interrupt.arm_sigint(qapp)
    try:
        assert settle(3) == 3
        assert signal.set_wakeup_fd(-1) == -1
        assert signal.getsignal(signal.SIGINT) is standing
    finally:
        signal.set_wakeup_fd(-1)
        signal.signal(signal.SIGINT, standing)
