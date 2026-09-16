//! One negotiated session: pty, shell child, privilege drop, and the
//! byte pump. The syscall edges sit behind two traits
//! ([`SessionSys`], [`ChildSys`]) so every failure branch is
//! reachable from tests — the 100% gate depends on that.

use std::ffi::CString;
use std::io;
use std::os::fd::RawFd;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::Command;
use std::time::Instant;

use crate::passwd::{lookup_groups_at, lookup_user_at, UserEntry, UserLookup};
use crate::prelude::read_prelude;
use crate::refuse;
use crate::write_all;

#[derive(Debug)]
pub struct PtyPair {
    pub master: RawFd,
    pub slave: String,
}

/// The fork of the shell child failed; nothing was exec'd.
#[derive(Debug, PartialEq)]
pub struct SpawnFail;

/// The session-level syscalls: pty creation, window size, spawning
/// the shell child. The real implementation is a dozen libc calls;
/// test implementations fail on demand.
pub trait SessionSys {
    /// A fresh, unlocked pty pair; `None` is fail-closed.
    fn open_pty(&self) -> Option<PtyPair>;
    /// TIOCSWINSZ on the pair.
    fn set_winsize(&self, master: RawFd, rows: u16, cols: u16) -> bool;
    /// Fork the shell child (which never returns in the child); `Err`
    /// means the fork itself failed.
    fn spawn_shell(
        &self,
        conn: RawFd,
        pty: &PtyPair,
        user: &UserEntry,
        term: &str,
    ) -> Result<(), SpawnFail>;
    fn close(&self, fd: RawFd);
}

pub struct RealSessionSys;

impl SessionSys for RealSessionSys {
    fn open_pty(&self) -> Option<PtyPair> {
        // SAFETY: the pty trio; any failure collapses to None and the
        // session fails closed.
        unsafe {
            let master = libc::posix_openpt(libc::O_RDWR | libc::O_NOCTTY);
            if master < 0 {
                return None;
            }
            // grantpt/unlockpt/ptsname cannot fail on a fresh Linux
            // pty master; a host where they do is broken, and the
            // panic (not a silent fallback) is the honest failure.
            assert_eq!(libc::grantpt(master), 0);
            assert_eq!(libc::unlockpt(master), 0);
            let ptr = libc::ptsname(master);
            assert!(!ptr.is_null());
            let slave = std::ffi::CStr::from_ptr(ptr).to_string_lossy().into_owned();
            Some(PtyPair { master, slave })
        }
    }

    fn set_winsize(&self, master: RawFd, rows: u16, cols: u16) -> bool {
        let ws = libc::winsize {
            ws_row: rows,
            ws_col: cols,
            ws_xpixel: 0,
            ws_ypixel: 0,
        };
        // SAFETY: TIOCSWINSZ with a winsize struct.
        unsafe { libc::ioctl(master, libc::TIOCSWINSZ, &ws as *const libc::winsize) == 0 }
    }

    fn spawn_shell(
        &self,
        conn: RawFd,
        pty: &PtyPair,
        user: &UserEntry,
        term: &str,
    ) -> Result<(), SpawnFail> {
        // SAFETY: fork(2); the child only runs run_shell_child and
        // exits.
        let pid = unsafe { libc::fork() };
        if pid < 0 {
            return Err(SpawnFail);
        }
        if pid == 0 {
            let code = match run_shell_child(conn, pty, user, &RealChildSys, term) {
                Ok(never) => match never {},
                Err(code) => code,
            };
            std::process::exit(code);
        }
        Ok(())
    }

    fn close(&self, fd: RawFd) {
        // SAFETY: plain close(2).
        unsafe { libc::close(fd) };
    }
}

