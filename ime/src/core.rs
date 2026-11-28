//! One helper's state: the take it serves, the live words, the binding and
//! the files Shuush reads. No sockets and no Wayland here, so all of it runs
//! in the tests against a recording sink.

use crate::driver::{Ack, Cmd, Driver, Sink};
use crate::live::{FileSource, LiveSource};
use crate::protocol::{Outcome, Req};
use crate::publish::{Paths, Publisher};
use crate::state::{CommitOutcome, MAX_STRING_BYTES};
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::time::{Duration, Instant};

/// How often the live words are looked at during a take.
pub const LIVE_POLL: Duration = Duration::from_millis(50);
/// How often voxtype's state is looked at during a take.
pub const STATE_POLL: Duration = Duration::from_secs(1);
/// A take nobody ended (the cleanup died, voxtype restarted) ends by itself
/// once voxtype has been idle this long.
pub const TAKE_IDLE: Duration = Duration::from_secs(60);
/// No take lasts longer than this, whatever voxtype's state says.
pub const TAKE_MAX: Duration = Duration::from_secs(30 * 60);
/// Field writes are coalesced: a burst of keystrokes is one write.
pub const FIELD_WRITE_DELAY: Duration = Duration::from_millis(300);
/// Cancelled takes remembered, so their late commit lands nowhere.
const RELEASED_KEPT: usize = 16;

/// Text the input method commits as is. Newlines and other control
/// characters mean key presses to a typing driver (Enter, Shift+Enter, Tab),
/// which a commit cannot reproduce.
pub fn committable(text: &str) -> bool {
    !text.chars().any(char::is_control) && text.len() <= MAX_STRING_BYTES
}

struct Take {
    id: String,
    live: Option<Box<dyn LiveSource>>,
    /// The live words last shown.
    text: String,
    began: Instant,
    idle_since: Option<Instant>,
    next_state_check: Instant,
    /// voxtype was seen recording this take.
    heard: bool,
}

/// voxtype is listening: the live words are still coming.
fn listening(state: &str) -> bool {
    matches!(state, "recording" | "streaming")
}

pub struct Core {
    driver: Driver,
    out: Publisher,
    take: Option<Take>,
    released: VecDeque<String>,
    pending_field: Option<(Instant, Option<(String, String)>)>,
}

impl Core {
    pub fn new(paths: Paths) -> Self {
        let mut out = Publisher::new(paths);
        out.reset();
        Self {
            driver: Driver::new(),
            out,
            take: None,
            released: VecDeque::new(),
            pending_field: None,
        }
    }

    /// Nothing to do: no take, nothing bound.
    pub fn should_exit(&self) -> bool {
        self.take.is_none() && !self.driver.status().bound
    }

    /// When [`tick`](Self::tick) must run next.
    pub fn deadline(&self, now: Instant) -> Option<Instant> {
        let take = self.take.as_ref().map(|t| {
            if t.live.is_some() {
                now + LIVE_POLL
            } else {
                t.next_state_check
            }
        });
        [
            self.driver.wake_deadline(),
            self.pending_field.as_ref().map(|(due, _)| *due),
            take,
        ]
        .into_iter()
        .flatten()
        .min()
    }

    pub fn event(&mut self, event: crate::state::Event, sink: &mut dyn Sink) {
        self.driver.event(event, sink);
        self.publish(Instant::now());
    }

    pub fn tick(&mut self, now: Instant, sink: &mut dyn Sink) {
        let mut ended = false;
        if let Some(take) = self.take.as_mut() {
            if take.live.is_some() {
                // Once voxtype stops listening the preedit keeps the last
                // words heard until the commit: what the cleanup writes to
                // the preview file next (the raw transcript, for the pill)
                // is not this take's live words.
                let state = std::fs::read_to_string(self.out.paths().state()).unwrap_or_default();
                if listening(state.trim()) {
                    take.heard = true;
                } else if take.heard {
                    take.live = None;
                }
            }
            if let Some(text) = take.live.as_mut().and_then(|live| live.poll()) {
                take.text = text.clone();
                self.driver.command(Cmd::Set(text), sink);
            }
            if now >= take.next_state_check {
                take.next_state_check = now + STATE_POLL;
                let state = std::fs::read_to_string(self.out.paths().state()).unwrap_or_default();
                if matches!(state.trim(), "" | "idle") {
                    let since = *take.idle_since.get_or_insert(now);
                    ended = now.duration_since(since) >= TAKE_IDLE;
                } else {
                    take.idle_since = None;
                }
                ended |= now.duration_since(take.began) >= TAKE_MAX;
            }
        }
        if ended {
            crate::info!("the take was never delivered or cancelled; letting it go");
            self.take = None;
            self.driver.command(Cmd::End, sink);
        }
        self.driver.tick(now, sink);
        self.publish(now);
    }

