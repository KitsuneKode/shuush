//! Applies Shuush's commands and the compositor's events to the current
//! binding. Shared by the Wayland backend and the tests' fake one.
//!
//! Moved from voxtype's `src/output/preedit/mod.rs` (`Driver`, `Sink`); the
//! daemon's channel and oneshot replies became plain return values, because
//! the helper answers each request on its own socket.

use crate::state::{CommitOutcome, Event, Ime, Request};
use std::time::{Duration, Instant};

/// After a take, the binding stays on the same field this long so the field's
/// text can be read as the user edits it (see [`Driver::field_update`]);
/// focus leaving the field or the next take ends it sooner.
pub const LINGER: Duration = Duration::from_secs(120);

/// How often a bound field is asked for its text, so edits made after a
/// take are seen by a field that reports only when asked.
pub const FIELD_ASK: Duration = Duration::from_millis(500);

/// What a backend does with the input method object; the real one talks
/// Wayland, the test one records.
pub trait Sink {
    /// Create the input method object. False when it cannot be created.
    fn bind(&mut self) -> bool;
    /// Destroy the input method object, if any.
    fn unbind(&mut self);
    fn send(&mut self, requests: Vec<Request>);
}

/// A command from Shuush.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Cmd {
    /// Bind the input method for a take.
    Begin,
    /// Bind only to read the focused field (no take): stays [`LINGER`].
    Read,
    Set(String),
    /// Remove the preedit.
    Clear,
    /// Commit text in place of the preedit.
    Commit(String),
    /// Clear the preedit; the binding stays on the field for [`LINGER`].
    End,
    /// Clear the preedit and release the input method now (a cancelled take).
    Release,
}

/// What the event loop must do after [`Driver::command`] returns.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ack {
    None,
    /// Send the pending requests and wait for the compositor before replying.
    Flush,
    Committed(CommitOutcome),
}

/// The binding as Shuush's markers see it.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Status {
    pub bound: bool,
    /// A text-input-v3 field is focused and listens to us.
    pub active: bool,
    /// That field is a password or PIN field.
    pub sensitive: bool,
    /// It shows our preedit.
    pub showing: bool,
    /// Bound after a take ended, for field reading only.
    pub lingering: bool,
}

pub struct Driver {
    ime: Option<Ime>,
    /// Set while bound after a take ended (no preedit, field reading only).
    linger_until: Option<Instant>,
    /// The field text last handed out by [`field_update`](Self::field_update).
    field: Option<(String, String)>,
    field_dirty: bool,
    /// When to next ask the field for its text (see [`Ime::ask_field`]).
    next_ask: Option<Instant>,
}

impl Default for Driver {
    fn default() -> Self {
        Self::new()
    }
}

impl Driver {
    pub fn new() -> Self {
        Self {
            ime: None,
            linger_until: None,
            field: None,
            field_dirty: false,
            next_ask: None,
        }
    }

    pub fn status(&self) -> Status {
        let ime = self.ime.as_ref();
        Status {
            bound: ime.is_some(),
            active: ime.is_some_and(Ime::active),
            sensitive: ime.is_some_and(Ime::sensitive),
            showing: ime.is_some_and(Ime::showing),
            lingering: self.linger_until.is_some(),
        }
    }

    /// The focused field's text before and after the cursor, when readable.
    pub fn field(&self) -> Option<(&str, &str)> {
        self.ime.as_ref().and_then(Ime::field)
    }

    /// When the event loop must wake for [`tick`](Self::tick).
    pub fn wake_deadline(&self) -> Option<Instant> {
        [self.linger_until, self.next_ask]
            .into_iter()
            .flatten()
            .min()
    }

    /// When the binding kept after a take must be released.
    #[cfg(test)]
    pub fn linger_deadline(&self) -> Option<Instant> {
        self.linger_until
    }

    /// Release a binding kept after a take once its time is up.
    pub fn tick(&mut self, now: Instant, sink: &mut dyn Sink) {
        if self.linger_until.is_some_and(|t| now >= t) {
            self.release(sink);
            self.publish();
        }
        match self.ime.as_ref().filter(|ime| ime.active()) {
            Some(ime) => {
                if self.next_ask.is_none_or(|t| now >= t) {
                    sink.send(ime.ask_field());
                    self.next_ask = Some(now + FIELD_ASK);
                }
            }
            None => self.next_ask = None,
        }
    }