/// The child-side syscalls: the privilege-drop sequence and exec, as
/// one injection point so each failure is coverable.
pub trait ChildSys {
    /// setsid(2)'s return value (0 on success); the setup path
    /// ignores it — a controlling tty is best-effort until the slave
    /// open — but the test child asserts on it.
    fn setsid_ret(&self) -> i32;
    fn setsid(&self);
    fn open_slave(&self, path: &str) -> RawFd;
    fn dup2(&self, from: RawFd, to: RawFd) -> bool;
    fn close(&self, fd: RawFd);
    fn create_dir(&self, path: &str);
    fn chown(&self, path: &str, uid: u32, gid: u32);
    fn setgroups(&self, gids: &[u32]) -> bool;
    fn setgid(&self, gid: u32) -> bool;
    fn setuid(&self, uid: u32) -> bool;
    fn current_ids(&self) -> (u32, u32);
    fn chdir(&self, path: &str) -> bool;
    /// execve; the returned error is the failure (success diverges).
    fn exec(&self, shell: &str, argv0: &str, env: &[(&str, String)]) -> io::Error;
}

pub struct RealChildSys;

impl ChildSys for RealChildSys {
    fn setsid_ret(&self) -> i32 {
        // SAFETY: plain setsid(2).
        unsafe { libc::setsid() as i32 }
    }

    fn setsid(&self) {
        // SAFETY: plain setsid(2).
        unsafe { libc::setsid() };
    }

    fn open_slave(&self, path: &str) -> RawFd {
        let Ok(path) = CString::new(path) else {
            return -1;
        };
        // SAFETY: open(2) with a valid path.
        unsafe { libc::open(path.as_ptr(), libc::O_RDWR) }
    }

    fn dup2(&self, from: RawFd, to: RawFd) -> bool {
        // SAFETY: plain dup2(2).
        unsafe { libc::dup2(from, to) >= 0 }
    }

    fn close(&self, fd: RawFd) {
        // SAFETY: plain close(2).
        unsafe { libc::close(fd) };
    }

    fn create_dir(&self, path: &str) {
        let _ = std::fs::create_dir(path);
    }

    fn chown(&self, path: &str, uid: u32, gid: u32) {
        if let Ok(path) = CString::new(path) {
            // SAFETY: plain chown(2) with a valid path.
            unsafe { libc::chown(path.as_ptr(), uid, gid) };
        }
    }

    fn setgroups(&self, gids: &[u32]) -> bool {
        // SAFETY: setgroups(2) with the collected gid slice.
        unsafe { libc::setgroups(gids.len(), gids.as_ptr()) == 0 }
    }

    fn setgid(&self, gid: u32) -> bool {
        // SAFETY: plain setgid(2).
        unsafe { libc::setgid(gid) == 0 }
    }

    fn setuid(&self, uid: u32) -> bool {
        // SAFETY: plain setuid(2).
        unsafe { libc::setuid(uid) == 0 }
    }

    fn current_ids(&self) -> (u32, u32) {
        // SAFETY: plain getuid(2)/getgid(2).
        unsafe { (libc::getuid(), libc::getgid()) }
    }

    fn chdir(&self, path: &str) -> bool {
        std::env::set_current_dir(path).is_ok()
    }

    /// The exec'd command: a login shell with a passwd-built
    /// environment. Success is execve, which never returns — the
    /// diverging `.exec()` call cannot be measured by any in-process
    /// coverage runtime, so the line is excluded from the gate; the
    /// builder below is fully covered, and the integration tests
    /// drive the real exec end-to-end.
    #[cfg_attr(coverage, coverage(off))]
    fn exec(&self, shell: &str, argv0: &str, env: &[(&str, String)]) -> io::Error {
        build_shell_command(shell, argv0, env).exec()
    }
}

const SHELL_PATH: &str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

/// The execve'd shell command: login argv[0], cleared environment,
/// passwd-derived variables.
pub fn build_shell_command(shell: &str, argv0: &str, env: &[(&str, String)]) -> Command {
    let mut command = Command::new(shell);
    command.arg0(argv0);
    command.env_clear();
    for (key, value) in env {
        command.env(key, value);
    }
    command
}