    /// Answer one request. True: send what is pending and wait for the
    /// compositor before replying.
    pub fn request(&mut self, req: Req, sink: &mut dyn Sink) -> (Value, bool) {
        let answer = self.answer(req, sink);
        self.publish(Instant::now());
        answer
    }

    fn current(&self, take: &str) -> bool {
        self.take.as_ref().is_some_and(|t| t.id == take)
    }

    fn answer(&mut self, req: Req, sink: &mut dyn Sink) -> (Value, bool) {
        let ok = json!({"ok": true});
        match req {
            Req::Begin { take, live, format } => {
                if self.released.contains(&take) {
                    return (
                        json!({"ok": false, "error": "the take was cancelled"}),
                        false,
                    );
                }
                if !self.current(&take) {
                    let now = Instant::now();
                    self.take = Some(Take {
                        id: take,
                        live: live
                            .map(|p| Box::new(FileSource::new(p, format)) as Box<dyn LiveSource>),
                        text: String::new(),
                        began: now,
                        idle_since: None,
                        next_state_check: now + STATE_POLL,
                        heard: false,
                    });
                    self.driver.command(Cmd::Begin, sink);
                }
                (ok, false)
            }
            Req::Read => {
                self.driver.command(Cmd::Read, sink);
                (ok, false)
            }
            Req::Commit { take, text } => {
                let outcome = self.commit(&take, &text, sink);
                (json!({"ok": true, "outcome": outcome.name()}), true)
            }
            Req::Clear => {
                if let Some(t) = self.take.as_mut() {
                    t.live = None;
                    t.text.clear();
                }
                self.driver.command(Cmd::Clear, sink);
                (ok, true)
            }
            Req::End { take } => {
                if take.as_deref().is_none_or(|id| self.current(id)) {
                    self.take = None;
                    self.driver.command(Cmd::End, sink);
                }
                (ok, true)
            }
            Req::Release { take } => {
                if let Some(id) = &take {
                    if !self.released.contains(id) {
                        if self.released.len() == RELEASED_KEPT {
                            self.released.pop_front();
                        }
                        self.released.push_back(id.clone());
                    }
                }
                if take.as_deref().is_none_or(|id| self.current(id)) {
                    self.take = None;
                    self.driver.command(Cmd::Release, sink);
                }
                (ok, true)
            }
            Req::Field => {
                self.driver.ask(sink);
                let field = self
                    .driver
                    .field()
                    .map(|(before, after)| json!({"before": before, "after": after}));
                (json!({"ok": true, "field": field}), true)
            }
            Req::Status => {
                let s = self.driver.status();
                (
                    json!({
                        "ok": true,
                        "bound": s.bound,
                        "active": s.active,
                        "sensitive": s.sensitive,
                        "showing": s.showing,
                        "lingering": s.lingering,
                        "take": self.take.as_ref().map(|t| t.id.clone()),
                        "live": self.take.as_ref().is_some_and(|t| t.live.is_some()),
                    }),
                    false,
                )
            }
        }
    }

    fn commit(&mut self, take: &str, text: &str, sink: &mut dyn Sink) -> Outcome {
        if self.released.iter().any(|id| id == take) {
            return Outcome::Stale;
        }
        let current = self.current(take);
        let outcome = if committable(text) {
            match self.driver.command(Cmd::Commit(text.to_string()), sink) {
                Ack::Committed(CommitOutcome::Committed) => Outcome::Committed,
                Ack::Committed(CommitOutcome::NotActive) => Outcome::NotActive,
                _ => Outcome::Unavailable,
            }
        } else {
            // Typed instead: the preedit must be gone before the first key.
            self.driver.command(Cmd::Clear, sink);
            if self.driver.status().bound {
                Outcome::NotCommittable
            } else {
                Outcome::Unavailable
            }
        };
        match self.take.as_ref() {
            // That take is over; keep reading the field it went into.
            _ if current => {
                self.take = None;
                self.driver.command(Cmd::End, sink);
            }
            // An earlier take landed while this one shows its words: show them again.
            Some(t) => {
                let text = t.text.clone();
                self.driver.command(Cmd::Set(text), sink);
            }
            None => {
                self.driver.command(Cmd::End, sink);
            }
        }
        outcome
    }

