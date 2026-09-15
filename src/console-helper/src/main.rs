//! The binary: argument wiring, the vsock socket, and the forked
//! listener. Kept as thin as possible — this file sits outside the
//! coverage gate (see lib.rs) because vsock sockets and fork-failure
//! branches cannot be created in a test environment; everything it
//! calls is gate-covered, and the integration tests drive this exact
//! binary end-to-end through `--test-listen-fd`.

use std::path::Path;
use std::process::exit;
use std::time::{Duration, Instant};

use msks_console_helper::cli::{parse_args, Mode};
use msks_console_helper::serve::{
    fork_session, install_signals, serve, vsock_peer_allowed, AF_VSOCK, VMADDR_CID_ANY,
};
use msks_console_helper::{PASSWD_PATH, PRELUDE_DEADLINE};

fn die(what: &str, error: std::io::Error) -> ! {
    eprintln!("msks-console-helper: {what}: {error}");
    exit(1);
}

/// sockaddr_vm, laid out by hand: family, 2 bytes of alignment
/// padding, reserved, port, cid — 16 bytes total.
#[repr(C)]
struct SockaddrVm {
    svm_family: u16,
    svm_reserved1: u32,
    svm_port: u32,
    svm_cid: u32,
}

fn vsock_listen(port: u32) -> std::io::Result<i32> {
    // SAFETY: socket(2) and the bind/listen pair with the sockaddr
    // above.
    unsafe {
        let fd = libc::socket(AF_VSOCK as i32, libc::SOCK_STREAM, 0);
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let one: libc::c_int = 1;
        libc::setsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_REUSEADDR,
            &one as *const libc::c_int as *const libc::c_void,
            std::mem::size_of::<libc::c_int>() as libc::socklen_t,
        );
        let addr = SockaddrVm {
            svm_family: AF_VSOCK,
            svm_reserved1: 0,
            svm_port: port,
            svm_cid: VMADDR_CID_ANY,
        };
        if libc::bind(
            fd,
            &addr as *const SockaddrVm as *const libc::sockaddr,
            std::mem::size_of::<SockaddrVm>() as libc::socklen_t,
        ) != 0
        {
            let error = std::io::Error::last_os_error();
            libc::close(fd);
            return Err(error);
        }
        if libc::listen(fd, 8) != 0 {
            let error = std::io::Error::last_os_error();
            libc::close(fd);
            return Err(error);
        }
        Ok(fd)
    }
}

fn run(listener: i32, allowed: impl Fn(u16, u32) -> bool, passwd: &Path, deadline: Duration) -> ! {
    // The deadline belongs to each connection: one measured at process
    // start would expire every shell opened more than `deadline`
    // after guest boot.
    match serve(listener, allowed, &|conn| {
        fork_session(conn, passwd, Instant::now() + deadline)
    }) {
        Ok(()) => exit(0),
        Err(error) => die("listener", error),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match parse_args(&args) {
        Err(usage) => {
            eprintln!("{usage}");
            exit(2);
        }
        Ok(Mode::Vsock { port }) => {
            install_signals();
            let fd = vsock_listen(port).unwrap_or_else(|error| die("vsock listen", error));
            run(
                fd,
                vsock_peer_allowed,
                Path::new(PASSWD_PATH),
                PRELUDE_DEADLINE,
            );
        }
        Ok(Mode::TestListen {
            fd,
            passwd,
            deadline,
        }) => {
            install_signals();
            run(fd, |_, _| true, &passwd, deadline);
        }
    }
}
