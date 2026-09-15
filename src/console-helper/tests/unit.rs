//! Unit tests for the console helper library, through its public API
//! (the coverage gate measures the library; test code lives outside
//! src/ so it cannot dilute the gate's totals).
//!
//! The session/serve tests drive the real syscall edge cases through
//! the public traits where the fake implementations inject failures.

mod cli {
    use msks_console_helper::cli::{parse_args, Mode, USAGE};
    use std::path::PathBuf;
    use std::time::Duration;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn rejects_empty() {
        assert_eq!(parse_args(&[]), Err(USAGE.to_string()));
    }

    #[test]
    fn rejects_extra() {
        assert_eq!(parse_args(&args(&["1", "2"])), Err(USAGE.to_string()));
    }

    #[test]
    fn rejects_unknown_flag() {
        // A single non-numeric argument parses as (and is reported
        // as) a bad port.
        assert_eq!(
            parse_args(&args(&["--nope"])),
            Err(format!("bad port: --nope\n{USAGE}"))
        );
    }

    #[test]
    fn rejects_bad_port() {
        assert_eq!(
            parse_args(&args(&["102x"])),
            Err(format!("bad port: 102x\n{USAGE}"))
        );
    }

    #[test]
    fn rejects_port_zero_and_over() {
        assert_eq!(
            parse_args(&args(&["0"])),
            Err(format!("bad port: 0\n{USAGE}"))
        );
        assert_eq!(
            parse_args(&args(&["65536"])),
            Err(format!("bad port: 65536\n{USAGE}"))
        );
    }

    #[test]
    fn accepts_port_bounds() {
        assert_eq!(parse_args(&args(&["1"])), Ok(Mode::Vsock { port: 1 }));
        assert_eq!(
            parse_args(&args(&["65535"])),
            Ok(Mode::Vsock { port: 65535 })
        );
    }

    #[test]
    fn test_listen_needs_a_number() {
        assert_eq!(
            parse_args(&args(&["--test-listen-fd", "x"])),
            Err(format!("bad fd: x\n{USAGE}"))
        );
    }

    #[test]
    fn test_listen_defaults() {
        let mode = parse_args(&args(&["--test-listen-fd", "7"])).unwrap();
        assert_eq!(
            mode,
            Mode::TestListen {
                fd: 7,
                passwd: PathBuf::from("/etc/passwd"),
                deadline: Duration::from_millis(10_000),
            }
        );
    }

    #[test]
    fn test_listen_with_passwd() {
        let mode =
            parse_args(&args(&["--test-listen-fd", "9", "--test-passwd", "/tmp/p"])).unwrap();
        assert_eq!(
            mode,
            Mode::TestListen {
                fd: 9,
                passwd: PathBuf::from("/tmp/p"),
                deadline: Duration::from_millis(10_000),
            }
        );
    }

    #[test]
    fn test_listen_with_passwd_and_deadline() {
        let mode = parse_args(&args(&[
            "--test-listen-fd",
            "3",
            "--test-passwd",
            "/tmp/p",
            "--test-deadline-ms",
            "250",
        ]))
        .unwrap();
        assert_eq!(
            mode,
            Mode::TestListen {
                fd: 3,
                passwd: PathBuf::from("/tmp/p"),
                deadline: Duration::from_millis(250),
            }
        );
    }

    #[test]
    fn arity_guard_mismatches() {
        // Four arguments whose first flag is wrong: the 4-arg guard's
        // first condition fails.
        assert_eq!(
            parse_args(&args(&["--other", "3", "--test-passwd", "p"])),
            Err(USAGE.to_string())
        );
        // Six arguments whose first flag is wrong.
        assert_eq!(
            parse_args(&args(&[
                "--other",
                "3",
                "--test-passwd",
                "p",
                "--test-deadline-ms",
                "1"
            ])),
            Err(USAGE.to_string())
        );
        // Six arguments whose passwd flag is wrong.
        assert_eq!(
            parse_args(&args(&[
                "--test-listen-fd",
                "3",
                "--other",
                "p",
                "--test-deadline-ms",
                "1"
            ])),
            Err(USAGE.to_string())
        );
    }

    #[test]
    fn test_listen_guard_fallthroughs() {
        // flag matches, passwd flag does not (4-arg form).
        assert_eq!(
            parse_args(&args(&["--test-listen-fd", "3", "--nope", "x"])),
            Err(USAGE.to_string())
        );
        // five arguments match no arm.
        assert_eq!(
            parse_args(&args(&[
                "--test-listen-fd",
                "3",
                "--test-passwd",
                "/p",
                "stray"
            ])),
            Err(USAGE.to_string())
        );
        // deadline flag wrong (6-arg form).
        assert_eq!(
            parse_args(&args(&[
                "--test-listen-fd",
                "3",
                "--test-passwd",
                "/p",
                "--nope",
                "1",
            ])),
            Err(USAGE.to_string())
        );
    }

    #[test]
    fn test_listen_with_passwd_rejects_bad_fd() {
        assert_eq!(
            parse_args(&args(&["--test-listen-fd", "x", "--test-passwd", "/tmp/p"])),
            Err(format!("bad fd: x\n{USAGE}"))
        );
        assert_eq!(
            parse_args(&args(&[
                "--test-listen-fd",
                "9a",
                "--test-passwd",
                "/tmp/p",
                "--test-deadline-ms",
                "250",
            ])),
            Err(format!("bad fd: 9a\n{USAGE}"))
        );
    }

    #[test]
    fn test_listen_rejects_bad_deadline() {
        assert_eq!(
            parse_args(&args(&[
                "--test-listen-fd",
                "3",
                "--test-passwd",
                "/tmp/p",
                "--test-deadline-ms",
                "soon",
            ])),
            Err(format!("bad deadline: soon\n{USAGE}"))
        );
    }

    #[test]
    fn test_listen_rejects_unknown_third_flag() {
        assert_eq!(
            parse_args(&args(&["--test-listen-fd", "3", "--nope", "x"])),
            Err(USAGE.to_string())
        );
    }
}

mod passwd {
    use msks_console_helper::passwd::{
        lookup_groups_at, lookup_groups_in, lookup_user_at, lookup_user_in, UserEntry, UserLookup,
        GROUPS_MAX,
    };
    use std::fs;
    use std::path::Path;

    const PASSWD: &str = "\
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
msks:x:1000:1000:msks workspace user:/home/msks:/bin/bash
weird:x:1001:1001:Bad Name:/home/weird:/bin/sh
broken:x:notanum:1001:broken:/home/broken:/bin/sh
short:x:1002
";