    fn publish(&mut self, now: Instant) {
        let status = self.driver.status();
        let live_in_field = self.take.is_some() && status.showing && !status.sensitive;
        self.out.markers(live_in_field, status.sensitive);
        if status.sensitive {
            if let Some(t) = &self.take {
                self.out.private_take(&t.id);
            }
        }
        if let Some(field) = self.driver.field_update() {
            // Gone is written at once; text waits for the burst to settle.
            let due = if field.is_none() {
                now
            } else {
                now + FIELD_WRITE_DELAY
            };
            let due = self
                .pending_field
                .as_ref()
                .map_or(due, |(d, _)| (*d).min(due));
            self.pending_field = Some((due, field));
        }
        if self
            .pending_field
            .as_ref()
            .is_some_and(|(due, _)| now >= *due)
        {
            if let Some((_, field)) = self.pending_field.take() {
                self.out.field(&field);
            }
        }
    }

    /// Release everything and remove every file (the helper exits).
    pub fn shutdown(&mut self, sink: &mut dyn Sink) {
        self.take = None;
        self.driver.shutdown(sink);
        self.pending_field = None;
        self.out.clear_all();
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::driver::tests::RecSink;
    use crate::live::Format;
    use crate::publish::tests::paths;
    use crate::state::{Event, PREEDIT_TAIL_BYTES};
    use std::fs;
    use std::path::PathBuf;

    struct Fake {
        core: Core,
        sink: RecSink,
        paths: Paths,
    }

    const TAKE: &str = "1001";

    impl Fake {
        fn new(name: &str) -> Self {
            let paths = paths(name);
            Self {
                core: Core::new(paths.clone()),
                sink: RecSink::default(),
                paths,
            }
        }
        fn req(&mut self, req: Req) -> Value {
            self.core.request(req, &mut self.sink).0
        }
        fn begin(&mut self, live: Option<PathBuf>) {
            let r = self.req(Req::Begin {
                take: TAKE.into(),
                live,
                format: Format::Text,
            });
            assert_eq!(r["ok"], true);
        }
        fn commit(&mut self, take: &str, text: &str) -> String {
            let r = self.req(Req::Commit {
                take: take.into(),
                text: text.into(),
            });
            r["outcome"].as_str().unwrap().to_string()
        }
        fn event(&mut self, e: Event) {
            self.core.event(e, &mut self.sink);
        }
        fn focus(&mut self) {
            self.event(Event::Activate);
            self.event(Event::Done);
        }
        fn tick(&mut self) {
            self.core.tick(Instant::now(), &mut self.sink);
        }
        fn log(&mut self) -> Vec<String> {
            std::mem::take(&mut self.sink.log)
        }
        fn live_file(&self) -> PathBuf {
            self.paths.live_text()
        }
        fn say(&mut self, words: &str) {
            write_live(&self.live_file(), words);
            self.tick();
        }
    }

    fn write_live(path: &std::path::Path, text: &str) {
        let tmp = path.with_extension("tmp");
        fs::write(&tmp, text).unwrap();
        // Distinct mtimes even on a coarse clock.
        static STEP: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
        let at = std::time::SystemTime::now()
            + Duration::from_millis(10 * STEP.fetch_add(1, std::sync::atomic::Ordering::Relaxed));
        fs::File::options()
            .write(true)
            .open(&tmp)
            .unwrap()
            .set_modified(at)
            .unwrap();
        fs::rename(&tmp, path).unwrap();
    }

    #[test]
    fn nothing_is_bound_until_a_take_begins_and_the_helper_is_done_after() {
        let mut fake = Fake::new("core-lifecycle");
        assert!(fake.core.should_exit(), "idle: nothing to do");
        assert!(fake.log().is_empty(), "nothing bound at rest");
        fake.begin(None);
        assert_eq!(fake.log(), ["bind:true"]);
        assert!(!fake.core.should_exit());
        fake.req(Req::Release {
            take: Some(TAKE.into()),
        });
        assert_eq!(fake.log(), ["unbind"]);
        assert!(fake.core.should_exit());
    }

    #[test]
    fn live_words_show_as_preedit_and_publish_live_in_field() {
        let mut fake = Fake::new("core-live");
        write_live(&fake.live_file(), "the last take's words");
        fake.begin(Some(fake.live_file()));
        fake.focus();
        fake.tick();
        assert!(
            !fake.log().iter().any(|l| l.contains("last take")),
            "stale text never shown"
        );
        assert!(!fake.paths.live_in_field().exists());
        fake.say("");
        fake.say("hello");
        assert_eq!(fake.log(), ["preedit:hello", "commit:1"]);
        assert!(fake.paths.live_in_field().exists());
        assert_eq!(fake.commit(TAKE, "Hello."), "committed");
        assert!(!fake.paths.live_in_field().exists());
        assert_eq!(fake.log(), ["preedit:", "commit_string:Hello.", "commit:1"]);
        // Delivered: the words that follow in the file are not this take's.
        fake.say("hello again");
        assert!(fake.log().is_empty());
        assert!(fake.core.driver.status().lingering);
    }

    #[test]
    fn the_tail_is_capped_with_an_ellipsis_at_a_word_start() {
        let mut fake = Fake::new("core-tail");
        fake.begin(Some(fake.live_file()));
        fake.focus();
        fake.log();
        let words: Vec<String> = (0..120).map(|i| format!("word{i}")).collect();
        let take = words.join(" ");
        fake.say(&take);
        let log = fake.log();
        let shown = log[0].strip_prefix("preedit:").unwrap();
        let body = shown
            .strip_prefix('\u{2026}')
            .expect("cut, with an ellipsis");
        assert!(body.len() <= PREEDIT_TAIL_BYTES, "{}", body.len());
        assert!(take.ends_with(body) && take.contains(&format!(" {body}")));
        assert_eq!(fake.commit(TAKE, &take), "committed");
        assert!(
            fake.log().contains(&format!("commit_string:{take}")),
            "whole take committed"
        );
    }

    #[test]
    fn a_password_field_shows_nothing_and_publishes_sensitive_field() {
        let mut fake = Fake::new("core-sensitive");
        fs::create_dir_all(fake.paths.voxtype.join("takes").join(TAKE)).unwrap();
        fake.begin(Some(fake.live_file()));
        fake.event(Event::Activate);
        fake.event(Event::ContentHidden(true));
        fake.event(Event::Surrounding {
            text: "hunter2".into(),
            cursor: 7,
        });
        fake.event(Event::Done);
        fake.say("my password is");
        assert!(!fake.log().iter().any(|l| l.contains("password")));
        assert!(fake.paths.sensitive_field().exists());
        assert!(!fake.paths.live_in_field().exists());
        assert!(fake
            .paths
            .voxtype
            .join("takes")
            .join(TAKE)
            .join("pp_private")
            .exists());
        std::thread::sleep(FIELD_WRITE_DELAY);
        fake.tick();
        assert!(
            !fake.paths.field().exists(),
            "a password field is never read"
        );
        let status = fake.req(Req::Field);
        assert_eq!(status["field"], Value::Null);
        // Focus moves to a normal field: the marker goes, the words show.
        fake.event(Event::Activate);
        fake.event(Event::Done);
        assert!(!fake.paths.sensitive_field().exists());
        fake.say("my password is safe");
        assert!(fake.paths.live_in_field().exists());
    }

    #[test]
    fn text_with_newlines_is_not_committed_and_the_preedit_goes_first() {
        let mut fake = Fake::new("core-newline");
        fake.begin(Some(fake.live_file()));
        fake.focus();
        fake.say("line one line two");
        fake.log();
        assert_eq!(fake.commit(TAKE, "Line one.\nLine two."), "not_committable");
        assert_eq!(fake.log(), ["preedit:", "commit:1"]);
        let long = "a".repeat(MAX_STRING_BYTES + 1);
        fake.begin(None);
        assert_eq!(fake.commit(TAKE, &long), "not_committable");
    }

    #[test]
    fn clear_removes_the_preedit_and_stops_the_live_words() {
        let mut fake = Fake::new("core-clear");
        fake.begin(Some(fake.live_file()));
        fake.focus();
        fake.say("hello");
        fake.log();
        let (_, flush) = fake.core.request(Req::Clear, &mut fake.sink);
        assert!(flush, "answered once the compositor has it");
        assert_eq!(fake.log(), ["preedit:", "commit:1"]);
        assert!(!fake.paths.live_in_field().exists());
        fake.say("hello there");
        assert!(fake.log().is_empty(), "no longer following");
        // The take can still be committed.
        assert_eq!(fake.commit(TAKE, "Hello there."), "committed");
    }

    #[test]
    fn a_cancelled_take_never_commits() {
        let mut fake = Fake::new("core-stale");
        fake.begin(None);
        fake.focus();
        fake.req(Req::Release {
            take: Some(TAKE.into()),
        });
        assert_eq!(fake.commit(TAKE, "late text"), "stale");
        assert!(!fake.log().iter().any(|l| l.starts_with("commit_string")));
        let r = fake.req(Req::Begin {
            take: TAKE.into(),
            live: None,
            format: Format::Text,
        });
        assert_eq!(r["ok"], false);
    }

    #[test]
    fn an_earlier_take_lands_under_the_live_words_of_the_next() {
        let mut fake = Fake::new("core-queue");
        fake.begin(Some(fake.live_file()));
        fake.focus();
        fake.say("");
        fake.req(Req::Begin {
            take: "1002".into(),
            live: Some(fake.live_file()),
            format: Format::Text,
        });
        fake.say("second take");
        fake.log();
        assert_eq!(fake.commit(TAKE, "First take."), "committed");
        assert_eq!(
            fake.log(),
            [
                "preedit:",
                "commit_string:First take.",
                "commit:1",
                "preedit:second take",
                "commit:1"
            ]
        );
        assert_eq!(fake.core.take.as_ref().unwrap().id, "1002");
    }

    #[test]
    fn the_field_is_read_on_request_without_a_take() {
        let mut fake = Fake::new("core-read");
        fake.req(Req::Read);
        assert_eq!(fake.log(), ["bind:true"]);
        fake.event(Event::Activate);
        fake.event(Event::Surrounding {
            text: "Dear Sam".into(),
            cursor: 8,
        });
        fake.event(Event::Done);
        let r = fake.req(Req::Field);
        assert_eq!(r["field"], json!({"before": "Dear Sam", "after": ""}));
        assert_eq!(
            fake.log(),
            ["commit:1"],
            "asked, for a field that reports only then"
        );
        // The file follows once the burst settles.
        assert!(!fake.paths.field().exists());
        std::thread::sleep(FIELD_WRITE_DELAY);
        fake.tick();
        let body: Value =
            serde_json::from_str(&fs::read_to_string(fake.paths.field()).unwrap()).unwrap();
        assert_eq!(body, json!({"before": "Dear Sam", "after": ""}));
        // Focus leaves: released, file gone at once, nothing left to do.
        fake.event(Event::Deactivate);
        fake.event(Event::Done);
        assert!(!fake.paths.field().exists());
        assert!(fake.core.should_exit());
    }

    #[test]
    fn a_take_nobody_ends_lets_go_once_voxtype_is_idle() {
        let mut fake = Fake::new("core-stuck");
        fs::write(fake.paths.state(), "idle\n").unwrap();
        fake.begin(None);
        let t0 = Instant::now();
        fake.core.tick(t0 + STATE_POLL, &mut fake.sink);
        assert!(fake.core.take.is_some());
        fake.core.tick(t0 + STATE_POLL + TAKE_IDLE, &mut fake.sink);
        assert!(fake.core.take.is_none());
        assert!(fake.core.should_exit(), "no field was focused: released");
    }

    #[test]
    fn a_recording_take_is_kept() {
        let mut fake = Fake::new("core-recording");
        fs::write(fake.paths.state(), "recording\n").unwrap();
        fake.begin(None);
        let t0 = Instant::now();
        fake.core.tick(t0 + STATE_POLL, &mut fake.sink);
        fake.core
            .tick(t0 + STATE_POLL + TAKE_IDLE + STATE_POLL, &mut fake.sink);
        assert!(fake.core.take.is_some());
    }

    #[test]
    fn the_live_words_stop_when_voxtype_stops_listening() {
        let mut fake = Fake::new("core-freeze");
        fake.begin(Some(fake.live_file()));
        fake.focus();
        fs::write(fake.paths.state(), "streaming\n").unwrap();
        fake.say("hello wold");
        fs::write(fake.paths.state(), "transcribing\n").unwrap();
        fake.say("hello wold, the raw transcript for the pill");
        let log = fake.log();
        assert!(log.contains(&"preedit:hello wold".to_string()));
        assert!(!log.iter().any(|l| l.contains("pill")), "{log:?}");
        assert!(fake.paths.live_in_field().exists(), "the last words stay");
        assert_eq!(fake.commit(TAKE, "Hello, world."), "committed");
    }

    #[test]
    fn shutdown_removes_every_file() {
        let mut fake = Fake::new("core-shutdown");
        fake.begin(Some(fake.live_file()));
        fake.focus();
        fake.say("hello");
        assert!(fake.paths.live_in_field().exists());
        fake.core.shutdown(&mut fake.sink);
        assert!(!fake.paths.live_in_field().exists());
        assert!(!fake.paths.field().exists());
        assert!(fake.log().ends_with(&["unbind".to_string()]));
    }
}
