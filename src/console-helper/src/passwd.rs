//! /etc/passwd and /etc/group parsing, in-process: the helper is a
//! static binary and must not depend on dlopen'd NSS machinery.

use std::fs;
use std::path::Path;

pub const USER_MAX: usize = 32;
pub const GROUPS_MAX: usize = 32;

#[derive(Debug, PartialEq)]
pub struct UserEntry {
    pub name: String,
    pub uid: u32,
    pub gid: u32,
    pub home: String,
    pub shell: String,
}

#[derive(Debug, PartialEq)]
pub enum UserLookup {
    Allowed(UserEntry),
    /// Present, but a system account outside the allowlist.
    Refused,
    /// No such user (or the passwd file is unreadable).
    Absent,
}

/// The allowed-account rule (#63): root plus regular users. System
/// accounts (0 < uid < 1000) are refused by name.
fn classify(_name: &str, fields: &[&str]) -> Option<UserLookup> {
    let (Ok(uid), Ok(gid)) = (fields[2].parse::<u32>(), fields[3].parse::<u32>()) else {
        return None;
    };
    if uid != 0 && uid < 1000 {
        return Some(UserLookup::Refused);
    }
    if !name_valid(fields[0]) {
        return None;
    }
    Some(UserLookup::Allowed(UserEntry {
        name: fields[0].to_string(),
        uid,
        gid,
        home: fields[5].to_string(),
        shell: fields[6].to_string(),
    }))
}

/// Same charset the prelude's USER line accepts — a passwd entry the
/// wire could never name is treated as absent rather than exploitable.
pub fn name_valid(name: &str) -> bool {
    let bytes = name.as_bytes();
    let first_ok = |b: u8| b.is_ascii_lowercase() || b == b'_';
    let rest_ok = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-';
    (1..=USER_MAX).contains(&bytes.len())
        && first_ok(bytes[0])
        && bytes[1..].iter().all(|&b| rest_ok(b))
}

/// Lookup against passwd file content.
pub fn lookup_user_in(name: &str, passwd: &str) -> UserLookup {
    for line in passwd.lines() {
        let fields: Vec<&str> = line.split(':').collect();
        if fields.len() < 7 || fields[0] != name {
            continue;
        }
        if let Some(lookup) = classify(name, &fields) {
            return lookup;
        }
    }
    UserLookup::Absent
}

/// Lookup against a passwd file path; unreadable means absent.
pub fn lookup_user_at(name: &str, path: &Path) -> UserLookup {
    match fs::read_to_string(path) {
        Ok(passwd) => lookup_user_in(name, &passwd),
        Err(_) => UserLookup::Absent,
    }
}

/// The user's gids: primary first, then every group membership, in
/// file order, capped at [`GROUPS_MAX`].
pub fn lookup_groups_in(name: &str, primary: u32, group: &str) -> Vec<u32> {
    let mut gids = vec![primary];
    for line in group.lines() {
        if gids.len() >= GROUPS_MAX {
            break;
        }
        let fields: Vec<&str> = line.split(':').collect();
        if fields.len() < 4 {
            continue;
        }
        let Ok(gid) = fields[2].parse::<u32>() else {
            continue;
        };
        if gid == primary {
            continue;
        }
        if fields[3].split(',').any(|member| member == name) {
            gids.push(gid);
        }
    }
    gids
}

/// [`lookup_groups_in`] against a group file path; unreadable means
/// the primary gid alone.
pub fn lookup_groups_at(name: &str, primary: u32, path: &Path) -> Vec<u32> {
    match fs::read_to_string(path) {
        Ok(group) => lookup_groups_in(name, primary, &group),
        Err(_) => vec![primary],
    }
}