/// The shell environment, built from passwd plus the client's TERM —
/// nothing from the listener's root environment.
fn shell_env(user: &UserEntry, term: &str) -> Vec<(&'static str, String)> {
    vec![
        ("TERM", term.to_string()),
        ("HOME", user.home.clone()),
        ("USER", user.name.clone()),
        ("LOGNAME", user.name.clone()),
        ("SHELL", user.shell.clone()),
        ("PATH", SHELL_PATH.to_string()),
    ]
}

/// The shell process: fresh session, the pty slave as controlling tty
/// and stdio, privileges dropped, passwd-built environment. Returns
/// only on failure, with the exit code (125 setup, 126 drop, 127
/// exec); success is execve, which never returns.
pub fn run_shell_child(
    conn: RawFd,
    pty: &PtyPair,
    user: &UserEntry,
    sys: &dyn ChildSys,
    term: &str,
) -> Result<std::convert::Infallible, i32> {
    sys.setsid();
    let slave = sys.open_slave(&pty.slave);
    if slave < 0 {
        return Err(125);
    }
    for fd in 0..3 {
        if !sys.dup2(slave, fd) {
            return Err(125);
        }
    }
    if slave > 2 {
        sys.close(slave);
    }
    sys.close(pty.master);
    sys.close(conn);

    // An unprivileged helper (dev and test runs, where the binary
    // already runs as a regular user) can serve only its own user:
    // serving root would exec root's shell under the helper's uid,
    // and any other identity is someone else's.
    let (helper_uid, helper_gid) = sys.current_ids();
    if helper_uid != 0 {
        let own_identity = user.uid == helper_uid && user.gid == helper_gid;
        if !own_identity {
            return Err(126);
        }
    }

    if user.uid != 0 {
        // The home lands on the persistent /home volume (#14); it is
        // created here, owned by the target user, before the drop.
        sys.create_dir(&user.home);
        sys.chown(&user.home, user.uid, user.gid);
        let dropped = if helper_uid == 0 {
            // The privileged helper (the guest's systemd unit): an
            // exact, verified drop through the full sequence.
            let gids = lookup_groups_at(&user.name, user.gid, Path::new("/etc/group"));
            !sys.setgroups(&gids) || !sys.setgid(user.gid) || !sys.setuid(user.uid) || {
                let (uid, gid) = sys.current_ids();
                uid != user.uid || gid != user.gid
            }
        } else {
            // The unprivileged-helper case was handled above; the
            // identity already matches.
            false
        };
        if dropped {
            return Err(126);
        }
    }
    if !sys.chdir(&user.home) {
        sys.chdir("/");
    }

    // A login shell: the leading dash makes bash read .bash_profile.
    let base = user.shell.rsplit('/').next().unwrap_or("sh");
    let argv0 = format!("-{base}");
    let env = shell_env(user, term);
    sys.exec(&user.shell, &argv0, &env);
    Err(127)
}

/// The pump's syscall edge: poll, nonblocking read/write, errno.
/// The kernel only produces some of these shapes racily (a
/// would-block write right after POLLOUT, an error read right after
/// POLLIN), so the edge sits behind a trait like the session's other
/// syscalls — every branch of the pump is reachable from tests.
pub trait PumpSys {
    fn set_nonblocking(&mut self, fd: RawFd);
    fn poll(&mut self, fds: &mut [libc::pollfd; 2], timeout: libc::c_int) -> libc::c_int;
    fn read(&mut self, fd: RawFd, buf: &mut [u8]) -> isize;
    fn write(&mut self, fd: RawFd, buf: &[u8]) -> isize;
    fn errno(&mut self) -> libc::c_int;
}

pub struct RealPumpSys;