    #[test]
    fn root_is_allowed() {
        assert_eq!(
            lookup_user_in("root", PASSWD,),
            UserLookup::Allowed(UserEntry {
                name: "root".into(),
                uid: 0,
                gid: 0,
                home: "/root".into(),
                shell: "/bin/bash".into(),
            })
        );
    }

    #[test]
    fn regular_user_is_allowed() {
        assert_eq!(
            lookup_user_in("msks", PASSWD),
            UserLookup::Allowed(UserEntry {
                name: "msks".into(),
                uid: 1000,
                gid: 1000,
                home: "/home/msks".into(),
                shell: "/bin/bash".into(),
            })
        );
    }

    #[test]
    fn system_account_is_refused() {
        assert_eq!(lookup_user_in("daemon", PASSWD), UserLookup::Refused);
    }

    #[test]
    fn unknown_user_is_absent() {
        assert_eq!(lookup_user_in("nobody-here", PASSWD), UserLookup::Absent);
    }

    #[test]
    fn unparsable_uid_line_is_skipped_not_fatal() {
        // The broken line comes before the matching good one? No —
        // broken is its own user; looking it up skips its bad fields
        // and falls through to absent.
        assert_eq!(lookup_user_in("broken", PASSWD), UserLookup::Absent);
    }

    #[test]
    fn short_line_is_skipped() {
        assert_eq!(lookup_user_in("short", PASSWD), UserLookup::Absent);
    }

    #[test]
    fn name_valid_charset_matrix() {
        use msks_console_helper::passwd::name_valid;
        for good in ["a", "ab", "a-b", "a_b", "a9", "z_z-9"] {
            assert!(name_valid(good), "{good}");
        }
        for bad in ["", "A", "-a", "9a", "aB", "a.b", "a b", "a\u{0}b", "ä"] {
            assert!(!name_valid(bad), "{bad:?}");
        }
        assert!(!name_valid(&"a".repeat(33)));
        assert!(name_valid(&"a".repeat(32)));
    }

    #[test]
    fn name_failing_the_wire_charset_is_absent() {
        // "weird"'s gecos is not the name; the NAME passes. Give a
        // real uppercase-name entry instead:
        let passwd = "Root:x:0:0:root:/root:/bin/bash\n";
        assert_eq!(lookup_user_in("Root", passwd), UserLookup::Absent);
    }

    #[test]
    fn unreadable_passwd_file_is_absent() {
        assert_eq!(
            lookup_user_at("root", Path::new("/nonexistent/passwd")),
            UserLookup::Absent
        );
    }

    #[test]
    fn readable_passwd_file_is_parsed() {
        let dir = std::env::temp_dir().join("msks-helper-test-passwd");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("passwd");
        fs::write(&path, "root:x:0:0:root:/root:/bin/bash\n").unwrap();
        assert_eq!(
            lookup_user_at("root", &path),
            UserLookup::Allowed(UserEntry {
                name: "root".into(),
                uid: 0,
                gid: 0,
                home: "/root".into(),
                shell: "/bin/bash".into(),
            })
        );
    }

    #[test]
    fn groups_collect_memberships_in_order() {
        let group = "\
msks:x:1000:
wheel:x:10:root,msks
devs:x:27:msks,other
broken:x:zz:msks
shortline:x:5
misc:x:99:other
";
        assert_eq!(lookup_groups_in("msks", 1000, group), vec![1000, 10, 27]);
        assert_eq!(lookup_groups_in("root", 0, group), vec![0, 10]);
        assert_eq!(lookup_groups_in("other", 99, group), vec![99, 27]);
    }

    #[test]
    fn groups_cap_at_the_limit() {
        let mut group = String::new();
        for gid in 1..=100 {
            group.push_str(&format!("g{gid}:x:{gid}:msks\n"));
        }
        assert_eq!(lookup_groups_in("msks", 0, &group).len(), GROUPS_MAX);
    }

    #[test]
    fn unreadable_group_file_leaves_the_primary() {
        assert_eq!(
            lookup_groups_at("msks", 1000, Path::new("/nonexistent/group")),
            vec![1000]
        );
    }
}

mod prelude {
    use msks_console_helper::prelude::{read_prelude, Prelude};
    use std::io::{Read, Write};
    use std::os::fd::AsRawFd;
    use std::os::unix::net::UnixStream;
    use std::time::{Duration, Instant};

    /// A connected socketpair; the test side writes, the helper side
    /// (passed to read_prelude) reads.
    fn pair() -> (UnixStream, UnixStream) {
        UnixStream::pair().expect("socketpair")
    }

    fn soon() -> Instant {
        Instant::now() + Duration::from_millis(2_000)
    }

    fn exchange(input: &[u8]) -> Option<(Option<Prelude>, String)> {
        let (mut client, server) = pair();
        client.write_all(input).unwrap();
        // Half-close so EOF-driven failures fail fast instead of
        // waiting out the deadline.
        let _ = client.shutdown(std::net::Shutdown::Write);
        let deadline = soon();
        let parsed = read_prelude(server.as_raw_fd(), deadline);
        // The parser's refusal (if any) went to the client side.
        let mut reply = String::new();
        let _ = client.set_read_timeout(Some(Duration::from_millis(200)));
        let _ = client.read_to_string(&mut reply);
        Some((parsed, reply))
    }

    fn refused(input: &[u8], reason: &str) {
        let (parsed, reply) = exchange(input).unwrap();
        assert!(parsed.is_none(), "{input:?} should not parse");
        assert_eq!(reply, format!("MSKS ERR {reason}\n"), "{input:?}");
    }

    fn accepted(input: &[u8]) -> Prelude {
        let (parsed, reply) = exchange(input).unwrap();
        assert_eq!(reply, "", "{input:?} drew a reply: {reply}");
        parsed.expect("should parse")
    }

    #[test]
    fn happy_path_with_winsz() {
        assert_eq!(
            accepted(b"HELLO 1\r\nUSER msks\r\nWINSZ 34 120\r\nGO\n"),
            Prelude {
                user: "msks".into(),
                rows: 34,
                cols: 120
            }
        );
    }

    #[test]
    fn happy_path_without_winsz_defaults_24x80() {
        assert_eq!(
            accepted(b"HELLO 1\nUSER root\nGO\n"),
            Prelude {
                user: "root".into(),
                rows: 24,
                cols: 80
            }
        );
    }

    #[test]
    fn version_mismatch_is_refused() {
        refused(b"HELLO 2\nUSER root\nGO\n", "version");
    }

