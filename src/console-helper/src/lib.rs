#![cfg_attr(coverage, feature(coverage_attribute))]

//! msks-console-helper — the guest side of the vsock console (#63).
//!
//! One listener on the workspace's vsock shell port replaces the old
//! `VSOCK-LISTEN:... EXEC:/bin/bash` socat line (#21). Every accepted
//! connection first negotiates a short identity prelude:
//!
//! ```text
//! HELLO 1
//! USER msks
//! WINSZ 34 120
//! GO
//! ```
//!
//! On GO the helper answers `MSKS OK <user>`, allocates the pty with
//! the requested window size, drops privileges to the requested user,
//! and execs that user's login shell. The stream after the reply is a
//! dumb byte pipe: prelude-like text arriving later is inert.
//!
//! Security shape (#63, "Security requirements"):
//!   - Connections are host-only: a peer CID other than the host's is
//!     closed without a reply, so guest-local processes get nothing
//!     from this listener.
//!   - Parsing fails closed: a malformed, oversized, or timed-out
//!     prelude draws one `MSKS ERR <reason>` line and a closed
//!     connection — never a shell.
//!   - The prelude carries names only. uid/gid/shell/home come from
//!     /etc/passwd, parsed here (a static binary must not depend on
//!     dlopen'd NSS machinery). The wire never picks the shell, the
//!     program, or the environment.
//!   - root (uid 0) and regular users (uid >= 1000) are the whole
//!     allowlist; system accounts are refused by name.
//!   - The drop order is setsid + controlling tty, TIOCSWINSZ,
//!     setgroups/setgid/setuid with a post-drop identity check,
//!     chdir($HOME), exec. The environment is built from passwd plus
//!     TERM — nothing from the listener's root environment.
//!
//! The binary is single-threaded by design: the children forked per
//! session only exec, so no locks are ever held across a fork.
//!
//! Coverage gate: this crate is held at 100% line and branch coverage
//! (cargo llvm-cov, see devenv's `rust-tests`). The layout exists for
//! that gate: everything testable lives in this library — parsing is
//! pure, and the syscall edges (pty creation, privilege drop, exec)
//! sit behind the [`session::SessionSys`] / [`session::ChildSys`]
//! traits so every failure branch is reachable from tests. Only
//! `src/main.rs` — vsock socket creation and the fork plumbing — sits
//! outside the gate, exercised end-to-end by the integration tests
//! through its `--test-listen-fd` mode.

pub mod auth;
pub mod cli;
pub mod passwd;
pub mod prelude;
pub mod serve;
pub mod session;

/// The production prelude deadline: ten seconds from accept to GO.
pub const PRELUDE_DEADLINE: std::time::Duration = std::time::Duration::from_secs(10);

/// The production account database.
pub const PASSWD_PATH: &str = "/etc/passwd";

/// The production console-auth trust store (#123); absent on guests
/// seeded before it, which serve no challenge.
pub const CONSOLE_SIGNERS_PATH: &str = "/etc/msks/console.allowed_signers";

/// Write a buffer fully to a stream fd. Partial writes loop; write
/// errors end the session. No EINTR retry is needed: the process
/// installs `SA_RESTART` dispositions for every signal it handles,
/// and blocking stream writes honor it.
pub fn write_all(fd: std::os::fd::RawFd, mut buf: &[u8]) -> bool {
    while !buf.is_empty() {
        // SAFETY: a plain write(2) of a valid buffer.
        let n = unsafe { libc::write(fd, buf.as_ptr().cast::<libc::c_void>(), buf.len()) };
        if n < 0 {
            return false;
        }
        buf = &buf[n as usize..];
    }
    true
}

/// One refusal line; the caller closes right after.
pub fn refuse(fd: std::os::fd::RawFd, reason: &str) {
    let line = format!("MSKS ERR {reason}\n");
    write_all(fd, line.as_bytes());
}

/// plain close(2), for the few spots without a Sys trait at hand.
pub fn close_fd(fd: std::os::fd::RawFd) {
    // SAFETY: plain close(2).
    unsafe { libc::close(fd) };
}
