//! Command line: one vsock port, or (for the integration tests that
//! drive the real binary) an inherited listener fd.

use std::path::PathBuf;
use std::time::Duration;

pub const USAGE: &str = "usage: msks-console-helper <vsock-port>";

#[derive(Debug, PartialEq)]
pub enum Mode {
    /// The production mode: listen on the vsock port.
    Vsock { port: u32 },
    /// The integration-test mode: serve an already-listening fd. The
    /// peer policy is allow-all (the fd is a unix socket, whose
    /// sockaddr is not a vsock one), the passwd file and deadline are
    /// injectable so failure paths are reachable end-to-end.
    TestListen {
        fd: i32,
        passwd: PathBuf,
        deadline: Duration,
    },
}

const TEST_DEADLINE_DEFAULT_MS: u64 = 10_000;

/// Parse `[]`-style args. `Ok` selects the mode; `Err` is the usage
/// line (exit code 2 at the caller).
pub fn parse_args(args: &[String]) -> Result<Mode, String> {
    match args {
        [port] => match port.parse::<u32>() {
            Ok(p) if (1..=65535).contains(&p) => Ok(Mode::Vsock { port: p }),
            _ => Err(format!("bad port: {port}\n{USAGE}")),
        },
        [flag, fd] if flag == "--test-listen-fd" => {
            let fd = fd
                .parse::<i32>()
                .map_err(|_| format!("bad fd: {fd}\n{USAGE}"))?;
            Ok(Mode::TestListen {
                fd,
                passwd: PathBuf::from("/etc/passwd"),
                deadline: Duration::from_millis(TEST_DEADLINE_DEFAULT_MS),
            })
        }
        [flag, fd, pflag, passwd] if flag == "--test-listen-fd" && pflag == "--test-passwd" => {
            let fd = fd
                .parse::<i32>()
                .map_err(|_| format!("bad fd: {fd}\n{USAGE}"))?;
            Ok(Mode::TestListen {
                fd,
                passwd: PathBuf::from(passwd),
                deadline: Duration::from_millis(TEST_DEADLINE_DEFAULT_MS),
            })
        }
        [flag, fd, pflag, passwd, dflag, deadline]
            if flag == "--test-listen-fd"
                && pflag == "--test-passwd"
                && dflag == "--test-deadline-ms" =>
        {
            let fd = fd
                .parse::<i32>()
                .map_err(|_| format!("bad fd: {fd}\n{USAGE}"))?;
            let ms = deadline
                .parse::<u64>()
                .map_err(|_| format!("bad deadline: {deadline}\n{USAGE}"))?;
            Ok(Mode::TestListen {
                fd,
                passwd: PathBuf::from(passwd),
                deadline: Duration::from_millis(ms),
            })
        }
        _ => Err(USAGE.to_string()),
    }
}