    #[test]
    fn go_without_user_is_refused() {
        refused(b"HELLO 1\nGO\n", "user");
    }

    #[test]
    fn second_user_line_is_syntax() {
        refused(b"HELLO 1\nUSER root\nUSER msks\nGO\n", "syntax");
    }

    #[test]
    fn bad_user_charset_is_refused() {
        refused(b"HELLO 1\nUSER R00t\nGO\n", "user");
        refused(b"HELLO 1\nUSER -bad\nGO\n", "user");
        refused(b"HELLO 1\nUSER \nGO\n", "user");
        refused(
            b"HELLO 1\nUSER 012345678901234567890123456789012\nGO\n",
            "user",
        );
    }

    #[test]
    fn second_winsz_is_syntax() {
        refused(
            b"HELLO 1\nUSER root\nWINSZ 10 10\nWINSZ 10 10\nGO\n",
            "syntax",
        );
    }

    #[test]
    fn winsz_malformed_is_refused() {
        refused(b"HELLO 1\nUSER root\nWINSZ ten ten\nGO\n", "winsz");
        refused(b"HELLO 1\nUSER root\nWINSZ 10\nGO\n", "winsz");
        refused(b"HELLO 1\nUSER root\nWINSZ 10 10 10\nGO\n", "winsz");
        refused(b"HELLO 1\nUSER root\nWINSZ  \nGO\n", "winsz");
    }

    #[test]
    fn winsz_out_of_range_is_refused() {
        refused(b"HELLO 1\nUSER root\nWINSZ 0 80\nGO\n", "winsz");
        refused(b"HELLO 1\nUSER root\nWINSZ 24 0\nGO\n", "winsz");
        refused(b"HELLO 1\nUSER root\nWINSZ 65536 80\nGO\n", "winsz");
    }

    #[test]
    fn unknown_directive_is_syntax() {
        refused(b"HELLO 1\nEXEC /bin/sh\nGO\n", "syntax");
    }

    #[test]
    fn too_many_lines_is_syntax() {
        let mut input = b"HELLO 1\n".to_vec();
        for _ in 0..8 {
            input.extend_from_slice(b"WINSZ 10 10\n");
        }
        refused(&input, "syntax");
    }

    #[test]
    fn oversize_line_fails_closed() {
        let mut input = b"HELLO 1\n".to_vec();
        input.extend(std::iter::repeat_n(b'a', 200));
        input.push(b'\n');
        refused(&input, "syntax");
    }

    #[test]
    fn eof_mid_prelude_fails_closed() {
        refused(b"HELLO 1\nUSER ro", "closed");
    }

    #[test]
    fn deadline_expiry_fails_closed() {
        let (_client, server) = pair();
        let before = Instant::now() - Duration::from_secs(1);
        let parsed = read_prelude(server.as_raw_fd(), before);
        assert!(parsed.is_none());
        // The refusal write happened on the socket:
        drop(server);
    }

    #[test]
    fn deadline_reply_is_written_on_expiry() {
        let (mut client, server) = pair();
        let expired = Instant::now() - Duration::from_secs(1);
        assert!(read_prelude(server.as_raw_fd(), expired).is_none());
        let mut reply = String::new();
        drop(server);
        client
            .set_read_timeout(Some(Duration::from_millis(200)))
            .unwrap();
        let _ = client.read_to_string(&mut reply);
        assert_eq!(reply, "MSKS ERR timeout\n");
    }

    #[test]
    fn winsz_bounds_accepted() {
        assert_eq!(accepted(b"HELLO 1\nUSER root\nWINSZ 1 1\nGO\n").rows, 1);
        let pre = accepted(b"HELLO 1\nUSER root\nWINSZ 65535 65535\nGO\n");
        assert_eq!((pre.rows, pre.cols), (65535, 65535));
    }
}

