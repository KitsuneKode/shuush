//! The control socket: one request per connection.
//!
//! A request is one JSON line (`{"cmd": "commit", "take": "…"}`) followed by
//! a body (the text, for `commit`) up to the end of the stream; the reply is
//! one JSON line. Values travel as JSON, never pasted into anything.

use crate::live::Format;
use serde_json::{json, Value};
use std::path::PathBuf;

/// Longest request accepted (a long take is a few tens of kB).
pub const MAX_REQUEST_BYTES: usize = 4 << 20;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Req {
    /// A take starts: bind, and show the live words from `live` if given.
    Begin {
        take: String,
        live: Option<PathBuf>,
        format: Format,
    },
    /// Bind for reading the field only, for [`crate::driver::LINGER`].
    Read,
    /// The take's final text, in place of the preedit.
    Commit {
        take: String,
        text: String,
    },
    /// Remove the preedit now and stop following the live words.
    Clear,
    /// The take is delivered some other way: clear, keep reading the field.
    End {
        take: Option<String>,
    },
    /// The take was cancelled: clear and let go of the input method now.
    Release {
        take: Option<String>,
    },
    /// The field's text around the cursor, now.
    Field,
    Status,
}

impl Req {
    pub fn encode(&self) -> Vec<u8> {
        let (header, body) = match self {
            Req::Begin { take, live, format } => (
                json!({"cmd": "begin", "take": take, "live": live, "format": match format {
                    Format::Text => "text",
                }}),
                "",
            ),
            Req::Read => (json!({"cmd": "read"}), ""),
            Req::Commit { take, text } => (json!({"cmd": "commit", "take": take}), text.as_str()),
            Req::Clear => (json!({"cmd": "clear"}), ""),
            Req::End { take } => (json!({"cmd": "end", "take": take}), ""),
            Req::Release { take } => (json!({"cmd": "release", "take": take}), ""),
            Req::Field => (json!({"cmd": "field"}), ""),
            Req::Status => (json!({"cmd": "status"}), ""),
        };
        let mut out = header.to_string().into_bytes();
        out.push(b'\n');
        out.extend_from_slice(body.as_bytes());
        out
    }

    pub fn decode(raw: &[u8]) -> Result<Self, String> {
        let split = raw.iter().position(|&b| b == b'\n').unwrap_or(raw.len());
        let header: Value =
            serde_json::from_slice(&raw[..split]).map_err(|_| "bad request".to_string())?;
        let body = raw.get(split + 1..).unwrap_or_default();
        let field = |name: &str| header.get(name).and_then(Value::as_str).map(str::to_string);
        let take = || -> Result<String, String> {
            field("take")
                .filter(|t| crate::publish::valid_take(t))
                .ok_or_else(|| "a take id is digits".to_string())
        };
        let opt_take = || -> Result<Option<String>, String> {
            match header.get("take") {
                None | Some(Value::Null) => Ok(None),
                _ => take().map(Some),
            }
        };
        match field("cmd").as_deref() {
            Some("begin") => Ok(Req::Begin {
                take: take()?,
                live: field("live").filter(|p| !p.is_empty()).map(PathBuf::from),
                format: match field("format") {
                    None => Format::Text,
                    Some(name) => {
                        Format::parse(&name).ok_or_else(|| "unknown live format".to_string())?
                    }
                },
            }),
            Some("read") => Ok(Req::Read),
            Some("commit") => Ok(Req::Commit {
                take: take()?,
                text: String::from_utf8(body.to_vec()).map_err(|_| "text is not UTF-8")?,
            }),
            Some("clear") => Ok(Req::Clear),
            Some("end") => Ok(Req::End { take: opt_take()? }),
            Some("release") => Ok(Req::Release { take: opt_take()? }),
            Some("field") => Ok(Req::Field),
            Some("status") => Ok(Req::Status),
            _ => Err("unknown command".to_string()),
        }
    }
}

/// How a `commit` ended, as the reply names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// The text is in the field.
    Committed,
    /// No text-input-v3 field is focused: type it (the preedit is gone).
    NotActive,
    /// Newlines, other control keys or a text too long for one commit:
    /// type it (the preedit is gone).
    NotCommittable,
    /// No input method: another one holds the seat, or none was bound.
    Unavailable,
    /// The take was cancelled; nothing was committed.
    Stale,
}

impl Outcome {
    pub fn name(self) -> &'static str {
        match self {
            Self::Committed => "committed",
            Self::NotActive => "not_active",
            Self::NotCommittable => "not_committable",
            Self::Unavailable => "unavailable",
            Self::Stale => "stale",
        }
    }

    pub fn from_name(name: &str) -> Option<Self> {
        [
            Self::Committed,
            Self::NotActive,
            Self::NotCommittable,
            Self::Unavailable,
            Self::Stale,
        ]
        .into_iter()
        .find(|o| o.name() == name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requests_round_trip() {
        for req in [
            Req::Begin {
                take: "17".into(),
                live: Some("/run/user/1000/voxtype/live_text".into()),
                format: Format::Text,
            },
            Req::Begin {
                take: "17".into(),
                live: None,
                format: Format::Text,
            },
            Req::Read,
            Req::Commit {
                take: "17".into(),
                text: "Hello,\n \"world\" {}".into(),
            },
            Req::Clear,
            Req::End { take: None },
            Req::End {
                take: Some("17".into()),
            },
            Req::Release { take: None },
            Req::Field,
            Req::Status,
        ] {
            assert_eq!(Req::decode(&req.encode()), Ok(req.clone()), "{req:?}");
        }
    }

    #[test]
    fn bad_requests_are_refused() {
        for raw in [
            &b"not json\n"[..],
            b"{\"cmd\":\"launch\"}\n",
            b"{\"cmd\":\"commit\",\"take\":\"../x\"}\nhi",
            b"{\"cmd\":\"commit\"}\nhi",
            b"{\"cmd\":\"commit\",\"take\":\"1\"}\n\xff",
            b"{\"cmd\":\"begin\",\"take\":\"1\",\"format\":\"xml\"}\n",
        ] {
            assert!(
                Req::decode(raw).is_err(),
                "{:?}",
                String::from_utf8_lossy(raw)
            );
        }
    }

    #[test]
    fn outcomes_have_names() {
        for o in [
            Outcome::Committed,
            Outcome::NotActive,
            Outcome::NotCommittable,
            Outcome::Unavailable,
            Outcome::Stale,
        ] {
            assert_eq!(Outcome::from_name(o.name()), Some(o));
        }
    }
}
