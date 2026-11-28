//! One helper per session: a lock held for its whole life, and its socket.
//! A second `serve` hands its request to the first and exits.

use crate::client::{self, ClientError};
use crate::protocol::Req;
use crate::publish::Paths;
use serde_json::{json, Value};
use std::fs::{self, File, OpenOptions};
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixListener;
use std::path::PathBuf;
use std::time::{Duration, Instant};

/// How long a second `serve` keeps trying to reach the first (it may be
/// starting, or on its way out).
pub const HANDOFF_WAIT: Duration = Duration::from_secs(2);

pub struct Instance {
    pub listener: UnixListener,
    socket: PathBuf,
    _lock: File,
}

impl Drop for Instance {
    fn drop(&mut self) {
        // Before the lock goes: a new helper never loses its socket to us.
        let _ = fs::remove_file(&self.socket);
    }
}

pub enum Start {
    /// This process is the helper.
    Server(Instance),
    /// Another helper runs and answered the request.
    Forwarded(Value),
}

fn try_lock(paths: &Paths) -> io::Result<Option<File>> {
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(paths.lock())?;
    // SAFETY: flock on a descriptor we own; no memory is passed.
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        return Ok(Some(lock));
    }
    let err = io::Error::last_os_error();
    if err.kind() == io::ErrorKind::WouldBlock {
        Ok(None)
    } else {
        Err(err)
    }
}

pub fn start(paths: &Paths, initial: Option<&Req>) -> io::Result<Start> {
    paths.prepare()?;
    let until = Instant::now() + HANDOFF_WAIT;
    loop {
        if let Some(lock) = try_lock(paths)? {
            let socket = paths.socket();
            let _ = fs::remove_file(&socket);
            let listener = UnixListener::bind(&socket)?;
            fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
            return Ok(Start::Server(Instance {
                listener,
                socket,
                _lock: lock,
            }));
        }
        let Some(req) = initial else {
            return Ok(Start::Forwarded(json!({"ok": true})));
        };
        match client::request(paths, req) {
            Ok(answer) => return Ok(Start::Forwarded(answer)),
            Err(ClientError::Refused(why)) => {
                return Ok(Start::Forwarded(json!({"ok": false, "error": why})))
            }
            Err(_) if Instant::now() < until => std::thread::sleep(Duration::from_millis(50)),
            Err(e) => return Err(io::Error::other(e.to_string())),
        }
    }
}
