//! The identity prelude: bounded, deadline-driven line reads and a
//! fail-closed parser.

use std::os::fd::RawFd;
use std::time::Instant;

use crate::passwd::name_valid;
use crate::refuse;

const PRELUDE_LINE_MAX: usize = 128;
const DEFAULT_ROWS: u16 = 24;
const DEFAULT_COLS: u16 = 80;
const DEFAULT_TERM: &str = "xterm";

/// Why a prelude read stopped early: every shape fails closed, but
/// the refusal names what actually happened.
#[derive(Debug, PartialEq, Clone, Copy)]
enum ReadFail {
    Deadline,
    Closed,
    Oversize,
}

#[derive(Debug, PartialEq)]
pub struct Prelude {
    pub user: String,
    pub rows: u16,
    pub cols: u16,
    /// The client's terminal type (a "TERM" line); a sane default
    /// when the client sent none.
    pub term: String,
}

/// One prelude line under the shared deadline, byte at a time; `Err`
/// names the failure shape (all fail closed), `Ok` is the line.
fn read_prelude_line(fd: RawFd, deadline: Instant) -> Result<String, ReadFail> {
    let mut line = Vec::new();
    loop {
        let Some(wait) = deadline.checked_duration_since(Instant::now()) else {
            return Err(ReadFail::Deadline);
        };
        let mut pfd = libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        };
        // SAFETY: one pollfd in, one pollfd out.
        let ready =
            unsafe { libc::poll(&mut pfd, 1, wait.as_millis().min(i32::MAX as u128) as i32) };
        if ready <= 0 {
            return Err(ReadFail::Deadline);
        }
        let mut byte = [0u8; 1];
        // SAFETY: a one-byte recv into a valid buffer.
        let n = unsafe { libc::recv(fd, byte.as_mut_ptr().cast::<libc::c_void>(), 1, 0) };
        if n <= 0 {
            return Err(ReadFail::Closed);
        }
        match byte[0] {
            b'\r' => continue,
            b'\n' => return Ok(String::from_utf8_lossy(&line).into_owned()),
            c => {
                if line.len() + 1 >= PRELUDE_LINE_MAX {
                    return Err(ReadFail::Oversize);
                }
                line.push(c);
            }
        }
    }
}

/// The wire charset for a TERM value: printable ASCII minus space —
/// every name terminfo uses (xterm-256color, tmux-256color, …) fits.
fn term_valid(value: &str) -> bool {
    match value.len() {
        1..=32 => value.bytes().all(|b| b.is_ascii_graphic()),
        _ => false,
    }
}

/// Parse and consume the whole prelude. `None` after refusing — the
/// caller closes; no shell is ever exec'd on `None`.
pub fn read_prelude(fd: RawFd, deadline: Instant) -> Option<Prelude> {
    let mut user: Option<String> = None;
    let mut winsz: Option<(u16, u16)> = None;
    let mut term: Option<String> = None;
    let mut first = true;
    loop {
        let line = match read_prelude_line(fd, deadline) {
            Ok(line) => line,
            Err(ReadFail::Deadline) => {
                refuse(fd, "timeout");
                return None;
            }
            Err(ReadFail::Closed) => {
                refuse(fd, "closed");
                return None;
            }
            Err(ReadFail::Oversize) => {
                refuse(fd, "syntax");
                return None;
            }
        };
        if first {
            first = false;
            if line == "HELLO 1" {
                continue;
            }
            refuse(fd, "version");
            return None;
        }
        if line == "GO" {
            return match user {
                Some(user) => {
                    let (rows, cols) = winsz.unwrap_or((DEFAULT_ROWS, DEFAULT_COLS));
                    let term = term.unwrap_or_else(|| DEFAULT_TERM.to_string());
                    Some(Prelude {
                        user,
                        rows,
                        cols,
                        term,
                    })
                }
                None => {
                    refuse(fd, "user");
                    None
                }
            };
        }
        if let Some(name) = line.strip_prefix("USER ") {
            match (&user, name_valid(name)) {
                (None, true) => user = Some(name.to_string()),
                (Some(_), _) => {
                    refuse(fd, "syntax");
                    return None;
                }
                (None, false) => {
                    refuse(fd, "user");
                    return None;
                }
            }
            continue;
        }
        if let Some(value) = line.strip_prefix("TERM ") {
            if term.is_some() || !term_valid(value) {
                refuse(fd, "syntax");
                return None;
            }
            term = Some(value.to_string());
            continue;
        }
        if let Some(rest) = line.strip_prefix("WINSZ ") {
            if winsz.is_some() {
                refuse(fd, "syntax");
                return None;
            }
            let mut parts = rest.split(' ');
            let parsed = match (parts.next(), parts.next(), parts.next()) {
                (Some(r), Some(c), None) => match (r.parse::<u32>(), c.parse::<u32>()) {
                    (Ok(r), Ok(c))
                        if (1..=u16::MAX as u32).contains(&r)
                            && (1..=u16::MAX as u32).contains(&c) =>
                    {
                        Some((r as u16, c as u16))
                    }
                    _ => None,
                },
                _ => None,
            };
            match parsed {
                Some(size) => winsz = Some(size),
                None => {
                    refuse(fd, "winsz");
                    return None;
                }
            }
            continue;
        }
        refuse(fd, "syntax");
        return None;
    }
}
