//! The console challenge (#123): after the prelude, the helper
//! demands a signature the daemon cannot produce.
//!
//! Every daemon-side check is a check the skeleton-key holder can
//! bypass; this one is not daemon-side. When the seeded
//! `console.allowed_signers` trust store exists, each connection
//! carries a challenge-response inside the console stream:
//!
//! ```text
//! helper  ->  AUTH CHALLENGE <64 hex chars>
//! client  ->  AUTH SIG <base64 SSHSIG file body>
//! helper  ->  AUTH OK          (then the pty, as before)
//! ```
//!
//! The nonce is fresh from `/dev/urandom`, so a captured exchange
//! cannot be replayed. Verification is the guest's own OpenSSH —
//! `ssh-keygen -Y verify` against the trust store, principal bound
//! to the workspace id, namespace `msks-console` — so the algorithm
//! policy belongs to the platform's crypto library (#115), not to
//! this helper. The daemon relays both lines and can answer for
//! neither: it sees a public half in the trust store and a signature
//! on the wire, never the private half a client-held key keeps.
//!
//! A guest without the trust store (an image or workspace seeded
//! before #123) serves no challenge and keeps today's behavior — the
//! extension is opt-in per workspace by what the seed planted.

use std::io::Write;
use std::os::fd::RawFd;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Instant;

use crate::prelude::read_line;
use crate::refuse;
use crate::write_all;

/// The signature namespace: binds every console signature to this
/// use, so a signature over some other payload (an ssh session, a
/// git commit) verifies nowhere here.
pub const NAMESPACE: &str = "msks-console";

/// The trust store the seed script plants (#123); its first line's
/// first field is the verify principal (the workspace id).
pub const SIGNERS_PATH: &str = "/etc/msks/console.allowed_signers";

/// The challenge nonce: 32 bytes, hex on the wire.
pub const NONCE_BYTES: usize = 32;

/// One signature line's ceiling: sized for the largest supplied key
/// material (#132 accepts any type — an RSA body's SSHSIG runs to
/// kilobytes), still bounded against a line-as-DoS.
const SIG_LINE_MAX: usize = 16384;

/// The challenge's own clock, separate from the prelude's: signing
/// can be a human act (a hardware key's touch-to-sign), which a
/// ten-second prelude budget would cut off.
pub const AUTH_DEADLINE: std::time::Duration = std::time::Duration::from_secs(30);

/// Where the verify child's signature file lives: a root-owned
/// 0700 directory under /run (tmpfs, root-only parents), so a
/// guest-local process cannot pre-plant or read the path. Tests
/// point it somewhere they own via the env override.
pub fn sig_dir() -> std::path::PathBuf {
    std::env::var_os("MSKSWS_CONSOLE_SIG_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from("/run/msks-console-helper"))
}

/// The syscalls and externals behind the challenge, as one trait so
/// every failure branch is reachable from the coverage-gated tests.
pub trait AuthSys {
    /// `NONCE_BYTES` fresh bytes; `false` when the source failed.
    fn urandom(&self, out: &mut [u8; NONCE_BYTES]) -> bool;
    /// `ssh-keygen -Y verify` of the armored `sig_blob` over the
    /// raw `nonce` bytes, principal and trust store as given.
    fn verify(&self, sig_blob: &[u8], nonce: &[u8], principal: &str, signers: &Path) -> bool;
}

/// The production edge: `/dev/urandom` and the guest's ssh-keygen.
pub struct RealAuthSys;

impl AuthSys for RealAuthSys {
    fn urandom(&self, out: &mut [u8; NONCE_BYTES]) -> bool {
        use std::io::Read;
        std::fs::File::open("/dev/urandom")
            .map(|mut file| file.read_exact(out).is_ok())
            .unwrap_or(false)
    }

    fn verify(&self, sig_blob: &[u8], nonce: &[u8], principal: &str, signers: &Path) -> bool {
        let dir = sig_dir();
        let made = match std::fs::create_dir(&dir) {
            Ok(()) => true,
            Err(_) => dir.is_dir(),
        };
        if !made {
            return false;
        }
        // Pin the mode whatever umask the unit runs under: 0700,
        // root-only. Best effort — the caller is root in the guest.
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700));
        let path = dir.join(format!("sig.{}", std::process::id()));
        let verdict = std::fs::write(&path, armor(sig_blob)).is_ok()
            && Self::ssh_verify(&path, nonce, principal, signers);
        let _ = std::fs::remove_file(&path);
        verdict
    }
}

impl RealAuthSys {
    /// The guest's own verifier, payload on stdin: exit 0 is the
    /// verdict. The algorithm policy is OpenSSH's (#115), not this
    /// helper's.
    fn ssh_verify(sig: &Path, nonce: &[u8], principal: &str, signers: &Path) -> bool {
        // The session child ignores SIGCHLD (serve.rs), which makes
        // the kernel auto-reap the verify child and our wait() fail
        // with ECHILD before any status exists. Reaping is this
        // spawn's own business: reset the disposition, then spawn.
        // The exec'd shell later sets its own dispositions, so the
        // reset costs nothing there.
        // SAFETY: plain signal(2) disposition.
        unsafe {
            libc::signal(libc::SIGCHLD, libc::SIG_DFL);
        }
        Command::new("ssh-keygen")
            .arg("-Y")
            .arg("verify")
            .arg("-f")
            .arg(signers)
            .arg("-I")
            .arg(principal)
            .arg("-n")
            .arg(NAMESPACE)
            .arg("-s")
            .arg(sig)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|mut child| {
                // Stdio::piped() guarantees the pipe; a failed write
                // hands ssh-keygen EOF and it fails the verify.
                let _ = child.stdin.as_mut().map(|pipe| pipe.write_all(nonce));
                child.wait().map(|status| status.success()).unwrap_or(false)
            })
            .unwrap_or(false)
    }
}