    /// Ask the field for its text now (a field that reports only when asked).
    pub fn ask(&mut self, sink: &mut dyn Sink) {
        if let Some(ime) = self.ime.as_ref() {
            sink.send(ime.ask_field());
        }
    }

    /// The focused field's (before, after) cursor text when it changed since
    /// the last call: `Some(None)` once it is gone (focus left, released,
    /// password field).
    pub fn field_update(&mut self) -> Option<Option<(String, String)>> {
        std::mem::take(&mut self.field_dirty).then(|| self.field.clone())
    }

    fn release(&mut self, sink: &mut dyn Sink) {
        self.linger_until = None;
        if let Some(mut ime) = self.ime.take() {
            sink.send(ime.clear());
            sink.unbind();
        }
    }

    pub fn command(&mut self, cmd: Cmd, sink: &mut dyn Sink) -> Ack {
        let ack = match cmd {
            Cmd::Begin => {
                self.linger_until = None;
                match self.ime.as_mut() {
                    // Still on the field the last take went into (or the
                    // take before is still waiting for its cleanup): keep it.
                    Some(ime) if ime.active() => sink.send(ime.clear()),
                    // A binding with no field.
                    _ => {
                        self.release(sink);
                        if sink.bind() {
                            self.ime = Some(Ime::new());
                        }
                    }
                }
                Ack::None
            }
            Cmd::Read => {
                let until = Some(Instant::now() + LINGER);
                match self.ime.as_ref() {
                    // A take is live: it reads the field already.
                    Some(_) if self.linger_until.is_none() => {}
                    Some(_) => self.linger_until = until,
                    None => {
                        if sink.bind() {
                            self.ime = Some(Ime::new());
                            self.linger_until = until;
                        }
                    }
                }
                Ack::None
            }
            Cmd::Set(text) => {
                if let Some(ime) = self.ime.as_mut() {
                    sink.send(ime.set(&text));
                }
                Ack::None
            }
            Cmd::Clear => {
                if let Some(ime) = self.ime.as_mut() {
                    sink.send(ime.clear());
                }
                Ack::Flush
            }
            Cmd::Commit(text) => {
                let outcome = match self.ime.as_mut() {
                    Some(ime) => {
                        let (outcome, requests) = ime.commit(&text);
                        sink.send(requests);
                        outcome
                    }
                    None => CommitOutcome::Unavailable,
                };
                Ack::Committed(outcome)
            }
            Cmd::End => {
                match self.ime.as_mut() {
                    // Keep reading the field the text went into.
                    Some(ime) if ime.active() && self.linger_until.is_none() => {
                        sink.send(ime.clear());
                        self.linger_until = Some(Instant::now() + LINGER);
                    }
                    Some(_) if self.linger_until.is_some() => {}
                    _ => self.release(sink),
                }
                Ack::Flush
            }
            Cmd::Release => {
                self.release(sink);
                Ack::Flush
            }
        };
        self.publish();
        ack
    }

    pub fn event(&mut self, event: Event, sink: &mut dyn Sink) {
        let Some(ime) = self.ime.as_mut() else {
            return;
        };
        match &event {
            // The field's own text never reaches a log.
            Event::Surrounding { text, cursor } => {
                crate::debug!(
                    "input method event: surrounding text ({} bytes, cursor {cursor})",
                    text.len()
                )
            }
            other => crate::debug!("input method event: {other:?}"),
        }
        let unavailable = event == Event::Unavailable;
        // Focus changes apply at `done`: a binding made to read the field
        // is not active until the first one.
        let done = event == Event::Done;
        sink.send(ime.on_event(event));
        if unavailable {
            crate::warn!("another input method holds the seat; Shuush types instead");
            sink.unbind();
            self.ime = None;
            self.linger_until = None;
        } else if done && self.linger_until.is_some() && !self.ime.as_ref().is_some_and(Ime::active)
        {
            // Focus left the field the text went into.
            self.release(sink);
        }
        self.publish();
    }

    /// Release everything (the helper is exiting).
    pub fn shutdown(&mut self, sink: &mut dyn Sink) {
        self.release(sink);
        self.publish();
    }

