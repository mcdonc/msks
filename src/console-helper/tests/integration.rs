//! End-to-end tests against the real binary, driven through its
//! `--test-listen-fd` mode: an inherited unix listener in place of
//! the vsock socket. The binary is instrumented like any test target
//! (LLVM_PROFILE_FILE propagates, with %p forced so the child
//! processes' counters merge into the gate instead of clobbering the
//! parent's).

use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

const HELPER: &str = env!("CARGO_BIN_EXE_msks-console-helper");

static FIXTURE_ID: AtomicU64 = AtomicU64::new(0);

/// The auth tests serialize: each spawns the helper, ssh-keygen, and
/// the verify child, and the CI runner's 2 vCPUs also run ten other
/// integration tests — the pair is small enough to take turns.
static AUTH_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

struct Fixture {
    child: Child,
    /// Held (never read) so the listening fd outlives the helper
    /// processes that inherited it.
    #[allow(dead_code)]
    listener: UnixListener,
    path: PathBuf,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_file(&self.path);
    }
}

/// Spawn the helper serving an inherited listener fd.
fn spawn_helper(
    passwd: Option<&std::path::Path>,
    deadline_ms: u64,
    signers: Option<&std::path::Path>,
) -> Fixture {
    let dir = std::env::temp_dir().join("msks-helper-integration");
    std::fs::create_dir_all(&dir).unwrap();
    let id = FIXTURE_ID.fetch_add(1, Ordering::SeqCst);
    let path = dir.join(format!("sock-{}-{id}", std::process::id()));
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path).unwrap();
    // std creates unix sockets close-on-exec; the helper needs the fd
    // to survive the spawn.
    unsafe {
        libc::fcntl(listener.as_raw_fd(), libc::F_SETFD, 0);
    }

    let mut command = Command::new(HELPER);
    command
        .arg("--test-listen-fd")
        .arg(listener.as_raw_fd().to_string());
    if let Some(passwd) = passwd {
        command
            .arg("--test-passwd")
            .arg(passwd)
            .arg("--test-deadline-ms")
            .arg(deadline_ms.to_string());
        if let Some(signers) = signers {
            command.arg("--test-signers").arg(signers);
            // The verify child writes its signature file into the
            // sig dir the env names; the production default under
            // /run is root-only, so a privileged-session test
            // points it at a directory this test owns.
            let sig_dir = std::env::temp_dir()
                .join(format!("msks-helper-sigdir-i-{}-{id}", std::process::id()));
            let _ = std::fs::remove_dir_all(&sig_dir);
            std::fs::create_dir_all(&sig_dir).unwrap();
            command.env("MSKS_CONSOLE_SIG_DIR", &sig_dir);
        }
    }
    profile_env(&mut command);
    let child = command.spawn().expect("helper spawn");
    Fixture {
        child,
        listener,
        path,
    }
}

/// Keep the coverage counters of every helper process mergeable: a
/// plain inherited path (the gate gives each test binary one) would
/// have every child overwrite it — rewrite to a per-process pattern,
/// which the helper binary records fine.
fn profile_env(command: &mut Command) {
    match std::env::var("LLVM_PROFILE_FILE") {
        Ok(pattern) if pattern.contains("%p") => {}
        Ok(pattern) if pattern.contains("%m") => {
            let fixed = pattern.replace(".profraw", "_%p.profraw");
            command.env("LLVM_PROFILE_FILE", fixed);
        }
        Ok(pattern) => {
            let dir = std::path::Path::new(&pattern)
                .parent()
                .map(|p| p.to_path_buf())
                .unwrap_or_else(std::env::temp_dir);
            command.env(
                "LLVM_PROFILE_FILE",
                format!("{}/child-%m-%p.profraw", dir.display()),
            );
        }
        Err(_) => {
            command.env(
                "LLVM_PROFILE_FILE",
                format!(
                    "{}/msks-helper-%p-%m.profraw",
                    std::env::temp_dir().display()
                ),
            );
        }
    }
}

fn connect(fixture: &Fixture) -> UnixStream {
    for _ in 0..200 {
        match UnixStream::connect(&fixture.path) {
            Ok(stream) => return stream,
            Err(_) => std::thread::sleep(Duration::from_millis(10)),
        }
    }
    panic!("helper never accepted");
}

