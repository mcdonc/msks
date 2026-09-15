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
    fn spawn_shell(&self, conn: RawFd, pty: &PtyPair, user: &UserEntry) -> Result<(), SpawnFail>;
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

    fn spawn_shell(&self, conn: RawFd, pty: &PtyPair, user: &UserEntry) -> Result<(), SpawnFail> {
        // SAFETY: fork(2); the child only runs run_shell_child and
        // exits.
        let pid = unsafe { libc::fork() };
        if pid < 0 {
            return Err(SpawnFail);
        }
        if pid == 0 {
            let code = match run_shell_child(conn, pty, user, &RealChildSys) {
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

/// The shell environment, built from passwd plus TERM — nothing from
/// the listener's root environment.
fn shell_env(user: &UserEntry) -> Vec<(&'static str, String)> {
    vec![
        ("TERM", "xterm".to_string()),
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

    if user.uid != 0 {
        // The home lands on the persistent /home volume (#14); it is
        // created here, owned by the target user, before the drop.
        sys.create_dir(&user.home);
        sys.chown(&user.home, user.uid, user.gid);
        let (uid, gid) = sys.current_ids();
        let dropped = if uid == 0 {
            // The privileged helper (the guest's systemd unit): an
            // exact, verified drop through the full sequence.
            let gids = lookup_groups_at(&user.name, user.gid, Path::new("/etc/group"));
            !sys.setgroups(&gids) || !sys.setgid(user.gid) || !sys.setuid(user.uid) || {
                let (uid, gid) = sys.current_ids();
                uid != user.uid || gid != user.gid
            }
        } else {
            // An unprivileged helper (dev and test runs, where the
            // binary already runs as the target user) serves only
            // its own user: there is no privilege to drop, and
            // serving anyone else is refused.
            uid != user.uid || gid != user.gid
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
    let env = shell_env(user);
    sys.exec(&user.shell, &argv0, &env);
    Err(127)
}

/// A plain read(2) of the buffer, as one clean line for the pump's
/// coverage mapping.
///
/// # Safety
///
/// The fd must be a readable stream socket.
unsafe fn read_fd(fd: RawFd, buf: &mut [u8]) -> isize {
    libc::read(fd, buf.as_mut_ptr().cast::<libc::c_void>(), buf.len())
}

/// Raw bytes both ways until either side reaches EOF (or a poll
/// error: an interrupted poll ends the session — the only signals
/// that can arrive mid-pump are ignored ones, so this is a shutdown).
pub fn pump(conn: RawFd, master: RawFd) {
    pump_with_timeout(conn, master, -1);
}

/// [`pump`] with an injectable poll timeout, so the "nothing left to
/// watch" edge is reachable from a test.
pub fn pump_with_timeout(conn: RawFd, master: RawFd, timeout: libc::c_int) {
    let mut buf = [0u8; 4096];
    loop {
        let mut fds = [
            libc::pollfd {
                fd: conn,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: master,
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        // SAFETY: two pollfds in, two pollfds out.
        let ready = unsafe { libc::poll(fds.as_mut_ptr(), 2, timeout) };
        if ready <= 0 {
            return;
        }
        // Both directions run the same step: whichever side is ready
        // moves one buffer's worth across; false ends the session.
        // SAFETY: both fds are readable streams.
        if !unsafe { pump_step(&fds[0], conn, master, &mut buf) }
            || !unsafe { pump_step(&fds[1], master, conn, &mut buf) }
        {
            return;
        }
    }
}

/// One direction's transfer: `from`'s readiness, one read, one write.
/// POLLNVAL is included in the readiness mask: a dead fd ends the
/// session instead of spinning.
///
/// # Safety
///
/// `from` and `to` must be readable/writable stream fds.
unsafe fn pump_step(pfd: &libc::pollfd, from: RawFd, to: RawFd, buf: &mut [u8]) -> bool {
    let readable = libc::POLLIN | libc::POLLERR | libc::POLLHUP | libc::POLLNVAL;
    if pfd.revents & readable == 0 {
        return true;
    }
    let n = read_fd(from, buf);
    if n <= 0 {
        return false;
    }
    write_all(to, &buf[..n as usize])
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
    if sys.spawn_shell(conn, &pty, &user).is_err() {
        sys.close(pty.master);
        sys.close(conn);
        return;
    }
    pump(conn, pty.master);
    sys.close(pty.master);
    sys.close(conn);
}