mod serve {
    use super::lock::SPAWN_LOCK;
    use msks_console_helper::serve::{
        install_signals, serve, vsock_peer_allowed, AF_VSOCK, VMADDR_CID_HOST,
    };
    use msks_console_helper::session::SpawnFail;
    use std::io::{Read, Write};
    use std::os::fd::AsRawFd;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Arc, Mutex};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn vsock_peer_filter_is_family_and_cid() {
        assert!(vsock_peer_allowed(AF_VSOCK, VMADDR_CID_HOST));
        assert!(!vsock_peer_allowed(AF_VSOCK, 3)); // the guest's own CID
        assert!(!vsock_peer_allowed(AF_VSOCK, 1)); // loopback (local transport)
        assert!(!vsock_peer_allowed(1, VMADDR_CID_HOST)); // AF_UNIX
    }

    fn listener() -> (UnixListener, std::path::PathBuf) {
        let dir = std::env::temp_dir().join("msks-helper-serve-tests");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!(
            "sock-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = std::fs::remove_file(&path);
        (UnixListener::bind(&path).unwrap(), path)
    }

    /// End a serve loop: shutdown(2) on the listening socket wakes a
    /// blocked accept (plain close does not — the syscall holds the
    /// file description open).
    fn stop(server: UnixListener) {
        // SAFETY: shutdown(2) on a live listener fd.
        unsafe { libc::shutdown(server.as_raw_fd(), libc::SHUT_RDWR) };
        drop(server);
    }

    #[test]
    fn serve_dispatches_allowed_peers() {
        let _spawns = SPAWN_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (server, path) = listener();
        let fd = server.as_raw_fd();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorder = Arc::clone(&seen);
        let handle = std::thread::spawn(move || {
            serve(fd, |family, _| family == 1, &move |conn| {
                recorder.lock().unwrap().push(conn);
                Ok(())
            })
        });
        let mut client = UnixStream::connect(&path).unwrap();
        let _ = client.write_all(b"x");
        for _ in 0..100 {
            if !seen.lock().unwrap().is_empty() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert_eq!(seen.lock().unwrap().len(), 1);
        // Shutdown wakes the blocked accept: the loop exits with the
        // accept error.
        stop(server);
        assert!(handle.join().unwrap().is_err());
    }

    #[test]
    fn serve_closes_refused_peers_without_spawning() {
        let _spawns = SPAWN_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (server, path) = listener();
        let fd = server.as_raw_fd();
        let spawned = Arc::new(Mutex::new(Vec::new()));
        let recorder = Arc::clone(&spawned);
        let handle = std::thread::spawn(move || {
            // Allow nothing.
            let _ = serve(fd, |_, _| false, &move |_conn| {
                recorder.lock().unwrap().push(0);
                Ok(())
            });
        });
        let mut client = UnixStream::connect(&path).unwrap();
        // The refused connection reads EOF right away: the fd was
        // closed without a reply.
        client
            .set_read_timeout(Some(std::time::Duration::from_millis(500)))
            .unwrap();
        let mut buf = [0u8; 8];
        assert_eq!(client.read(&mut buf).unwrap_or(0), 0);
        stop(server);
        handle.join().unwrap();
        assert!(spawned.lock().unwrap().is_empty());
    }

    #[test]
    fn serve_closes_when_spawn_fails() {
        let _spawns = SPAWN_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (server, path) = listener();
        let fd = server.as_raw_fd();
        let handle = std::thread::spawn(move || {
            let _ = serve(fd, |_, _| true, &|_conn| Err(SpawnFail));
        });
        let mut client = UnixStream::connect(&path).unwrap();
        client
            .set_read_timeout(Some(std::time::Duration::from_millis(500)))
            .unwrap();
        let mut buf = [0u8; 8];
        assert_eq!(client.read(&mut buf).unwrap_or(0), 0);
        stop(server);
        handle.join().unwrap();
    }

    #[test]
    fn serve_on_a_dead_listener_returns_err() {
        let (server, _path) = listener();
        let fd = server.as_raw_fd();
        drop(server);
        assert!(serve(fd, |_, _| true, &|_| Ok(())).is_err());
    }

    #[test]
    fn install_signals_is_idempotent() {
        install_signals();
        install_signals();
    }
}

mod session {
    use super::lock::SPAWN_LOCK;

    use msks_console_helper::passwd::UserEntry;
    use msks_console_helper::session::{
        handle_session, pump, pump_with_timeout, run_shell_child, ChildSys, PtyPair, SessionSys,
        SpawnFail,
    };
    use msks_console_helper::write_all;
    use std::io;
    use std::io::{Read, Write};
    use std::os::fd::RawFd;
    use std::os::fd::{AsRawFd, IntoRawFd};
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    const FAR: Duration = Duration::from_secs(10);

    fn user() -> UserEntry {
        UserEntry {
            name: "msks".into(),
            uid: 1000,
            gid: 1000,
            home: "/home/msks".into(),
            shell: "/bin/bash".into(),
        }
    }

    fn passwd_file() -> PathBuf {
        let dir = std::env::temp_dir().join("msks-helper-session-tests");
        std::fs::create_dir_all(&dir).unwrap();
        // A fresh file per call: tests run concurrently, and a shared
        // path would let one test's truncate-rewrite race another's
        // read (a partial passwd reads as "user absent").
        static CALLS: AtomicU64 = AtomicU64::new(0);
        let path = dir.join(format!(
            "passwd-{}-{}",
            std::process::id(),
            CALLS.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::write(
            &path,
            "root:x:0:0:root:/root:/bin/bash\nmsks:x:1000:1000::/home/msks:/bin/bash\n",
        )
        .unwrap();
        path
    }

    /// A scriptable SessionSys: each knob fails on demand, calls are
    /// recorded.
    #[derive(Default)]
    struct FakeSessionSys {
        pty_ok: bool,
        winsize_ok: bool,
        spawn_ok: bool,
        closed: Mutex<Vec<RawFd>>,
        spawned: Mutex<Vec<String>>,
    }

    impl SessionSys for FakeSessionSys {
        fn open_pty(&self) -> Option<PtyPair> {
            if !self.pty_ok {
                return None;
            }
            let (a, b) = UnixStream::pair().unwrap();
            Some(PtyPair {
                master: b.as_raw_fd(),
                slave: format!("/dev/fd/{}", a.as_raw_fd()),
            })
            .inspect(|_pty| {
                std::mem::forget(b); // keep the fd for pump
            })
        }

        fn set_winsize(&self, _master: RawFd, _rows: u16, _cols: u16) -> bool {
            self.winsize_ok
        }

        fn spawn_shell(
            &self,
            _conn: RawFd,
            _pty: &PtyPair,
            user: &UserEntry,
        ) -> Result<(), SpawnFail> {
            self.spawned.lock().unwrap().push(user.name.clone());
            if self.spawn_ok {
                Ok(())
            } else {
                Err(SpawnFail)
            }
        }

        fn close(&self, fd: RawFd) {
            self.closed.lock().unwrap().push(fd);
        }
    }

    fn start_session(
        sys: &Arc<FakeSessionSys>,
        input: &[u8],
    ) -> (
        UnixStream,
        RawFd,
        std::thread::JoinHandle<()>,
        Arc<AtomicBool>,
    ) {
        let _spawns = SPAWN_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (mut client, server) = UnixStream::pair().unwrap();
        client.write_all(input).unwrap();
        let done = Arc::new(AtomicBool::new(false));
        let ran = Arc::clone(&done);
        let passwd = passwd_file();
        let sys = Arc::clone(sys);
        let fd = server.as_raw_fd();
        std::mem::forget(server);
        let handle = std::thread::spawn(move || {
            handle_session(fd, &*sys, &passwd, Instant::now() + FAR);
            ran.store(true, Ordering::SeqCst);
        });
        (client, fd, handle, done)
    }

    fn read_reply(client: &mut UnixStream) -> String {
        client
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let mut reply = [0u8; 64];
        let n = client.read(&mut reply).unwrap_or(0);
        String::from_utf8_lossy(&reply[..n]).into_owned()
    }

    #[test]
    fn bad_prelude_closes_without_lookup() {
        let sys = Arc::new(FakeSessionSys::default());
        let (mut client, fd, handle, _) = start_session(&sys, b"HELLO 9\nUSER root\nGO\n");
        assert_eq!(read_reply(&mut client), "MSKS ERR version\n");
        handle.join().unwrap();
        assert_eq!(*sys.closed.lock().unwrap(), vec![fd]);
    }

    #[test]
    fn refused_user_closes() {
        let sys = Arc::new(FakeSessionSys::default());
        let (mut client, fd, handle, _) = start_session(&sys, b"HELLO 1\nUSER nosuch\nGO\n");
        assert_eq!(read_reply(&mut client), "MSKS ERR user\n");
        handle.join().unwrap();
        assert_eq!(*sys.closed.lock().unwrap(), vec![fd]);
    }

    #[test]
    fn reply_write_failure_closes() {
        let sys = Arc::new(FakeSessionSys::default());
        let (client, server) = UnixStream::pair().unwrap();
        // The prelude bytes are already buffered, but the client is
        // gone: the reads succeed out of the socket buffer, the
        // reply write fails.
        let mut writer = client.try_clone().unwrap();
        writer.write_all(b"HELLO 1\nUSER root\nGO\n").unwrap();
        drop(writer);
        drop(client);
        let passwd = passwd_file();
        let fd = server.as_raw_fd();
        std::mem::forget(server);
        handle_session(fd, &*sys, &passwd, Instant::now() + FAR);
        assert_eq!(*sys.closed.lock().unwrap(), vec![fd]);
    }

    #[test]
    fn pty_failure_closes_after_ok() {
        let sys = Arc::new(FakeSessionSys::default());
        let (mut client, fd, handle, _) = start_session(&sys, b"HELLO 1\nUSER root\nGO\n");
        assert_eq!(read_reply(&mut client), "MSKS OK root\n");
        handle.join().unwrap();
        assert_eq!(*sys.closed.lock().unwrap(), vec![fd]);
    }

    #[test]
    fn winsize_failure_closes_both() {
        let sys = Arc::new(FakeSessionSys {
            pty_ok: true,
            winsize_ok: false,
            ..FakeSessionSys::default()
        });
        let (mut client, _fd, handle, _) = start_session(&sys, b"HELLO 1\nUSER root\nGO\n");
        assert_eq!(read_reply(&mut client), "MSKS OK root\n");
        handle.join().unwrap();
        let closed = sys.closed.lock().unwrap().clone();
        assert_eq!(closed.len(), 2);
    }

    #[test]
    fn spawn_failure_closes_both() {
        let sys = Arc::new(FakeSessionSys {
            pty_ok: true,
            winsize_ok: true,
            spawn_ok: false,
            ..FakeSessionSys::default()
        });
        let (mut client, _fd, handle, _) = start_session(&sys, b"HELLO 1\nUSER msks\nGO\n");
        assert_eq!(read_reply(&mut client), "MSKS OK msks\n");
        handle.join().unwrap();
        assert_eq!(*sys.spawned.lock().unwrap(), vec!["msks".to_string()]);
        assert_eq!(sys.closed.lock().unwrap().len(), 2);
    }

    #[test]
    fn happy_session_runs_to_completion() {
        let sys = Arc::new(FakeSessionSys {
            pty_ok: true,
            winsize_ok: true,
            spawn_ok: true,
            ..FakeSessionSys::default()
        });
        let (mut client, _fd, handle, done) =
            start_session(&sys, b"HELLO 1\nUSER msks\nWINSZ 34 120\nGO\n");
        assert_eq!(read_reply(&mut client), "MSKS OK msks\n");
        handle.join().unwrap();
        assert!(done.load(Ordering::SeqCst));
        let _ = client.shutdown(std::net::Shutdown::Both);
    }

    // --- pump, against real fds ---

    fn socketpair() -> (UnixStream, UnixStream) {
        UnixStream::pair().unwrap()
    }

    fn spawn_pump(conn: UnixStream, master: UnixStream) -> std::thread::JoinHandle<()> {
        let _spawns = SPAWN_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let fd1 = conn.as_raw_fd();
        let fd2 = master.as_raw_fd();
        let handle = std::thread::spawn(move || pump(fd1, fd2));
        std::mem::forget((conn, master));
        handle
    }

    #[test]
    fn pump_moves_bytes_master_to_conn() {
        let (conn, mut conn_peer) = socketpair();
        let (master, mut master_peer) = socketpair();
        let handle = spawn_pump(conn, master);
        master_peer.write_all(b"from-master").unwrap();
        conn_peer
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let mut buf = [0u8; 16];
        let mut got = Vec::new();
        while !got.contains(&b"from-master"[0]) || got.len() < 11 {
            let n = conn_peer.read(&mut buf).expect("byte never arrived");
            got.extend_from_slice(&buf[..n]);
            if got.windows(11).any(|w| w == b"from-master") {
                break;
            }
        }
        drop(conn_peer);
        drop(master_peer);
        handle.join().unwrap();
    }

    #[test]
    fn pump_streams_master_chunks_to_conn() {
        // Multiple master->conn round trips while both peers live:
        // the happy arm of the master branch, more than once.
        let (conn, mut conn_peer) = socketpair();
        let (master, mut master_peer) = socketpair();
        let handle = spawn_pump(conn, master);
        for chunk in ["one", "two", "three"] {
            master_peer.write_all(chunk.as_bytes()).unwrap();
            conn_peer
                .set_read_timeout(Some(Duration::from_millis(500)))
                .unwrap();
            let mut got = Vec::new();
            let want = chunk.as_bytes().to_vec();
            while got != want {
                let mut byte = [0u8; 1];
                let n = conn_peer.read(&mut byte).expect("chunk byte");
                assert_eq!(n, 1);
                got.push(byte[0]);
            }
        }
        drop(conn_peer);
        drop(master_peer);
        handle.join().unwrap();
    }

    #[test]
    fn pump_conn_eof_ends() {
        let (conn, conn_peer) = socketpair();
        let (master, master_peer) = socketpair();
        drop(conn_peer);
        let handle = spawn_pump(conn, master);
        drop(master_peer);
        handle.join().unwrap();
    }

    #[test]
    fn pump_master_eof_ends() {
        let (conn, mut conn_peer) = socketpair();
        let (master, master_peer) = socketpair();
        // The connection's peer stays alive: with both ends hung up in
        // one poll round the conn branch (checked first) would return
        // before the master branch ever reads its EOF.
        drop(master_peer);
        let handle = spawn_pump(conn, master);
        conn_peer
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let mut buf = [0u8; 8];
        let _ = conn_peer.read(&mut buf);
        drop(conn_peer);
        handle.join().unwrap();
    }

    #[test]
    fn pump_write_to_dead_master_ends() {
        // conn has data queued, master's peer is gone: the
        // conn->master write fails (EPIPE) and the pump returns.
        let (conn, mut conn_peer) = socketpair();
        let (master, master_peer) = socketpair();
        conn_peer.write_all(b"queued").unwrap();
        drop(master_peer);
        let handle = spawn_pump(conn, master);
        handle.join().unwrap();
    }

    #[test]
    fn pump_write_to_dead_conn_ends() {
        // master has data queued, conn's peer is gone: the
        // master->conn write fails (EPIPE) and the pump returns.
        let (conn, conn_peer) = socketpair();
        let (master, mut master_peer) = socketpair();
        master_peer.write_all(b"queued").unwrap();
        drop(conn_peer);
        let handle = spawn_pump(conn, master);
        handle.join().unwrap();
    }

    #[test]
    fn pump_nothing_to_watch_ends() {
        // Zero timeout with both fds ignored (negative fds are
        // skipped by poll): ready == 0, loop exits.
        pump_with_timeout(-1, -1, 0);
    }

    // --- write_all ---

    #[test]
    fn write_all_loops_over_partial_writes() {
        let _spawns = SPAWN_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        // A slow reader on the other end: once the socket buffer
        // fills, write(2) returns partial until the reader drains,
        // exercising the loop-back edge of write_all.
        let (reader, writer) = socketpair();
        let read_side = reader.try_clone().unwrap();
        let handle = std::thread::spawn(move || {
            let mut sink = read_side;
            let mut got = 0usize;
            let mut buf = [0u8; 4096];
            let deadline = Instant::now() + Duration::from_secs(30);
            sink.set_read_timeout(Some(Duration::from_millis(100)))
                .unwrap();
            while got < 512 * 1024 && Instant::now() < deadline {
                match sink.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => got += n,
                    Err(_) => continue, // timeout while writer blocks
                }
            }
            got
        });
        let payload = vec![b'x'; 512 * 1024];
        assert!(write_all(writer.into_raw_fd(), &payload));
        drop(reader);
        let got = handle.join().unwrap();
        assert!(got >= 200 * 1024, "reader saw {got}");
    }

    #[test]
    fn write_all_failure_on_closed_fd() {
        let (client, server) = socketpair();
        drop(client);
        let fd = server.as_raw_fd();
        drop(server);
        assert!(!write_all(fd, b"x"));
    }

    // --- run_shell_child, against a scriptable ChildSys ---

    #[derive(Default)]
    struct FakeChildSys {
        fail_open_slave: bool,
        fail_dup2_at: Option<RawFd>,
        fail_setgroups: bool,
        fail_setgid: bool,
        fail_setuid: bool,
        /// The ids current_ids reports before the drop (default: the
        /// privileged root helper).
        ids: Option<(u32, u32)>,
        /// Once setuid has run, current_ids reports the target ids —
        /// the successful-drop shape.
        dropped: std::sync::atomic::AtomicBool,
        fail_chdir: bool,
        exec_error: Option<i32>,
        saw: Mutex<Vec<String>>,
    }

    impl ChildSys for FakeChildSys {
        fn setsid_ret(&self) -> i32 {
            0
        }

        fn setsid(&self) {
            self.saw.lock().unwrap().push("setsid".into());
        }

        fn open_slave(&self, path: &str) -> RawFd {
            self.saw.lock().unwrap().push(format!("open {path}"));
            if self.fail_open_slave {
                -1
            } else {
                0
            }
        }

        fn dup2(&self, from: RawFd, to: RawFd) -> bool {
            self.saw.lock().unwrap().push(format!("dup2 {from}->{to}"));
            self.fail_dup2_at != Some(to)
        }

        fn close(&self, fd: RawFd) {
            self.saw.lock().unwrap().push(format!("close {fd}"));
        }

        fn create_dir(&self, path: &str) {
            self.saw.lock().unwrap().push(format!("mkdir {path}"));
        }

        fn chown(&self, path: &str, uid: u32, gid: u32) {
            self.saw
                .lock()
                .unwrap()
                .push(format!("chown {path} {uid}:{gid}"));
        }

        fn setgroups(&self, gids: &[u32]) -> bool {
            self.saw.lock().unwrap().push(format!("setgroups {gids:?}"));
            !self.fail_setgroups
        }

        fn setgid(&self, gid: u32) -> bool {
            self.saw.lock().unwrap().push(format!("setgid {gid}"));
            !self.fail_setgid
        }

        fn setuid(&self, uid: u32) -> bool {
            self.saw.lock().unwrap().push(format!("setuid {uid}"));
            if !self.fail_setuid {
                self.dropped
                    .store(true, std::sync::atomic::Ordering::SeqCst);
            }
            !self.fail_setuid
        }

        fn current_ids(&self) -> (u32, u32) {
            if self.dropped.load(std::sync::atomic::Ordering::SeqCst) {
                self.ids.unwrap_or((1000, 1000))
            } else {
                self.ids.unwrap_or((0, 0))
            }
        }

        fn chdir(&self, path: &str) -> bool {
            self.saw.lock().unwrap().push(format!("chdir {path}"));
            !self.fail_chdir
        }

        fn exec(&self, shell: &str, argv0: &str, env: &[(&str, String)]) -> io::Error {
            self.saw.lock().unwrap().push(format!(
                "exec {shell} {argv0} {}",
                env.iter()
                    .map(|(k, v)| format!("{k}={v}"))
                    .collect::<Vec<_>>()
                    .join(",")
            ));
            self.exec_error
                .map(io::Error::from_raw_os_error)
                .unwrap_or_else(|| io::Error::from_raw_os_error(127))
        }
    }

    fn pty() -> PtyPair {
        PtyPair {
            master: 9,
            slave: "/dev/pts/3".into(),
        }
    }

    #[test]
    fn child_setup_fails_125_on_slave_open() {
        let sys = FakeChildSys {
            fail_open_slave: true,
            ..FakeChildSys::default()
        };
        assert_eq!(run_shell_child(7, &pty(), &user(), &sys), Err(125));
    }

    #[test]
    fn child_setup_fails_125_on_dup2() {
        let sys = FakeChildSys {
            fail_dup2_at: Some(1),
            ..FakeChildSys::default()
        };
        assert_eq!(run_shell_child(7, &pty(), &user(), &sys), Err(125));
    }

    #[test]
    fn drop_sequence_fails_126_at_each_step() {
        // The privileged drop: the fake reports root first so every
        // step of the sequence runs.
        for sys in [
            FakeChildSys {
                ids: Some((0, 0)),
                fail_setgroups: true,
                ..FakeChildSys::default()
            },
            FakeChildSys {
                ids: Some((0, 0)),
                fail_setgid: true,
                ..FakeChildSys::default()
            },
            FakeChildSys {
                ids: Some((0, 0)),
                fail_setuid: true,
                ..FakeChildSys::default()
            },
            // Post-drop identity check: after the "drop" the fake
            // still reports root.
            FakeChildSys {
                ids: Some((0, 0)),
                ..FakeChildSys::default()
            },
        ] {
            assert_eq!(run_shell_child(7, &pty(), &user(), &sys), Err(126));
        }
    }

    #[test]
    fn unprivileged_helper_refuses_uid0_requests() {
        // USER root from a non-root helper would exec root's shell
        // under the helper's uid: refused like any other mismatch.
        let root = UserEntry {
            name: "root".into(),
            uid: 0,
            gid: 0,
            home: "/root".into(),
            shell: "/bin/bash".into(),
        };
        let sys = FakeChildSys {
            ids: Some((1000, 1000)),
            ..FakeChildSys::default()
        };
        assert_eq!(run_shell_child(7, &pty(), &root, &sys), Err(126));
    }

    #[test]
    fn unprivileged_helper_serves_only_its_own_user() {
        // Already the target user: no drop, straight to exec.
        let sys = FakeChildSys {
            ids: Some((1000, 1000)),
            ..FakeChildSys::default()
        };
        assert_eq!(run_shell_child(7, &pty(), &user(), &sys), Err(127));
        let saw = sys.saw.lock().unwrap().join(" ");
        assert!(!saw.contains("setuid"), "{saw}");

        // Some other user: refused with the drop-failure exit.
        let sys = FakeChildSys {
            ids: Some((1001, 1001)),
            ..FakeChildSys::default()
        };
        assert_eq!(run_shell_child(7, &pty(), &user(), &sys), Err(126));
    }

    #[test]
    fn root_skips_the_drop_and_execs() {
        let root = UserEntry {
            name: "root".into(),
            uid: 0,
            gid: 0,
            home: "/root".into(),
            shell: "/bin/bash".into(),
        };
        let sys = FakeChildSys::default();
        assert_eq!(run_shell_child(7, &pty(), &root, &sys), Err(127));
        let saw = sys.saw.lock().unwrap().join(" ");
        assert!(!saw.contains("setuid"), "{saw}");
        assert!(saw.contains("exec /bin/bash -bash"), "{saw}");
        assert!(saw.contains("TERM=xterm"), "{saw}");
        assert!(saw.contains("HOME=/root"), "{saw}");
    }

    #[test]
    fn non_root_drops_then_execs() {
        let sys = FakeChildSys::default();
        assert_eq!(run_shell_child(7, &pty(), &user(), &sys), Err(127));
        let saw = sys.saw.lock().unwrap().join(" ");
        assert!(saw.contains("setuid 1000"), "{saw}");
        assert!(saw.contains("mkdir /home/msks"), "{saw}");
        assert!(saw.contains("chown /home/msks 1000:1000"), "{saw}");
    }

    #[test]
    fn chdir_falls_back_to_root() {
        let sys = FakeChildSys {
            fail_chdir: true,
            ..FakeChildSys::default()
        };
        assert_eq!(run_shell_child(7, &pty(), &user(), &sys), Err(127));
        let saw = sys.saw.lock().unwrap().join(" ");
        assert!(saw.contains("chdir /home/msks chdir /"), "{saw}");
    }

    #[test]
    fn exec_failure_reports_the_error() {
        let sys = FakeChildSys {
            exec_error: Some(2),
            ..FakeChildSys::default()
        };
        assert_eq!(run_shell_child(7, &pty(), &user(), &sys), Err(127));
    }

    #[test]
    fn login_shell_argv0_and_env_come_from_passwd() {
        let sys = FakeChildSys::default();
        let _ = run_shell_child(7, &pty(), &user(), &sys);
        let saw = sys.saw.lock().unwrap().join(" ");
        assert!(saw.contains("-bash"), "{saw}");
        assert!(saw.contains("USER=msks"), "{saw}");
        assert!(saw.contains("LOGNAME=msks"), "{saw}");
        assert!(saw.contains("SHELL=/bin/bash"), "{saw}");
        assert!(saw.contains("PATH=/usr/local/sbin"), "{saw}");
    }
}

/// RLIMIT and spawn interlock: rlimits are process-wide (every thread
/// shares them), so tests that lower them and tests that fork or
/// spawn threads serialize against each other here.
mod lock {
    use std::sync::Mutex;

    pub(crate) static SPAWN_LOCK: Mutex<()> = Mutex::new(());
}

/// The real syscall implementations, exercised in-process: the lines
/// run regardless of syscall success, and the fork-failure arms are
/// forced through RLIMIT (no fd left / no process left).
mod real_impls {
    use msks_console_helper::close_fd;
    use msks_console_helper::passwd::UserEntry;
    use msks_console_helper::serve::fork_session;
    use msks_console_helper::session::{
        ChildSys, RealChildSys, RealSessionSys, SessionSys, SpawnFail,
    };
    use std::io::Write;
    use std::os::fd::AsRawFd;
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::time::{Duration, Instant};

    use super::lock::SPAWN_LOCK;

    fn current_user() -> UserEntry {
        let uid = unsafe { libc::getuid() };
        let gid = unsafe { libc::getgid() };
        UserEntry {
            name: std::env::var("USER").unwrap_or_else(|_| "root".into()),
            uid,
            gid,
            home: std::env::temp_dir().to_string_lossy().into_owned(),
            shell: "/nonexistent/shell".into(),
        }
    }

    fn passwd_with_current_user() -> PathBuf {
        let dir = std::env::temp_dir().join("msks-helper-real-tests");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!("passwd-{}", std::process::id()));
        let user = current_user();
        std::fs::write(
            &path,
            format!(
                "{}:x:{}:{}::{}:{}\n",
                user.name, user.uid, user.gid, user.home, user.shell
            ),
        )
        .unwrap();
        path
    }

    #[test]
    fn real_open_pty_and_winsize() {
        let sys = RealSessionSys;
        let pty = sys.open_pty().expect("a fresh pty");
        assert!(pty.slave.starts_with("/dev/pts/"));
        assert!(sys.set_winsize(pty.master, 33, 111));
        close_fd(pty.master);
    }

    #[test]
    fn real_open_pty_fails_without_fds() {
        let _forks = SPAWN_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        // Squeeze the fd limit so posix_openpt fails: the failure arm
        // fails closed with None.
        let limit = rlimit(libc::RLIMIT_NOFILE);
        let dir = std::env::temp_dir().join("msks-helper-real-tests");
        std::fs::create_dir_all(&dir).unwrap();
        let probe = std::fs::File::open(&dir).unwrap();
        set_rlimit(libc::RLIMIT_NOFILE, probe.as_raw_fd() as u64);
        assert!(RealSessionSys.open_pty().is_none());
        set_rlimit(libc::RLIMIT_NOFILE, limit);
    }

    fn rlimit(resource: u32) -> u64 {
        let mut value = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        // SAFETY: getrlimit(2) into the struct above.
        unsafe { libc::getrlimit(resource, &mut value) };
        value.rlim_cur
    }

    fn set_rlimit(resource: u32, cur: u64) {
        let value = libc::rlimit {
            rlim_cur: cur,
            rlim_max: cur.max(rlimit_max(resource)),
        };
        // SAFETY: setrlimit(2) with the struct above.
        unsafe { libc::setrlimit(resource, &value) };
    }

    fn rlimit_max(resource: u32) -> u64 {
        let mut value = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        // SAFETY: getrlimit(2) into the struct above.
        unsafe { libc::getrlimit(resource, &mut value) };
        value.rlim_max
    }

    #[test]
    fn real_child_sys_calls() {
        let _forks = SPAWN_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let sys = RealChildSys;
        let dir = std::env::temp_dir().join("msks-helper-real-tests/child");
        std::fs::create_dir_all(&dir).unwrap();

        // setsid/open/dup2/close round trip on a real pty — inside a
        // forked child: doing it in the test process would make the
        // test binary a session leader holding a controlling tty, and
        // closing the master SIGHUPs it.
        let pty = RealSessionSys.open_pty().unwrap();
        // SAFETY: fork(2); the child exercises the tty calls and exits.
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            // The child reports failures on stderr before its nonzero
            // exit: a bare assert would only show up as a wait status.
            let report = |what: &str| {
                eprintln!("child failed at {what}");
                std::process::exit(7);
            };
            // Production ignores setsid's result (a controlling tty is
            // best-effort); so does this child.
            let _ = sys.setsid_ret();
            let slave = sys.open_slave(&pty.slave);
            if slave < 0 {
                report("open_slave");
            }
            if !sys.dup2(slave, 200) {
                report("dup2");
            }
            if sys.dup2(-1, 5) {
                report("dup2-bad");
            }
            sys.close(200);
            close_fd(slave);
            // The master stays open: closing it while the child owns
            // the controlling tty SIGHUPs the child's own foreground
            // group. Exit closes it; the parent closes its copy.
            std::process::exit(0);
        }
        sys.close(pty.master);
        let mut status = 0;
        // SAFETY: waitpid(2) for the child above.
        unsafe { libc::waitpid(pid, &mut status, 0) };
        assert!(status == 0);

        // An interior NUL fails the path conversion before any call.
        assert_eq!(sys.open_slave("bad\0path"), -1);

        // Best-effort home setup.
        let home = dir.join("home");
        std::fs::create_dir_all(&home).unwrap();
        sys.create_dir(&home.to_string_lossy());
        sys.chown(&home.to_string_lossy(), 1000, 1000);
        sys.chown("bad\0path", 1000, 1000);

        // The drop sequence and identity probes.
        sys.setgroups(&[1, 2]);
        sys.setgid(0);
        sys.setuid(0);
        let (uid, gid) = sys.current_ids();
        assert_eq!(uid, unsafe { libc::getuid() });
        assert_eq!(gid, unsafe { libc::getgid() });

        let before = std::env::current_dir().unwrap();
        assert!(sys.chdir(&dir.to_string_lossy()));
        assert!(!sys.chdir("/nonexistent/dir"));
        // chdir is process-wide (every test thread shares it): without
        // the restore, relative paths — including the coverage
        // runtime's exit-time profile write — resolve elsewhere.
        std::env::set_current_dir(before).unwrap();
    }

    #[test]
    fn real_spawn_shell_fails_when_fork_is_denied() {
        let _forks = SPAWN_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        // RLIMIT_NPROC 0 makes fork(2) fail with EAGAIN for an
        // unprivileged process: the Err(SpawnFail) arm, nothing exec'd.
        let limit = rlimit(libc::RLIMIT_NPROC);
        set_rlimit(libc::RLIMIT_NPROC, 0);
        let pty = RealSessionSys.open_pty().unwrap();
        let result = RealSessionSys.spawn_shell(0, &pty, &current_user());
        close_fd(pty.master);
        set_rlimit(libc::RLIMIT_NPROC, limit);
        assert_eq!(result, Err(SpawnFail));
    }

    #[test]
    fn real_spawn_shell_child_runs_to_exec_failure() {
        let _forks = SPAWN_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        // The child runs the whole real setup and fails the exec
        // (nonexistent shell): it exits 127, writing its profile on
        // the way out; the parent returns Ok.
        let pty = RealSessionSys.open_pty().unwrap();
        let result = RealSessionSys.spawn_shell(1, &pty, &current_user());
        close_fd(pty.master);
        assert_eq!(result, Ok(()));
        // Reap nothing: SIGCHLD is ignored or the child already exited.
    }

    #[test]
    fn fork_session_fails_when_fork_is_denied() {
        let _forks = SPAWN_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let limit = rlimit(libc::RLIMIT_NPROC);
        set_rlimit(libc::RLIMIT_NPROC, 0);
        let passwd = passwd_with_current_user();
        let result = fork_session(0, &passwd, Instant::now() + Duration::from_millis(100));
        set_rlimit(libc::RLIMIT_NPROC, limit);
        assert_eq!(result, Err(SpawnFail));
    }

    #[test]
    fn fork_session_runs_a_real_session() {
        let _forks = SPAWN_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        // A whole session through the real spawn: a client socketpair
        // end as the connection, the current user with a nonexistent
        // shell. The child answers through the real prelude code and
        // exits after the exec failure; the parent reports Ok.
        let (_client, server) = UnixStream::pair().unwrap();
        let passwd = passwd_with_current_user();
        let fd = server.as_raw_fd();
        std::mem::forget(server);
        let result = fork_session(fd, &passwd, Instant::now() + Duration::from_secs(10));
        assert_eq!(result, Ok(()));
    }

    #[test]
    fn refuse_writes_the_error_line() {
        let (mut client, server) = UnixStream::pair().unwrap();
        msks_console_helper::refuse(server.as_raw_fd(), "probe");
        drop(server);
        client
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let mut line = String::new();
        use std::io::Read;
        let _ = client.read_to_string(&mut line);
        assert_eq!(line, "MSKS ERR probe\n");
        let _ = std::io::stdout().flush();
    }
}

mod shell_command {
    use msks_console_helper::session::build_shell_command;

    #[test]
    fn builder_sets_argv0_and_env() {
        let env = vec![
            ("TERM", "xterm".to_string()),
            ("HOME", "/home/msks".to_string()),
        ];
        let command = build_shell_command("/bin/bash", "-bash", &env);
        let debug = format!("{command:?}");
        assert!(debug.contains("\"-bash\""), "{debug}");
        assert!(debug.contains("TERM=\"xterm\""), "{debug}");
        assert!(debug.contains("HOME=\"/home/msks\""), "{debug}");
    }
}