    fn publish(&mut self) {
        let field = self.field().map(|(b, a)| (b.to_string(), a.to_string()));
        if field != self.field {
            self.field = field;
            self.field_dirty = true;
        }
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    /// The fake input method: records what a real one would send.
    #[derive(Default)]
    pub struct RecSink {
        pub bound: bool,
        pub refuse: bool,
        pub log: Vec<String>,
    }

    impl Sink for RecSink {
        fn bind(&mut self) -> bool {
            self.bound = !self.refuse;
            self.log.push(format!("bind:{}", self.bound));
            self.bound
        }
        fn unbind(&mut self) {
            self.bound = false;
            self.log.push("unbind".to_string());
        }
        fn send(&mut self, requests: Vec<Request>) {
            for r in requests {
                self.log.push(match r {
                    Request::Preedit(t) => format!("preedit:{t}"),
                    Request::CommitString(t) => format!("commit_string:{t}"),
                    Request::DeleteBefore(n) => format!("delete_before:{n}"),
                    Request::Commit(s) => format!("commit:{s}"),
                });
            }
        }
    }

    struct Fake {
        driver: Driver,
        sink: RecSink,
    }

    impl Fake {
        fn new() -> Self {
            Self {
                driver: Driver::new(),
                sink: RecSink::default(),
            }
        }
        fn cmd(&mut self, cmd: Cmd) -> Ack {
            self.driver.command(cmd, &mut self.sink)
        }
        fn set(&mut self, text: &str) {
            self.cmd(Cmd::Set(text.to_string()));
        }
        fn commit(&mut self, text: &str) -> CommitOutcome {
            match self.cmd(Cmd::Commit(text.to_string())) {
                Ack::Committed(outcome) => outcome,
                other => panic!("expected a commit outcome, got {other:?}"),
            }
        }
        fn event(&mut self, event: Event) {
            self.driver.event(event, &mut self.sink);
        }
        fn focus(&mut self) {
            self.event(Event::Activate);
            self.event(Event::Done);
        }
        fn unfocus(&mut self) {
            self.event(Event::Deactivate);
            self.event(Event::Done);
        }
        fn log(&mut self) -> Vec<String> {
            std::mem::take(&mut self.sink.log)
        }
    }

    #[test]
    fn a_take_shows_partials_then_commits_the_final_text() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.focus();
        fake.set("hello");
        fake.set("hello wold");
        assert!(fake.driver.status().active);
        assert!(fake.driver.status().showing);
        assert_eq!(fake.commit("Hello, world."), CommitOutcome::Committed);
        fake.cmd(Cmd::End);
        assert_eq!(
            fake.log(),
            [
                "bind:true",
                "preedit:hello",
                "commit:1",
                "preedit:hello wold",
                "commit:1",
                "preedit:",
                "commit_string:Hello, world.",
                "commit:1",
            ]
        );
        // The binding stays on the field until focus leaves it.
        assert!(fake.driver.status().lingering);
        fake.unfocus();
        assert_eq!(fake.log(), ["unbind"]);
        assert!(!fake.driver.status().bound);
    }

