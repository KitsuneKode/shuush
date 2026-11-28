//! The files other parts of Shuush read: where they are and how they are
//! written. Names and formats are the fork's, so the HUD, the toggle, the
//! cleanup and the edit watcher read them unchanged.

use std::fs;
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::time::Duration;

/// After a private take Shuush leaves `field_text.off`: the field is not
/// written for this long after that (a password manager, a sign-in page).
pub const CAPTURE_PAUSE: Duration = Duration::from_secs(300);

#[derive(Debug, Clone)]
pub struct Paths {
    /// `$XDG_RUNTIME_DIR/voxtype`: the markers and the field text.
    pub voxtype: PathBuf,
    /// `$XDG_RUNTIME_DIR/shuush`: the helper's socket and lock.
    pub shuush: PathBuf,
}

impl Paths {
    pub fn from_env() -> Self {
        let rt = std::env::var_os("XDG_RUNTIME_DIR")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            // SAFETY: getuid has no preconditions and cannot fail.
            .unwrap_or_else(|| PathBuf::from(format!("/run/user/{}", unsafe { libc::getuid() })));
        Self {
            voxtype: rt.join("voxtype"),
            shuush: rt.join("shuush"),
        }
    }

    pub fn socket(&self) -> PathBuf {
        self.shuush.join("ime.sock")
    }
    pub fn lock(&self) -> PathBuf {
        self.shuush.join("ime.lock")
    }
    /// `{"before": …, "after": …}`: the focused field around the cursor.
    pub fn field(&self) -> PathBuf {
        self.voxtype.join("field_text.json")
    }
    pub fn field_off(&self) -> PathBuf {
        self.voxtype.join("field_text.off")
    }
    /// Present while the take's words show in the field (the HUD leaves them out).
    pub fn live_in_field(&self) -> PathBuf {
        self.voxtype.join("live_in_field")
    }
    /// Present while a password or PIN field is focused.
    pub fn sensitive_field(&self) -> PathBuf {
        self.voxtype.join("sensitive_field")
    }
    /// voxtype's one-word state file.
    pub fn state(&self) -> PathBuf {
        self.voxtype.join("state")
    }
    /// The fork's preview file.
    pub fn live_text(&self) -> PathBuf {
        self.voxtype.join("live_text")
    }
    pub fn take_dir(&self, take: &str) -> Option<PathBuf> {
        valid_take(take).then(|| self.voxtype.join("takes").join(take))
    }

    /// Create the helper's own directory, private to the user.
    pub fn prepare(&self) -> io::Result<()> {
        use std::os::unix::fs::DirBuilderExt;
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.shuush)?;
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.voxtype)
    }
}

/// Take IDs are the toggle's nanosecond stamps: digits only.
pub fn valid_take(take: &str) -> bool {
    !take.is_empty() && take.len() <= 32 && take.bytes().all(|b| b.is_ascii_digit())
}

/// Writes the markers only on a change, and removes what it wrote.
pub struct Publisher {
    paths: Paths,
    live_in_field: bool,
    sensitive: bool,
    private_take: Option<String>,
}

impl Publisher {
    pub fn new(paths: Paths) -> Self {
        Self {
            paths,
            live_in_field: false,
            sensitive: false,
            private_take: None,
        }
    }

    pub fn paths(&self) -> &Paths {
        &self.paths
    }

    /// Remove what an earlier helper may have left (it was killed).
    pub fn reset(&mut self) {
        for path in [
            self.paths.field(),
            self.paths.live_in_field(),
            self.paths.sensitive_field(),
        ] {
            let _ = fs::remove_file(path);
        }
        self.live_in_field = false;
        self.sensitive = false;
    }

    pub fn markers(&mut self, live_in_field: bool, sensitive: bool) {
        if live_in_field != self.live_in_field {
            self.live_in_field = live_in_field;
            mark(self.paths.live_in_field(), live_in_field);
        }
        if sensitive != self.sensitive {
            self.sensitive = sensitive;
            mark(self.paths.sensitive_field(), sensitive);
        }
    }

