//! Talking to a running helper.

use crate::protocol::Req;
use crate::publish::Paths;
use serde_json::Value;
use std::io::{self, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::time::Duration;

/// How long a client waits for an answer (a commit waits for the compositor).
pub const ANSWER_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug)]
pub enum ClientError {
    /// No helper is running (or it is on its way out).
    NoHelper,
    /// The helper refused the request.
    Refused(String),
    Io(io::Error),
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoHelper => f.write_str("no input method helper is running"),
            Self::Refused(why) => f.write_str(why),
            Self::Io(e) => write!(f, "{e}"),
        }
    }
}

pub fn request(paths: &Paths, req: &Req) -> Result<Value, ClientError> {
    let mut stream = match UnixStream::connect(paths.socket()) {
        Ok(s) => s,
        Err(e)
            if matches!(
                e.kind(),
                io::ErrorKind::NotFound | io::ErrorKind::ConnectionRefused
            ) =>
        {
            return Err(ClientError::NoHelper)
        }
        Err(e) => return Err(ClientError::Io(e)),
    };
    stream
        .set_write_timeout(Some(ANSWER_TIMEOUT))
        .and_then(|_| stream.set_read_timeout(Some(ANSWER_TIMEOUT)))
        .map_err(ClientError::Io)?;
    let sent = stream
        .write_all(&req.encode())
        .and_then(|_| stream.shutdown(Shutdown::Write));
    if let Err(e) = sent {
        // A helper that closed the socket on its way out.
        return Err(match e.kind() {
            io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset => ClientError::NoHelper,
            _ => ClientError::Io(e),
        });
    }
    let mut answer = String::new();
    match stream.read_to_string(&mut answer) {
        Ok(_) => {}
        Err(e) if e.kind() == io::ErrorKind::ConnectionReset => return Err(ClientError::NoHelper),
        Err(e) => return Err(ClientError::Io(e)),
    }
    if answer.trim().is_empty() {
        return Err(ClientError::NoHelper);
    }
    let value: Value = serde_json::from_str(answer.trim())
        .map_err(|_| ClientError::Io(io::Error::other("the helper's answer is not JSON")))?;
    if value["ok"] == true {
        Ok(value)
    } else {
        Err(ClientError::Refused(
            value["error"].as_str().unwrap_or("refused").to_string(),
        ))
    }
}