impl PumpSys for RealPumpSys {
    fn set_nonblocking(&mut self, fd: RawFd) {
        // SAFETY: fcntl(2) F_GETFL then F_SETFL|O_NONBLOCK on a
        // stream fd; an fd with no flags (already closed) is left
        // alone — poll below reports it and the session ends.
        unsafe {
            let flags = libc::fcntl(fd, libc::F_GETFL);
            if flags >= 0 {
                libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK);
            }
        }
    }

    fn poll(&mut self, fds: &mut [libc::pollfd; 2], timeout: libc::c_int) -> libc::c_int {
        // SAFETY: two pollfds in, two pollfds out.
        unsafe { libc::poll(fds.as_mut_ptr(), 2, timeout) }
    }

    fn read(&mut self, fd: RawFd, buf: &mut [u8]) -> isize {
        // SAFETY: a nonblocking read(2) into a valid buffer.
        unsafe { libc::read(fd, buf.as_mut_ptr().cast::<libc::c_void>(), buf.len()) }
    }

    fn write(&mut self, fd: RawFd, buf: &[u8]) -> isize {
        // SAFETY: a nonblocking write(2) of a valid buffer.
        unsafe { libc::write(fd, buf.as_ptr().cast::<libc::c_void>(), buf.len()) }
    }

    fn errno(&mut self) -> libc::c_int {
        // SAFETY: plain errno fetch.
        unsafe { *libc::__errno_location() }
    }
}

/// Bytes one direction may hold undelivered before it stops reading
/// its source: a slow consumer backpressures the producer instead of
/// growing the buffer without bound (#103).
pub const PENDING_CAP: usize = 64 * 1024;

/// How long a pump with undelivered bytes may deliver nothing at all
/// before the session is torn down (#103): a stream that accepts no
/// writes for this long is a wedged transport, and a wedged console
/// must fail loudly — the client reconnects — instead of hanging
/// open and silent forever. Deliberately far above the daemon's
/// configurable `console_stall_timeout_s` (default 60 s): the
/// daemon's watchdog names the failure with close code 4502, this
/// teardown closes plainly, so the named close must win the race
/// for any operator window under five minutes.
const STALL_TEARDOWN_MS: libc::c_int = 300_000;

/// Raw bytes both ways until either side reaches EOF (or a poll
/// error: an interrupted poll ends the session — the only signals
/// that can arrive mid-pump are ignored ones, so this is a shutdown).
/// The pump never blocks on one direction: each side keeps its own
/// pending buffer and only polls for what it can move (#103), so a
/// stalled host-side reader (a wedged vsock relay) jams its own
/// output without ever stopping the input path.
pub fn pump(conn: RawFd, master: RawFd) {
    pump_bounded(conn, master, -1, STALL_TEARDOWN_MS);
}

/// [`pump`] with an injectable poll timeout, so the "nothing left to
/// watch" edge is reachable from a test.
pub fn pump_with_timeout(conn: RawFd, master: RawFd, timeout: libc::c_int) {
    pump_bounded(conn, master, timeout, STALL_TEARDOWN_MS);
}

/// [`pump`] with both knobs injectable: the poll timeout (a test
/// edge) and the stall teardown window.
pub fn pump_bounded(conn: RawFd, master: RawFd, timeout: libc::c_int, stall_ms: libc::c_int) {
    let mut sys = RealPumpSys;
    pump_sys(conn, master, timeout, stall_ms, &mut sys);
}

