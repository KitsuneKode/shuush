//! The helper end to end: its event loop, its socket and the client, against
//! a fake compositor in this process. Nothing here talks to a real one.

use crate::client::{self, ClientError};
use crate::core::Core;
use crate::instance::{self, Start};
use crate::live::Format;
use crate::protocol::Req;
use crate::publish::tests::paths;
use crate::publish::Paths;
use crate::server::{self, fake, Backend};
use crate::state::Event;
use serde_json::{json, Value};
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

struct Helper {
    paths: Paths,
    compositor: fake::Compositor,
    sink: fake::Shared,
    stop: UnixStream,
    thread: Option<JoinHandle<std::io::Result<()>>>,
}

impl Helper {
    /// `serve` as main runs it, with the fake compositor.
    fn start(paths: Paths, initial: Option<Req>) -> Self {
        let Start::Server(inst) = instance::start(&paths, initial.as_ref()).unwrap() else {
            panic!("a helper is already running");
        };
        let (mut backend, compositor, sink) = fake::backend(Core::new(paths.clone()));
        let (stop_rx, stop) = UnixStream::pair().unwrap();
        stop_rx.set_nonblocking(true).unwrap();
        if let Some(req) = initial {
            let (answer, _) = backend.with(|core, sink| core.request(req, sink));
            assert_eq!(answer["ok"], true, "{answer}");
        }
        let thread = std::thread::spawn(move || {
            let result = server::run(&mut backend, &inst.listener, &stop_rx);
            drop(inst);
            result
        });
        Self {
            paths,
            compositor,
            sink,
            stop,
            thread: Some(thread),
        }
    }

    fn req(&self, req: Req) -> Result<Value, ClientError> {
        client::request(&self.paths, &req)
    }

    fn log(&self) -> Vec<String> {
        std::mem::take(&mut self.sink.0.lock().unwrap().log)
    }

    fn bound(&self) -> bool {
        self.sink.0.lock().unwrap().bound
    }

