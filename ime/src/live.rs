//! Where the words heard so far come from.
//!
//! Today that is the companion fork's preview file
//! (`[output] streaming_preview_file = "auto"`, `$XDG_RUNTIME_DIR/voxtype/live_text`):
//! the whole live transcript, replaced atomically on every update. Stock
//! voxtype's preview file (upstream PR #1122 and the preview-file PR after it)
//! plugs in as another [`LiveSource`]; a different format is another
//! [`Format`]. A take without a source still binds for the commit and the
//! field text; it just shows no preedit.

use std::fs;
use std::io::Read;
use std::path::PathBuf;
use std::time::SystemTime;

/// A live transcript longer than this is not read (a broken writer).
pub const MAX_LIVE_BYTES: u64 = 1 << 20;

/// The words heard so far in the current take.
pub trait LiveSource: Send {
    /// The live text when it changed since the last call.
    fn poll(&mut self) -> Option<String>;
}

/// How a preview file holds the transcript.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    /// The transcript as plain UTF-8 (the fork's `live_text`).
    Text,
}

impl Format {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "text" => Some(Self::Text),
            _ => None,
        }
    }

    fn decode(self, raw: &str) -> String {
        match self {
            Self::Text => raw.trim().to_string(),
        }
    }
}

type Stamp = Option<(u64, SystemTime)>;

/// A file the recognizer rewrites with the whole live transcript.
///
/// Whatever the file holds when the take begins belongs to the last take:
/// it is ignored until the file changes (the writer empties it at session
/// start), so a stale transcript never flashes in the new field.
pub struct FileSource {
    path: PathBuf,
    format: Format,
    stamp: Stamp,
    text: Option<String>,
}

impl FileSource {
    pub fn new(path: PathBuf, format: Format) -> Self {
        let stamp = stamp(&path);
        Self {
            path,
            format,
            stamp,
            text: None,
        }
    }

    fn read(&self) -> Option<String> {
        let mut raw = String::new();
        fs::File::open(&self.path)
            .ok()?
            .take(MAX_LIVE_BYTES)
            .read_to_string(&mut raw)
            .ok()?;
        Some(self.format.decode(&raw))
    }
}

fn stamp(path: &PathBuf) -> Stamp {
    let meta = fs::metadata(path).ok()?;
    Some((meta.len(), meta.modified().ok()?))
}

impl LiveSource for FileSource {
    fn poll(&mut self) -> Option<String> {
        let now = stamp(&self.path);
        if now == self.stamp {
            return None;
        }
        self.stamp = now;
        if now.is_some_and(|(len, _)| len > MAX_LIVE_BYTES) {
            return None;
        }
        // A file that went away means nothing heard yet.
        let text = if now.is_none() {
            String::new()
        } else {
            self.read()?
        };
        if self.text.as_deref() == Some(text.as_str()) {
            return None;
        }
        self.text = Some(text.clone());
        Some(text)
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::path::Path;

    pub fn temp_dir(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("shuush-ime-test-{}-{}", std::process::id(), name));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Rewrite atomically, as the fork's PreviewFile does, with a new mtime.
    fn write(path: &Path, text: &str) {
        let tmp = path.with_extension("tmp");
        fs::write(&tmp, text).unwrap();
        let later = SystemTime::now() + std::time::Duration::from_secs(1);
        let old = stamp(&path.to_path_buf()).map(|(_, t)| t);
        let at = old.map_or(later, |t| {
            t.max(later) + std::time::Duration::from_millis(10)
        });
        fs::File::options()
            .write(true)
            .open(&tmp)
            .unwrap()
            .set_modified(at)
            .unwrap();
        fs::rename(&tmp, path).unwrap();
    }

    #[test]
    fn the_last_takes_text_is_ignored_until_the_file_changes() {
        let dir = temp_dir("live-stale");
        let path = dir.join("live_text");
        write(&path, "words of the last take");
        let mut source = FileSource::new(path.clone(), Format::Text);
        assert_eq!(source.poll(), None);
        write(&path, "");
        assert_eq!(source.poll().as_deref(), Some(""));
        write(&path, "hello\n");
        assert_eq!(source.poll().as_deref(), Some("hello"));
        assert_eq!(source.poll(), None, "unchanged");
        write(&path, "hello there");
        assert_eq!(source.poll().as_deref(), Some("hello there"));
    }

    #[test]
    fn a_missing_file_is_an_empty_transcript_once_it_appears() {
        let dir = temp_dir("live-missing");
        let path = dir.join("live_text");
        let mut source = FileSource::new(path.clone(), Format::Text);
        assert_eq!(source.poll(), None);
        write(&path, "first words");
        assert_eq!(source.poll().as_deref(), Some("first words"));
        fs::remove_file(&path).unwrap();
        assert_eq!(source.poll().as_deref(), Some(""));
    }

    #[test]
    fn a_rewrite_with_the_same_words_is_not_an_update() {
        let dir = temp_dir("live-same");
        let path = dir.join("live_text");
        let mut source = FileSource::new(path.clone(), Format::Text);
        write(&path, "same");
        assert_eq!(source.poll().as_deref(), Some("same"));
        write(&path, "same\n");
        assert_eq!(source.poll(), None);
    }

    #[test]
    fn formats_have_names() {
        assert_eq!(Format::parse("text"), Some(Format::Text));
        assert_eq!(Format::parse("json"), None);
    }
}
