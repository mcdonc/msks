"""The starting terminal's Ctrl-C (#1037): the interrupt the
window must answer. ``QApplication.exec`` is Qt's own C++ wait
— it never returns to Python while the window stands, and
Python's SIGINT answer (a ``KeyboardInterrupt`` raised between
bytecodes) never gets its turn: the flag the C handler trips
waits for bytecode that does not come, so the window stands on
past the Ctrl-C that should close it. The wiring borrows the
interpreter's own wakeup path instead: ``signal.set_wakeup_fd``
turns a tripped SIGINT into one byte on a socket, a
``QSocketNotifier`` on the read end wakes the loop when the
byte lands, and the wake slot quits through the window's own
end — ``aboutToQuit`` fires, the bridge stops, the hub's
terminals close, the geometry saves, the same end the close
button takes.

The module imports PySide6 at module scope, the screens' own
contract (#928): nothing imports it at CLI parse time, and the
window path loads it after its guards.
"""

import signal
from collections.abc import Callable
from socket import socketpair

from PySide6 import QtCore

#: The exit status a Ctrl-C answers with (#1037): 128 plus the
#: signal's own number, the convention the shell reads as
#: "interrupted" — the same 130 the CLI's own interrupt edge
#: exits with.
INTERRUPT_EXIT = 128 + signal.SIGINT


def arm_sigint(qapp: object) -> Callable[[int], int]:
    """Wire the starting terminal's SIGINT to the window's quit
    (#1037), and return the settle callable the close path
    calls with the event loop's own exit code. The settle
    unwinds the wiring — the wakeup fd and the handler handed
    back as the arming found them, the notifier off, the
    sockets closed — and answers ``INTERRUPT_EXIT`` when the
    quit came from a Ctrl-C, or the loop's own code when it
    did not."""
    read, write = socketpair()
    # Both ends non-blocking: the interpreter's C handler
    # writes the wakeup byte from inside the signal, where a
    # blocked write cannot be waited out.
    read.setblocking(False)
    write.setblocking(False)
    ctrl_c: list[bool] = []
    notifier = QtCore.QSocketNotifier(
        read.fileno(), QtCore.QSocketNotifier.Type.Read
    )

    def hold(*args: object) -> None:
        """The Python handler the wiring installs: a no-op whose
        only work happens in the C machinery before it — the
        tripped signal writes the wakeup byte, and the notifier
        below turns the byte into the wake. Raising here would
        wait for bytecode the loop never runs."""

    def wake() -> None:
        """The slot the wakeup byte lands in (#1037): the quit
        takes the window's own end, the ``aboutToQuit`` path
        that stops the bridge and closes every shell. The byte
        itself needs no drain — the socket goes with the
        settle, and no wake comes after the quit it asked
        for."""
        ctrl_c.append(True)
        qapp.quit()

    notifier.activated.connect(wake)
    was_handler = signal.signal(signal.SIGINT, hold)
    # The wakeup fd only writes while a Python handler stands
    # (the C handler it rides is the one ``signal.signal``
    # installs), so the fd lands second.
    was_wakeup = signal.set_wakeup_fd(write.fileno())

    def settle(code: int) -> int:
        """Unwind the wiring (#1037) and name the exit: the
        terminal's own status for a Ctrl-C quit, the loop's own
        code for any other end. The fd comes off before the
        socket closes — a byte written to a closed fd number
        could land in whatever the kernel recycled it as."""
        signal.set_wakeup_fd(was_wakeup)
        signal.signal(signal.SIGINT, was_handler)
        notifier.setEnabled(False)
        read.close()
        write.close()
        return INTERRUPT_EXIT if ctrl_c else code

    return settle