/// The challenge-response exchange on `conn`. `false` refuses (the
/// caller closes); a guest without the trust store passes through
/// without a word (today's behavior, #123's opt-in).
pub fn authenticate(conn: RawFd, deadline: Instant, signers: &Path, sys: &dyn AuthSys) -> bool {
    let principal = match read_signers(signers) {
        // A pre-change guest: no trust store, no challenge.
        Signers::Absent => return true,
        // Half-seeded (the seed created the file but no line landed,
        // or an append failed): ssh is key-gated, so the console
        // fails closed rather than serving the weaker posture.
        Signers::NoPrincipal => {
            refuse(conn, "auth");
            return false;
        }
        Signers::Principal(principal) => principal,
    };
    let mut nonce = [0u8; NONCE_BYTES];
    if !sys.urandom(&mut nonce) {
        refuse(conn, "auth");
        return false;
    }
    let challenge = format!("AUTH CHALLENGE {}\n", hex(&nonce));
    if !write_all(conn, challenge.as_bytes()) {
        return false;
    }
    let line = match read_line(conn, deadline, SIG_LINE_MAX) {
        Ok(line) => line,
        Err(_) => {
            refuse(conn, "auth");
            return false;
        }
    };
    let Some(b64) = line.strip_prefix("AUTH SIG ") else {
        refuse(conn, "auth");
        return false;
    };
    match b64_decode(b64.trim()) {
        Some(blob) if sys.verify(&blob, &nonce, &principal, signers) => {
            write_all(conn, b"AUTH OK\n")
        }
        _ => {
            refuse(conn, "auth");
            false
        }
    }
}

/// What the trust store says: absent (a pre-change guest — no
/// challenge), present without a principal line (a half-seeded
/// state — refuse, fail closed), or the verify principal (the
/// workspace id the seed wrote).
pub enum Signers {
    Absent,
    NoPrincipal,
    Principal(String),
}

/// Read the trust store's verdict. Only a missing file reads as
/// absent: a store that exists but cannot be read (permissions
/// drift, a directory in its place, io errors) refuses — the same
/// fail-closed posture as the half-seeded state.
pub fn read_signers(signers: &Path) -> Signers {
    let text = match std::fs::read_to_string(signers) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Signers::Absent,
        Err(_) => return Signers::NoPrincipal,
    };
    let principal = text
        .lines()
        .next()
        .map(|line| {
            line.trim()
                .split(' ')
                .next()
                .unwrap_or("")
                .trim()
                .to_string()
        })
        .unwrap_or_default();
    if principal.is_empty() {
        Signers::NoPrincipal
    } else {
        Signers::Principal(principal)
    }
}

/// Lowercase hex, the challenge's wire form.
pub fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0xf) as usize] as char);
    }
    out
}

/// Strict standard base64 decode: canonical alphabet, correct
/// padding, no whitespace. `None` on any deviation.
pub fn b64_decode(text: &str) -> Option<Vec<u8>> {
    fn value(byte: u8) -> Option<u32> {
        match byte {
            b'A'..=b'Z' => Some((byte - b'A') as u32),
            b'a'..=b'z' => Some((byte - b'a' + 26) as u32),
            b'0'..=b'9' => Some((byte - b'0' + 52) as u32),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let bytes = text.as_bytes();
    if bytes.is_empty() || !bytes.len().is_multiple_of(4) {
        return None;
    }
    // Padding lives only at the end, at most two columns, and the
    // pushed bytes above already skip the padded positions.
    let body_end = bytes.len() - bytes.iter().rev().take_while(|b| **b == b'=').count();
    if bytes.len() - body_end > 2 || bytes[..body_end].contains(&b'=') {
        return None;
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for chunk in bytes.chunks(4) {
        let mut acc = 0u32;
        for byte in chunk.iter() {
            // Padding placement was validated above (only the final
            // one or two columns); padded positions contribute zero.
            let v = if *byte == b'=' { 0 } else { value(*byte)? };
            acc = (acc << 6) | v;
        }
        out.push((acc >> 16) as u8);
        if chunk[2] != b'=' {
            out.push((acc >> 8) as u8);
        }
        if chunk[3] != b'=' {
            out.push(acc as u8);
        }
    }
    Some(out)
}

/// Standard base64 encode wrapped at `width` — the SSHSIG armor's
/// body form (OpenSSH wraps at 70).
pub fn b64_encode(bytes: &[u8], width: usize) -> String {
    const DIGITS: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            chunk.get(1).copied().unwrap_or(0),
            chunk.get(2).copied().unwrap_or(0),
        ];
        let acc = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(DIGITS[(acc >> 18) as usize & 63] as char);
        out.push(DIGITS[(acc >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            DIGITS[(acc >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            DIGITS[acc as usize & 63] as char
        } else {
            '='
        });
    }
    if width == 0 {
        return out;
    }
    let wrapped: Vec<String> = out
        .as_bytes()
        .chunks(width)
        .map(|c| String::from_utf8_lossy(c).into_owned())
        .collect();
    wrapped.join("\n")
}

/// The SSHSIG armored file a verifier reads (PROTOCOL.sshsig):
/// fixed header, the base64 body wrapped at 70, fixed footer.
fn armor(blob: &[u8]) -> String {
    format!(
        "-----BEGIN SSH SIGNATURE-----\n{}\n-----END SSH SIGNATURE-----\n",
        b64_encode(blob, 70)
    )
}