/// [`pump_bounded`] against an injectable syscall edge (the would-
/// block and error shapes the kernel only produces racily).
pub fn pump_sys(
    conn: RawFd,
    master: RawFd,
    timeout: libc::c_int,
    stall_ms: libc::c_int,
    sys: &mut dyn PumpSys,
) {
    sys.set_nonblocking(conn);
    sys.set_nonblocking(master);
    // Pending bytes per direction: to_guest is conn->master input,
    // to_client is master->conn output. A source that reaches EOF
    // stops feeding its buffer but the session keeps flushing the
    // bytes already read — the shell's final output must reach the
    // client before the session ends (the review's tail-drop find).
    let mut to_guest: Vec<u8> = Vec::new();
    let mut to_client: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 4096];
    let mut last_delivered = std::time::Instant::now();
    let mut conn_done = false;
    let mut master_done = false;
    let mut conn_writable = true;
    let mut master_writable = true;
    loop {
        let mut fds = [
            libc::pollfd {
                fd: conn,
                events: poll_events(
                    !conn_done && to_guest.len() < PENDING_CAP,
                    conn_writable && !to_client.is_empty(),
                ),
                revents: 0,
            },
            libc::pollfd {
                fd: master,
                events: poll_events(
                    !master_done && to_client.len() < PENDING_CAP,
                    master_writable && !to_guest.is_empty(),
                ),
                revents: 0,
            },
        ];
        // With bytes pending, poll wakes at least once per stall
        // window even when no fd is ready — otherwise a fully jammed
        // transport would sleep in poll forever and the stall clock
        // below could never fire (#103). With nothing pending, the
        // caller's timeout governs (infinite in production).
        let pending = !to_guest.is_empty() || !to_client.is_empty();
        let wait = if pending {
            if timeout < 0 {
                stall_ms
            } else {
                timeout.min(stall_ms)
            }
        } else {
            timeout
        };
        // SAFETY: (trait) two pollfds in, two pollfds out.
        let ready = sys.poll(&mut fds, wait);
        if ready <= 0 {
            // Zero: the (bounded) wait expired with nothing ready —
            // either the caller's own timeout or a pending stall
            // window that never moved a byte. Negative: EINTR or a
            // poll error. Both end the session.
            return;
        }
        let mut delivered = false;
        // Reads first (each only while its source lives and its
        // direction has room), then writes (each only with something
        // pending and a live destination). Any successful write is
        // delivery: only delivery resets the stall clock. EOF feeds
        // no buffer — the loop's completion rules below flush what
        // is already buffered and then end the session.
        if !conn_done && fds[0].revents & READABLE != 0 && to_guest.len() < PENDING_CAP {
            match classify_read(sys.read(conn, &mut chunk), sys.errno()) {
                ReadOutcome::Data(n) => to_guest.extend_from_slice(&chunk[..n]),
                ReadOutcome::Wait => {}
                ReadOutcome::Eof | ReadOutcome::End => conn_done = true,
            }
        }
        if !master_done && fds[1].revents & READABLE != 0 && to_client.len() < PENDING_CAP {
            match classify_read(sys.read(master, &mut chunk), sys.errno()) {
                ReadOutcome::Data(n) => to_client.extend_from_slice(&chunk[..n]),
                ReadOutcome::Wait => {}
                ReadOutcome::Eof | ReadOutcome::End => master_done = true,
            }
        }
        if write_step(
            &to_guest,
            master_writable,
            fds[1].revents & libc::POLLOUT != 0,
        ) {
            match classify_write(sys.write(master, &to_guest), sys.errno()) {
                WriteOutcome::Wrote(n) => {
                    to_guest.drain(..n);
                    delivered = true;
                }
                WriteOutcome::Wait => {}
                WriteOutcome::End => master_writable = false,
            }
        }
        if write_step(
            &to_client,
            conn_writable,
            fds[0].revents & libc::POLLOUT != 0,
        ) {
            match classify_write(sys.write(conn, &to_client), sys.errno()) {
                WriteOutcome::Wrote(n) => {
                    to_client.drain(..n);
                    delivered = true;
                }
                WriteOutcome::Wait => {}
                WriteOutcome::End => conn_writable = false,
            }
        }
        if delivered {
            last_delivered = std::time::Instant::now();
        } else if (!to_guest.is_empty() || !to_client.is_empty())
            && last_delivered.elapsed().as_millis() as libc::c_int >= stall_ms
        {
            // Undeliverable bytes and a silent transport past the
            // stall window: end the session (the listener accepts a
            // reconnect; the alternative is an open-but-dead
            // stream, #103).
            return;
        }
        // Completion: any source at EOF, with every pending buffer
        // flushed or refused — the shell's last output travels first,
        // and input still queued when the client vanished is
        // delivered while the master accepts writes.
        let guest_settled = to_guest.is_empty() || !master_writable;
        let client_settled = to_client.is_empty() || !conn_writable;
        if (conn_done || master_done) && guest_settled && client_settled {
            return;
        }
    }
}

