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
    use std::io::Write;
    if let Ok(mut console) = std::fs::OpenOptions::new().write(true).open("/dev/console") {
        let _ = writeln!(console, "msks-console-helper: {what}: {error}");
    }
    exit(1);
}

fn vsock_listen(port: u32) -> std::io::Result<i32> {
    // libc's sockaddr_vm matches the kernel layout exactly:
    // family@0 (u16), reserved@2 (u16), port@4, cid@8. A hand-rolled
    // struct with a wider reserved field shifts port and cid past
    // their kernel offsets and bind(2) answers EINVAL.
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
        let mut addr: libc::sockaddr_vm = std::mem::zeroed();
        addr.svm_family = AF_VSOCK;
        addr.svm_port = port;
        addr.svm_cid = VMADDR_CID_ANY;
        if libc::bind(
            fd,
            &addr as *const libc::sockaddr_vm as *const libc::sockaddr,
            std::mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t,
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
