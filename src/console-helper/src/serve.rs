//! The listener loop: accept, filter peers, hand connections to the
//! session machinery.

use std::io;
use std::os::fd::RawFd;
use std::time::Instant;

use crate::close_fd;
use crate::session::handle_session;
use crate::session::{RealSessionSys, SpawnFail};

/// `AF_VSOCK` on Linux. Defined here, not pulled from libc, so the
/// value is pinned by this file regardless of libc-crate vintage.
pub const AF_VSOCK: u16 = 40;

/// The bind-any CID (`VMADDR_CID_ANY`): listen on every guest CID.
pub const VMADDR_CID_ANY: u32 = 0xFFFF_FFFF;

/// The CID the host side of the vsock device presents to the guest
/// (what `VMADDR_CID_HOST`/`VMADDR_CID_HYPERVISOR` both name).
pub const VMADDR_CID_HOST: u32 = 2;

/// The production peer filter (#63): the vsock link is inside the
/// trust boundary exactly when the peer is the host side of the
/// device. Guest-local peers are closed without a reply.
pub fn vsock_peer_allowed(family: u16, cid: u32) -> bool {
    family == AF_VSOCK && cid == VMADDR_CID_HOST
}

/// accept(2) plus the peer's (family, cid), extracted from the
/// sockaddr bytes the kernel wrote. A short sockaddr (a unix one, in
/// the test mode) reads as family-with-cid-zero — never a vsock host
/// peer, which is why the test mode passes its own allow-all filter.
fn accept_peer(listener: RawFd) -> io::Result<(RawFd, u16, u32)> {
    // libc's sockaddr_vm is the kernel layout: family@0 (u16),
    // reserved@2 (u16), port@4, cid@8. Reading the cid from any other
    // offset inspects padding — always zero — and refuses every peer
    // including the host.
    let mut addr: libc::sockaddr_vm = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t;
    // SAFETY: accept(2) into the sockaddr above.
    let fd = unsafe {
        libc::accept(
            listener,
            &mut addr as *mut libc::sockaddr_vm as *mut libc::sockaddr,
            &mut len,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // A short sockaddr (a unix listener in the test mode) keeps the
    // zeroed cid — never a vsock host peer.
    Ok((fd, addr.svm_family, addr.svm_cid))
}

/// The listener loop: accept forever, filtering peers through
/// `allowed` and dispatching allowed connections to `spawn` (which
/// owns the connection fd). Returns only on an accept error.
pub fn serve(
    listener: RawFd,
    allowed: impl Fn(u16, u32) -> bool,
    spawn: &dyn Fn(RawFd) -> Result<(), SpawnFail>,
) -> io::Result<()> {
    loop {
        let (conn, family, cid) = accept_peer(listener)?;
        if !allowed(family, cid) {
            close_fd(conn);
            continue;
        }
        if spawn(conn).is_err() {
            close_fd(conn);
        }
    }
}

/// One forked session child (the production `spawn`): fork, child
/// runs the session and exits, parent releases the connection. Used
/// by main and by the integration tests through the real binary; its
/// failure arm is the fork itself failing.
pub fn fork_session(
    conn: RawFd,
    passwd: &std::path::Path,
    deadline: Instant,
) -> Result<(), SpawnFail> {
    // SAFETY: fork(2); the child only runs handle_session and exits.
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        return Err(SpawnFail);
    }
    if pid == 0 {
        handle_session(conn, &RealSessionSys, passwd, deadline);
        std::process::exit(0);
    }
    close_fd(conn);
    Ok(())
}

/// The signal dispositions: children are session handlers and shells
/// that exit on their own, so reaping is automatic; a dead peer must
/// end a pump with EPIPE, not the process. `SA_RESTART` (the default
/// for `signal(2)`) keeps blocking stream I/O restartable.
pub fn install_signals() {
    // SAFETY: plain signal(2) dispositions.
    unsafe {
        libc::signal(libc::SIGCHLD, libc::SIG_IGN);
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
    }
}
