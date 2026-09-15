//! The identity prelude: bounded, deadline-driven line reads and a
//! fail-closed parser.

use std::os::fd::RawFd;
use std::time::Instant;

use crate::passwd::name_valid;
use crate::refuse;

const PRELUDE_LINE_MAX: usize = 128;
const DEFAULT_ROWS: u16 = 24;
const DEFAULT_COLS: u16 = 80;

#[derive(Debug, PartialEq)]
pub struct Prelude {
    pub user: String,
    pub rows: u16,
    pub cols: u16,
}

/// One prelude line under the shared deadline, byte at a time. `None`
/// on timeout, EOF, or oversize — all fail closed.
fn read_prelude_line(fd: RawFd, deadline: Instant) -> Option<String> {
    let mut line = Vec::new();
    loop {
        let wait = deadline.checked_duration_since(Instant::now())?;
        let mut pfd = libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        };
        // SAFETY: one pollfd in, one pollfd out.
        let ready =
            unsafe { libc::poll(&mut pfd, 1, wait.as_millis().min(i32::MAX as u128) as i32) };
        if ready <= 0 {
            return None;
        }
        let mut byte = [0u8; 1];
        // SAFETY: a one-byte recv into a valid buffer.
        let n = unsafe { libc::recv(fd, byte.as_mut_ptr().cast::<libc::c_void>(), 1, 0) };
        if n <= 0 {
            return None;
        }
        match byte[0] {
            b'\r' => continue,
            b'\n' => return Some(String::from_utf8_lossy(&line).into_owned()),
            c => {
                if line.len() + 1 >= PRELUDE_LINE_MAX {
                    return None;
                }
                line.push(c);
            }
        }
    }
}

/// Parse and consume the whole prelude. `None` after refusing — the
/// caller closes; no shell is ever exec'd on `None`.
pub fn read_prelude(fd: RawFd, deadline: Instant) -> Option<Prelude> {
    let mut user: Option<String> = None;
    let mut winsz: Option<(u16, u16)> = None;
    let mut first = true;
    loop {
        let Some(line) = read_prelude_line(fd, deadline) else {
            refuse(fd, "timeout");
            return None;
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
                    Some(Prelude { user, rows, cols })
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