/// The poll mask for one fd: read while the source lives and the
/// buffer its reads feed is under the cap, write while the
/// destination accepts writes and its buffer is non-empty.
pub fn poll_events(read_wanted: bool, write_wanted: bool) -> libc::c_short {
    let mut events = 0;
    if read_wanted {
        events |= libc::POLLIN;
    }
    if write_wanted {
        events |= libc::POLLOUT;
    }
    events
}

const READABLE: libc::c_short = libc::POLLIN | libc::POLLERR | libc::POLLHUP | libc::POLLNVAL;

/// Whether one direction's write step runs: bytes pending, the
/// destination still accepting writes, and POLLOUT reported.
pub fn write_step(pending: &[u8], writable: bool, pollout: bool) -> bool {
    !pending.is_empty() && writable && pollout
}

pub enum ReadOutcome {
    Data(usize),
    Wait,
    Eof,
    End,
}

/// Classify one read(2) return: positive is data, zero is EOF,
/// -EAGAIN on a nonblocking fd is "nothing right now" (the poll
/// mask includes ERR/HUP, so a readable report can still find the
/// buffer momentarily empty), and any other error ends the session
/// instead of spinning. (EWOULDBLOCK is EAGAIN's spelling on
/// Linux — one comparison covers both.)
pub fn classify_read(n: isize, errno: libc::c_int) -> ReadOutcome {
    if n > 0 {
        ReadOutcome::Data(n as usize)
    } else if n == 0 {
        ReadOutcome::Eof
    } else if errno == libc::EAGAIN {
        ReadOutcome::Wait
    } else {
        ReadOutcome::End
    }
}

pub enum WriteOutcome {
    Wrote(usize),
    Wait,
    End,
}

/// Classify one write(2) return of a nonempty buffer: positive is
/// progress, -EAGAIN is "no room right now" (EWOULDBLOCK is the
/// same value on Linux), and anything else (error, or the
/// never-really-happens zero) ends the session.
pub fn classify_write(n: isize, errno: libc::c_int) -> WriteOutcome {
    if n > 0 {
        WriteOutcome::Wrote(n as usize)
    } else if n < 0 && errno == libc::EAGAIN {
        WriteOutcome::Wait
    } else {
        WriteOutcome::End
    }
}

/// One whole session on an accepted connection: prelude, user
/// lookup, OK reply, pty, shell, pump, close. Every refusal path
/// closes the connection — a shell is never exec'd on failure.
pub fn handle_session(conn: RawFd, sys: &dyn SessionSys, passwd: &Path, deadline: Instant) {
    let pre = match read_prelude(conn, deadline) {
        Some(pre) => pre,
        None => {
            sys.close(conn);
            return;
        }
    };
    let user = match lookup_user_at(&pre.user, passwd) {
        UserLookup::Allowed(user) => user,
        UserLookup::Refused | UserLookup::Absent => {
            refuse(conn, "user");
            sys.close(conn);
            return;
        }
    };
    let reply = format!("MSKS OK {}\n", user.name);
    if !write_all(conn, reply.as_bytes()) {
        sys.close(conn);
        return;
    }
    let pty = match sys.open_pty() {
        Some(pty) => pty,
        None => {
            sys.close(conn);
            return;
        }
    };
    if !sys.set_winsize(pty.master, pre.rows, pre.cols) {
        sys.close(pty.master);
        sys.close(conn);
        return;
    }
    if sys.spawn_shell(conn, &pty, &user, &pre.term).is_err() {
        sys.close(pty.master);
        sys.close(conn);
        return;
    }
    pump(conn, pty.master);
    sys.close(pty.master);
    sys.close(conn);
}