    #[test]
    fn no_focused_text_input_reports_not_active() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.set("hello");
        assert!(!fake.driver.status().active);
        assert_eq!(fake.commit("Hello."), CommitOutcome::NotActive);
        assert!(!fake.log().iter().any(|l| l.starts_with("commit_string")));
    }

    #[test]
    fn end_clears_the_preedit_and_keeps_the_field() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.focus();
        fake.set("never mind");
        assert_eq!(fake.cmd(Cmd::End), Ack::Flush);
        assert_eq!(
            fake.log(),
            [
                "bind:true",
                "preedit:never mind",
                "commit:1",
                "preedit:",
                "commit:1",
            ]
        );
        // Kept on the field (no preedit) until focus leaves it.
        fake.unfocus();
        assert_eq!(fake.log(), ["unbind"]);
        assert!(!fake.driver.status().active);
        assert_eq!(fake.commit("x"), CommitOutcome::Unavailable);
    }

    #[test]
    fn a_cancelled_take_releases_at_once() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.focus();
        fake.set("never mind");
        fake.cmd(Cmd::Release);
        assert_eq!(
            fake.log(),
            [
                "bind:true",
                "preedit:never mind",
                "commit:1",
                "preedit:",
                "commit:1",
                "unbind"
            ]
        );
        assert_eq!(fake.driver.status(), Status::default());
    }

    #[test]
    fn end_without_a_focused_field_releases() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.cmd(Cmd::End);
        assert_eq!(fake.log(), ["bind:true", "unbind"]);
        assert!(!fake.driver.status().bound);
    }

    #[test]
    fn the_kept_binding_reports_field_edits_then_expires() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.focus();
        fake.cmd(Cmd::End);
        fake.log();
        fake.event(Event::Surrounding {
            text: "Ask Groq now".into(),
            cursor: 12,
        });
        fake.event(Event::Done);
        let Fake { driver, sink } = &mut fake;
        assert_eq!(
            driver.field_update(),
            Some(Some(("Ask Groq now".to_string(), String::new())))
        );
        assert_eq!(
            driver.field_update(),
            None,
            "an unchanged field is reported once"
        );
        let deadline = driver.linger_deadline().expect("lingering");
        driver.tick(deadline, sink);
        assert_eq!(sink.log, ["unbind"]);
        assert_eq!(
            driver.field_update(),
            Some(None),
            "released: the field is gone"
        );
        assert_eq!(driver.linger_deadline(), None);
    }

    #[test]
    fn another_input_method_makes_the_take_fall_back() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.event(Event::Unavailable);
        assert_eq!(fake.commit("Hello."), CommitOutcome::Unavailable);
        assert_eq!(fake.log(), ["bind:true", "unbind"]);
        // The next take tries again.
        fake.cmd(Cmd::Begin);
        assert_eq!(fake.log(), ["bind:true"]);
    }

    #[test]
    fn focus_loss_mid_take_follows_the_focus() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.focus();
        fake.set("hello");
        fake.unfocus();
        assert!(!fake.driver.status().active);
        fake.set("hello there");
        fake.focus();
        assert_eq!(fake.commit("Hello there."), CommitOutcome::Committed);
        let log = fake.log();
        assert_eq!(
            &log[log.len() - 5..],
            [
                "preedit:hello there",
                "commit:3",
                "preedit:",
                "commit_string:Hello there.",
                "commit:3"
            ]
        );
    }

    #[test]
    fn focus_lost_at_delivery_reports_not_active() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.focus();
        fake.set("hello");
        fake.unfocus();
        assert_eq!(fake.commit("Hello."), CommitOutcome::NotActive);
    }

    #[test]
    fn a_bind_the_compositor_refuses_leaves_nothing_bound() {
        let mut fake = Fake::new();
        fake.sink.refuse = true;
        fake.cmd(Cmd::Begin);
        assert!(!fake.driver.status().bound);
        assert_eq!(fake.commit("x"), CommitOutcome::Unavailable);
    }

    #[test]
    fn the_next_take_keeps_the_field_the_last_one_went_into() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.focus();
        fake.commit("One.");
        fake.cmd(Cmd::End);
        fake.log();
        fake.cmd(Cmd::Begin);
        assert!(!fake.driver.status().lingering);
        assert!(
            !fake.log().iter().any(|l| l.starts_with("bind")),
            "no second binding"
        );
        fake.set("two");
        assert_eq!(fake.log(), ["preedit:two", "commit:1"]);
    }

    #[test]
    fn a_read_binds_without_a_take_and_lingers() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Read);
        assert_eq!(fake.log(), ["bind:true"]);
        assert!(fake.driver.status().lingering);
        fake.event(Event::Activate);
        fake.event(Event::Surrounding {
            text: "Hi there".into(),
            cursor: 2,
        });
        fake.event(Event::Done);
        assert_eq!(fake.driver.field(), Some(("Hi", " there")));
        // Asked for its text while nothing is shown.
        let Fake { driver, sink } = &mut fake;
        driver.tick(Instant::now(), sink);
        assert_eq!(sink.log, ["commit:1"]);
        // A take while reading keeps the same binding.
        fake.log();
        fake.cmd(Cmd::Begin);
        assert!(fake.log().iter().all(|l| !l.starts_with("bind")));
        // A read during a take changes nothing.
        fake.cmd(Cmd::Read);
        assert!(!fake.driver.status().lingering);
    }

    #[test]
    fn a_read_with_no_text_field_is_released_at_the_first_done() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Read);
        fake.event(Event::Done);
        assert_eq!(fake.log(), ["bind:true", "unbind"]);
    }

    #[test]
    fn a_password_field_is_sensitive_and_never_read() {
        let mut fake = Fake::new();
        fake.cmd(Cmd::Begin);
        fake.event(Event::Activate);
        fake.event(Event::ContentHidden(true));
        fake.event(Event::Surrounding {
            text: "hunter2".into(),
            cursor: 7,
        });
        fake.event(Event::Done);
        fake.set("my secret");
        let status = fake.driver.status();
        assert!(status.sensitive && !status.showing);
        assert_eq!(fake.driver.field(), None);
        assert_eq!(fake.driver.field_update(), None, "never handed out");
        assert!(!fake.log().iter().any(|l| l.starts_with("preedit:my")));
    }
}