/// The current user as an allowed passwd entry with a real shell, or
/// root when the tests run privileged.
fn test_passwd() -> (PathBuf, String) {
    let uid = unsafe { libc::getuid() };
    let gid = unsafe { libc::getgid() };
    let user = std::env::var("USER").unwrap_or_else(|_| "root".into());
    let dir = std::env::temp_dir().join("msks-helper-integration");
    std::fs::create_dir_all(&dir).unwrap();
    let home = dir.join(format!("home-{uid}"));
    std::fs::create_dir_all(&home).unwrap();
    // One passwd per test thread: the shared path races a concurrent
    // test's truncate-and-write rewrite (the helper reads the file at
    // lookup time, and a mid-rewrite read looks up an empty file).
    let thread = format!("{:?}", std::thread::current().id());
    let passwd = dir.join(format!("passwd-{thread}"));
    std::fs::write(
        &passwd,
        format!("{user}:x:{uid}:{gid}::{}:/bin/sh\n", home.display()),
    )
    .unwrap();
    (passwd, user)
}

fn read_line(stream: &mut UnixStream) -> String {
    read_line_within(stream, Duration::from_secs(10), "reply line")
}

/// Byte-wise: no per-call BufReader. The helper's protocol lines
/// are small, and a buffered reader built per call silently drops
/// whatever followed the returned line in its buffer when two guest
/// lines coalesce into one read — exactly what a slow CI runner
/// schedules between MSKS OK and the challenge.
fn read_line_within(stream: &mut UnixStream, timeout: Duration, awaited: &str) -> String {
    stream.set_read_timeout(Some(timeout)).unwrap();
    let mut line = Vec::new();
    let mut byte = [0u8; 1];
    loop {
        match stream.read(&mut byte) {
            Ok(0) => break,
            Ok(_) => {
                line.push(byte[0]);
                if byte[0] == b'\n' {
                    break;
                }
            }
            Err(error) => panic!("{awaited}: {error}"),
        }
    }
    String::from_utf8_lossy(&line).into_owned()
}

fn read_until_eof(stream: &mut UnixStream) -> String {
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut buf = Vec::new();
    let _ = stream.read_to_end(&mut buf);
    String::from_utf8_lossy(&buf).into_owned()
}

fn send_prelude(stream: &mut UnixStream, user: &str, rows: u16, cols: u16) {
    stream
        .write_all(format!("HELLO 1\nUSER {user}\nWINSZ {rows} {cols}\nGO\n").as_bytes())
        .unwrap();
}

#[test]
fn shell_session_round_trip() {
    let (passwd, user) = test_passwd();
    let fixture = spawn_helper(Some(&passwd), 10_000, None);
    let mut client = connect(&fixture);
    send_prelude(&mut client, &user, 34, 120);
    assert_eq!(read_line(&mut client), format!("MSKS OK {user}\n"));

    // The pty echoes input and the shell answers: both pump
    // directions carry bytes.
    let marker = format!("m{}", std::process::id());
    client
        .write_all(format!("echo {marker}\n").as_bytes())
        .unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut seen = String::new();
    let mut byte = [0u8; 1];
    while !seen.contains(&marker) {
        match client.read(&mut byte) {
            Ok(0) => panic!("session ended before the marker: {seen}"),
            Ok(_) => seen.push(byte[0] as char),
            Err(error) => panic!("read failed: {error}"),
        }
    }
    drop(client);
}

#[test]
fn window_size_is_applied_to_the_pty() {
    let (passwd, user) = test_passwd();
    let fixture = spawn_helper(Some(&passwd), 10_000, None);
    let mut client = connect(&fixture);
    // A size no terminal would default to.
    send_prelude(&mut client, &user, 33, 111);
    assert_eq!(read_line(&mut client), format!("MSKS OK {user}\n"));
    client
        .write_all("stty size\n".to_string().as_bytes())
        .unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut seen = String::new();
    let mut byte = [0u8; 1];
    while !seen.contains("33 111") {
        match client.read(&mut byte) {
            Ok(0) => panic!("session ended before the size: {seen}"),
            Ok(_) => seen.push(byte[0] as char),
            Err(error) => panic!("read failed: {error}"),
        }
        if seen.lines().count() > 8 {
            panic!("stty size never reported 33 111: {seen}");
        }
    }
    drop(client);
}