    /// Wait for the helper to exit by itself.
    fn exited(&mut self) -> bool {
        let until = Instant::now() + Duration::from_secs(3);
        while Instant::now() < until {
            if self.thread.as_ref().is_some_and(|t| t.is_finished()) {
                self.thread.take().unwrap().join().unwrap().unwrap();
                return true;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        false
    }

    fn wait_for(&self, what: impl Fn() -> bool) -> bool {
        let until = Instant::now() + Duration::from_secs(3);
        while Instant::now() < until {
            if what() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        false
    }
}

impl Drop for Helper {
    fn drop(&mut self) {
        let _ = (&self.stop).write(&[1]);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

fn begin(take: &str, live: Option<std::path::PathBuf>) -> Req {
    Req::Begin {
        take: take.into(),
        live,
        format: Format::Text,
    }
}

fn commit(take: &str, text: &str) -> Req {
    Req::Commit {
        take: take.into(),
        text: text.into(),
    }
}

#[test]
fn a_take_binds_shows_commits_and_the_helper_leaves_with_the_field() {
    let name = "e2e-take";
    let rt = paths(name);
    std::fs::write(rt.live_text(), "").unwrap();
    let mut helper = Helper::start(rt.clone(), Some(begin("7", Some(rt.live_text()))));
    assert!(helper.bound(), "bound for the take");
    helper.compositor.focus();
    std::fs::write(rt.live_text(), "hello wold").unwrap();
    assert!(
        helper.wait_for(|| rt.live_in_field().exists()),
        "words in the field"
    );
    let answer = helper.req(commit("7", "Hello, world.")).unwrap();
    assert_eq!(answer["outcome"], "committed");
    assert!(!rt.live_in_field().exists());
    let log = helper.log();
    assert_eq!(log[0], "bind:true");
    assert!(log.contains(&"preedit:hello wold".to_string()), "{log:?}");
    assert!(log.ends_with(&[
        "preedit:".to_string(),
        "commit_string:Hello, world.".to_string(),
        "commit:1".to_string()
    ]));
    // Lingering on the field: its text is published as the user edits.
    helper.compositor.send(Event::Surrounding {
        text: "Hello, world. Bye".into(),
        cursor: 17,
    });
    helper.compositor.send(Event::Done);
    assert!(
        helper.wait_for(|| rt.field().exists()),
        "field_text.json written"
    );
    let field: Value = serde_json::from_str(&std::fs::read_to_string(rt.field()).unwrap()).unwrap();
    assert_eq!(field, json!({"before": "Hello, world. Bye", "after": ""}));
    // Focus leaves the field: nothing left to do, the helper exits.
    helper.compositor.send(Event::Deactivate);
    helper.compositor.send(Event::Done);
    assert!(helper.exited(), "exits when idle");
    assert!(!helper.bound(), "the input method is let go");
    assert!(!rt.field().exists() && !rt.socket().exists());
    assert!(matches!(
        helper.req(Req::Status),
        Err(ClientError::NoHelper)
    ));
}

#[test]
fn a_second_serve_hands_its_take_to_the_running_helper() {
    let name = "e2e-second";
    let helper = Helper::start(paths(name), Some(begin("8", None)));
    let Start::Forwarded(answer) = instance::start(&helper.paths, Some(&begin("9", None))).unwrap()
    else {
        panic!("became a second helper");
    };
    assert_eq!(answer["ok"], true);
    let status = helper.req(Req::Status).unwrap();
    assert_eq!(status["take"], "9");
}

#[test]
fn a_cancelled_take_lets_go_at_once() {
    let mut helper = Helper::start(paths("e2e-release"), Some(begin("10", None)));
    helper.compositor.focus();
    helper
        .req(Req::Release {
            take: Some("10".into()),
        })
        .unwrap();
    assert!(helper.exited());
    assert!(helper.log().ends_with(&["unbind".to_string()]));
}

#[test]
fn no_text_field_means_the_caller_types_and_the_helper_leaves() {
    let mut helper = Helper::start(paths("e2e-not-active"), Some(begin("11", None)));
    let answer = helper.req(commit("11", "Hello.")).unwrap();
    assert_eq!(answer["outcome"], "not_active");
    assert!(helper.exited());
}

#[test]
fn the_field_is_read_on_request() {
    let mut helper = Helper::start(paths("e2e-read"), Some(Req::Read));
    helper.compositor.send(Event::Activate);
    helper.compositor.send(Event::Surrounding {
        text: "Dear Sam, ".into(),
        cursor: 10,
    });
    helper.compositor.send(Event::Done);
    let answer = helper.req(Req::Field).unwrap();
    assert_eq!(
        answer["field"],
        json!({"before": "Dear Sam, ", "after": ""})
    );
    helper.req(Req::End { take: None }).unwrap();
    let status = helper.req(Req::Status).unwrap();
    assert_eq!(status["lingering"], true, "{status}");
    helper.compositor.send(Event::Deactivate);
    helper.compositor.send(Event::Done);
    assert!(helper.exited());
}

#[test]
fn a_password_field_publishes_sensitive_field_and_never_the_words() {
    let name = "e2e-sensitive";
    let rt = paths(name);
    std::fs::write(rt.live_text(), "").unwrap();
    let helper = Helper::start(rt.clone(), Some(begin("12", Some(rt.live_text()))));
    helper.compositor.send(Event::Activate);
    helper.compositor.send(Event::ContentHidden(true));
    helper.compositor.send(Event::Done);
    std::fs::write(rt.live_text(), "the secret words").unwrap();
    assert!(helper.wait_for(|| rt.sensitive_field().exists()));
    std::thread::sleep(Duration::from_millis(150));
    assert!(!helper.log().iter().any(|l| l.contains("secret")));
    assert!(!rt.live_in_field().exists());
    let status = helper.req(Req::Status).unwrap();
    assert_eq!(status["sensitive"], true);
}

#[test]
fn a_stop_signal_releases_and_removes_every_file() {
    let name = "e2e-stop";
    let rt = paths(name);
    std::fs::write(rt.live_text(), "").unwrap();
    let mut helper = Helper::start(rt.clone(), Some(begin("13", Some(rt.live_text()))));
    helper.compositor.focus();
    std::fs::write(rt.live_text(), "hello").unwrap();
    assert!(helper.wait_for(|| rt.live_in_field().exists()));
    (&helper.stop).write_all(&[1]).unwrap();
    assert!(helper.exited());
    assert!(!rt.live_in_field().exists() && !rt.socket().exists());
    let log = helper.log();
    assert!(
        log.ends_with(&["preedit:".into(), "commit:1".into(), "unbind".into()]),
        "{log:?}"
    );
}

#[test]
fn bad_requests_are_answered_and_change_nothing() {
    let helper = Helper::start(paths("e2e-bad"), Some(begin("14", None)));
    let mut s = UnixStream::connect(helper.paths.socket()).unwrap();
    s.write_all(b"{\"cmd\":\"commit\",\"take\":\"$(reboot)\"}\nhi")
        .unwrap();
    s.shutdown(std::net::Shutdown::Write).unwrap();
    let mut answer = String::new();
    std::io::Read::read_to_string(&mut s, &mut answer).unwrap();
    let answer: Value = serde_json::from_str(answer.trim()).unwrap();
    assert_eq!(answer["ok"], false);
    assert_eq!(helper.req(Req::Status).unwrap()["take"], "14");
}

#[test]
fn without_a_helper_every_client_says_so() {
    let rt = paths("e2e-none");
    for req in [Req::Status, Req::Clear, commit("1", "x"), Req::Field] {
        assert!(matches!(
            client::request(&rt, &req),
            Err(ClientError::NoHelper)
        ));
    }
}