    /// A take whose field turned out to be a password field is private:
    /// Shuush's cleanup then keeps it out of history, the clipboard and the
    /// model.
    pub fn private_take(&mut self, take: &str) {
        if self.private_take.as_deref() == Some(take) {
            return;
        }
        let Some(dir) = self.paths.take_dir(take).filter(|d| d.is_dir()) else {
            return;
        };
        if let Err(e) = fs::write(dir.join("pp_private"), b"1") {
            crate::warn!("cannot mark the take private: {e}");
        }
        self.private_take = Some(take.to_string());
    }

    fn capture_paused(&self) -> bool {
        fs::metadata(self.paths.field_off())
            .and_then(|m| m.modified())
            .is_ok_and(|t| t.elapsed().is_ok_and(|age| age < CAPTURE_PAUSE))
    }

    pub fn field(&self, field: &Option<(String, String)>) {
        let path = self.paths.field();
        let Some((before, after)) = field.as_ref().filter(|_| !self.capture_paused()) else {
            let _ = fs::remove_file(&path);
            return;
        };
        let body = serde_json::json!({ "before": before, "after": after }).to_string();
        let tmp = path.with_extension("tmp");
        let written = (|| {
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(&tmp)?;
            f.write_all(body.as_bytes())?;
            fs::rename(&tmp, &path)
        })();
        if let Err(e) = written {
            crate::debug!("cannot write the field text: {e}");
        }
    }

    /// Everything gone (the helper exits).
    pub fn clear_all(&mut self) {
        self.markers(false, false);
        self.field(&None);
    }
}

fn mark(path: PathBuf, on: bool) {
    if on {
        if let Err(e) = fs::write(&path, b"1") {
            crate::warn!("cannot write {}: {e}", path.display());
        }
    } else {
        let _ = fs::remove_file(path);
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::live::tests::temp_dir;

    pub fn paths(name: &str) -> Paths {
        let rt = temp_dir(name);
        let paths = Paths {
            voxtype: rt.join("voxtype"),
            shuush: rt.join("shuush"),
        };
        paths.prepare().unwrap();
        paths
    }

    #[test]
    fn markers_follow_the_field_and_are_removed_at_exit() {
        let paths = paths("markers");
        let mut out = Publisher::new(paths.clone());
        out.markers(true, false);
        assert!(paths.live_in_field().exists());
        assert!(!paths.sensitive_field().exists());
        out.markers(false, true);
        assert!(!paths.live_in_field().exists());
        assert!(paths.sensitive_field().exists());
        out.clear_all();
        assert!(!paths.sensitive_field().exists());
    }

    #[test]
    fn a_left_over_marker_is_removed_at_start() {
        let paths = paths("reset");
        fs::write(paths.sensitive_field(), "1").unwrap();
        fs::write(paths.field(), "{}").unwrap();
        Publisher::new(paths.clone()).reset();
        assert!(!paths.sensitive_field().exists());
        assert!(!paths.field().exists());
    }

    #[test]
    fn the_field_is_written_private_and_paused_after_a_private_take() {
        use std::os::unix::fs::PermissionsExt;
        let paths = paths("field");
        let out = Publisher::new(paths.clone());
        out.field(&Some(("Dear \"Sam\"".into(), "!".into())));
        let body: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(paths.field()).unwrap()).unwrap();
        assert_eq!(
            body,
            serde_json::json!({"before": "Dear \"Sam\"", "after": "!"})
        );
        let mode = fs::metadata(paths.field()).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        fs::write(paths.field_off(), "").unwrap();
        out.field(&Some(("anything".into(), String::new())));
        assert!(!paths.field().exists(), "paused: removed, not written");
    }

    #[test]
    fn only_a_real_take_directory_is_marked_private() {
        let paths = paths("private");
        let mut out = Publisher::new(paths.clone());
        out.private_take("../../etc");
        out.private_take("123");
        assert!(!paths.voxtype.join("takes/123").exists(), "never created");
        fs::create_dir_all(paths.voxtype.join("takes/124")).unwrap();
        out.private_take("124");
        assert!(paths.voxtype.join("takes/124/pp_private").exists());
    }

    #[test]
    fn take_ids_are_digits() {
        assert!(valid_take("1764321234567890123"));
        assert!(!valid_take(""));
        assert!(!valid_take("12a"));
        assert!(!valid_take("../1"));
    }
}