#[test]
fn env_is_built_from_passwd_not_the_listener() {
    let (passwd, user) = test_passwd();
    let fixture = spawn_helper(Some(&passwd), 10_000, None);
    let mut client = connect(&fixture);
    send_prelude(&mut client, &user, 24, 80);
    assert_eq!(read_line(&mut client), format!("MSKS OK {user}\n"));
    // A poisoned variable the listener never passes through, and the
    // passwd-derived one that must exist. The pty echoes the command
    // line (with the literal $ names) first; the answer carries the
    // expansions: [xterm] [<user>] []. Interactive shells may wrap
    // the line in bracketed-paste and title escapes, so the check is
    // containment, not equality.
    let expected = format!("[xterm] [{user}] []");
    client
        .write_all(b"echo [$TERM] [$USER] [$MSKS_POISON]\n")
        .unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut seen = String::new();
    let mut byte = [0u8; 1];
    while !seen.contains(&expected) {
        match client.read(&mut byte) {
            Ok(0) => panic!("session ended before the env answer: {seen}"),
            Ok(_) => seen.push(byte[0] as char),
            Err(error) => panic!("read failed: {error}"),
        }
    }
    drop(client);
}

#[test]
fn refused_user_gets_a_named_refusal() {
    let (passwd, _) = test_passwd();
    let fixture = spawn_helper(Some(&passwd), 10_000, None);
    let mut client = connect(&fixture);
    send_prelude(&mut client, "nosuchuser", 24, 80);
    assert_eq!(read_line(&mut client), "MSKS ERR user\n");
    assert_eq!(read_until_eof(&mut client), "");
}

#[test]
fn system_account_is_refused() {
    let dir = std::env::temp_dir().join("msks-helper-integration");
    std::fs::create_dir_all(&dir).unwrap();
    let passwd = dir.join("passwd-system");
    std::fs::write(&passwd, "daemon:x:1:1::/nonexistent:/usr/sbin/nologin\n").unwrap();
    let fixture = spawn_helper(Some(&passwd), 10_000, None);
    let mut client = connect(&fixture);
    send_prelude(&mut client, "daemon", 24, 80);
    assert_eq!(read_line(&mut client), "MSKS ERR user\n");
}

#[test]
fn bad_version_is_refused() {
    let (passwd, _) = test_passwd();
    let fixture = spawn_helper(Some(&passwd), 10_000, None);
    let mut client = connect(&fixture);
    client.write_all(b"HELLO 2\nUSER root\nGO\n").unwrap();
    assert_eq!(read_line(&mut client), "MSKS ERR version\n");
}

#[test]
fn syntax_is_refused() {
    let (passwd, _) = test_passwd();
    let fixture = spawn_helper(Some(&passwd), 10_000, None);
    let mut client = connect(&fixture);
    client.write_all(b"HELLO 1\nEXEC /bin/sh\nGO\n").unwrap();
    assert_eq!(read_line(&mut client), "MSKS ERR syntax\n");
}

#[test]
fn silence_times_out_and_fails_closed() {
    let (passwd, _) = test_passwd();
    // A deadline the test can wait out.
    let fixture = spawn_helper(Some(&passwd), 300, None);
    let mut client = connect(&fixture);
    client.write_all(b"HELLO 1\n").unwrap();
    assert_eq!(read_line(&mut client), "MSKS ERR timeout\n");
    assert_eq!(read_until_eof(&mut client), "");
}

#[test]
fn exec_failure_closes_the_session_after_ok() {
    let dir = std::env::temp_dir().join("msks-helper-integration");
    std::fs::create_dir_all(&dir).unwrap();
    let home = dir.join("home-badshell");
    std::fs::create_dir_all(&home).unwrap();
    let passwd = dir.join("passwd-badshell");
    std::fs::write(
        &passwd,
        format!("msks:x:1000:1000::{}:/nonexistent/shell\n", home.display()),
    )
    .unwrap();
    let fixture = spawn_helper(Some(&passwd), 10_000, None);
    let mut client = connect(&fixture);
    send_prelude(&mut client, "msks", 24, 80);
    assert_eq!(read_line(&mut client), "MSKS OK msks\n");
    // The shell never starts: EOF, not a fallback.
    assert_eq!(read_until_eof(&mut client), "");
}

#[test]
fn usage_exits_two() {
    for args in [<Vec<&str>>::new(), vec!["--nope"], vec!["0"], vec!["70000"]] {
        let mut command = Command::new(HELPER);
        if !args.is_empty() {
            command.args(&args);
        }
        let status = command.status().unwrap();
        assert_eq!(status.code(), Some(2), "{args:?}");
    }
}

/// The console challenge, end to end through the real binary: a
/// trust store planted for a real key, a real signature over the
/// served nonce, and the shell behind it.
#[test]
fn console_auth_admits_a_real_signature() {
    let _auth = AUTH_LOCK.lock().unwrap_or_else(|p| p.into_inner());
    let dir = std::env::temp_dir().join(format!("msks-helper-auth-ok-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let key = dir.join("key");
    let status = Command::new("ssh-keygen")
        .args(["-q", "-t", "ed25519", "-N", "", "-f"])
        .arg(&key)
        .status()
        .unwrap();
    assert!(status.success());
    let public = std::fs::read_to_string(key.with_extension("pub")).unwrap();
    let signers = dir.join("signers");
    std::fs::write(&signers, format!("ws-int {}", public)).unwrap();
    let (passwd, user) = test_passwd();
    let fixture = spawn_helper(Some(&passwd), 10_000, Some(&signers));
    let mut client = connect(&fixture);
    send_prelude(&mut client, &user, 24, 80);
    assert_eq!(read_line(&mut client), format!("MSKS OK {user}\n"));

    let challenge = read_line_within(&mut client, Duration::from_secs(30), "challenge");
    let nonce_hex = challenge
        .strip_prefix("AUTH CHALLENGE ")
        .unwrap_or_else(|| panic!("expected a challenge, got {challenge:?}"));
    let nonce = (0..nonce_hex.trim().len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&nonce_hex.trim()[i..i + 2], 16).unwrap())
        .collect::<Vec<u8>>();
    let nonce_file = dir.join("nonce");
    std::fs::write(&nonce_file, &nonce).unwrap();
    let status = Command::new("ssh-keygen")
        .args(["-Y", "sign", "-q", "-f"])
        .arg(&key)
        .arg("-n")
        .arg("msks-console")
        .arg(&nonce_file)
        .status()
        .unwrap();
    assert!(status.success());
    let armored = std::fs::read_to_string(dir.join("nonce.sig")).unwrap();
    let body: String = armored
        .lines()
        .filter(|line| !line.starts_with('-'))
        .collect();
    let blob = msks_console_helper::auth::b64_decode(&body).expect("armored body decodes");
    client
        .write_all(
            format!(
                "AUTH SIG {}\n",
                msks_console_helper::auth::b64_encode(&blob, 0)
            )
            .as_bytes(),
        )
        .unwrap();
    assert_eq!(
        read_line_within(&mut client, Duration::from_secs(30), "auth verdict"),
        "AUTH OK\n"
    );

    let marker = format!("a{}", std::process::id());
    client
        .write_all(format!("echo {marker}\n").as_bytes())
        .unwrap();
    let mut seen = String::new();
    let mut byte = [0u8; 1];
    client
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    while !seen.contains(&marker) {
        match client.read(&mut byte) {
            Ok(0) => panic!("session ended before the marker: {seen}"),
            Ok(_) => seen.push(byte[0] as char),
            Err(error) => panic!("read failed: {error}"),
        }
    }
    drop(client);
    let _ = std::fs::remove_dir_all(&dir);
}

/// The same wiring, refused: a signature that verifies against
/// nothing gets one refusal line and a closed session.
#[test]
fn console_auth_refuses_garbage() {
    let _auth = AUTH_LOCK.lock().unwrap_or_else(|p| p.into_inner());
    // No key generation needed: any principal line turns the
    // challenge on, and a garbage signature fails the verify
    // regardless of what key the store names.
    let dir = std::env::temp_dir().join(format!("msks-helper-auth-no-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let signers = dir.join("signers");
    std::fs::write(
        &signers,
        "ws-int ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAtest\n",
    )
    .unwrap();
    let (passwd, user) = test_passwd();
    let fixture = spawn_helper(Some(&passwd), 10_000, Some(&signers));
    let mut client = connect(&fixture);
    send_prelude(&mut client, &user, 24, 80);
    assert_eq!(read_line(&mut client), format!("MSKS OK {user}\n"));
    let window = Duration::from_secs(30);
    let challenge = read_line_within(&mut client, window, "challenge");
    assert!(
        challenge.starts_with("AUTH CHALLENGE "),
        "expected a challenge, got {challenge:?}"
    );
    client
        .write_all(b"AUTH SIG bm90LXNpZ25hdHVyZQ==\n")
        .unwrap();
    let refusal = read_line_within(&mut client, window, "challenge");
    assert_eq!(refusal, "MSKS ERR auth\n", "got {challenge:?} first");
    assert_eq!(read_until_eof(&mut client), "");
    let _ = std::fs::remove_dir_all(&dir);
}
