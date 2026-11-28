//! The real binary against a private compositor: a Wayland server in this
//! test that offers a seat and input-method-v2 and records every request.
//! It listens on a socket in a temporary runtime directory, so nothing here
//! reaches the desktop the tests run in.

use std::collections::HashMap;
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use wayland_protocols::wp::text_input::zv3::server::zwp_text_input_v3::{
    ContentHint, ContentPurpose,
};
use wayland_protocols_misc::zwp_input_method_v2::server::zwp_input_method_manager_v2::{
    self, ZwpInputMethodManagerV2,
};
use wayland_protocols_misc::zwp_input_method_v2::server::zwp_input_method_v2::{
    self, ZwpInputMethodV2,
};
use wayland_server::backend::{ClientData, ClientId, DisconnectReason};
use wayland_server::protocol::wl_seat::{self, WlSeat};
use wayland_server::{
    Client, DataInit, Dispatch, Display, DisplayHandle, GlobalDispatch, ListeningSocket, New,
};

type Log = Arc<Mutex<Vec<String>>>;

struct State {
    log: Log,
    ims: Vec<ZwpInputMethodV2>,
}

struct Peer;
impl ClientData for Peer {
    fn initialized(&self, _: ClientId) {}
    fn disconnected(&self, _: ClientId, _: DisconnectReason) {}
}

impl GlobalDispatch<WlSeat, ()> for State {
    fn bind(
        _: &mut Self,
        _: &DisplayHandle,
        _: &Client,
        resource: New<WlSeat>,
        _: &(),
        data_init: &mut DataInit<'_, Self>,
    ) {
        data_init.init(resource, ());
    }
}

impl Dispatch<WlSeat, ()> for State {
    fn request(
        _: &mut Self,
        _: &Client,
        _: &WlSeat,
        _: wl_seat::Request,
        _: &(),
        _: &DisplayHandle,
        _: &mut DataInit<'_, Self>,
    ) {
    }
}

impl GlobalDispatch<ZwpInputMethodManagerV2, ()> for State {
    fn bind(
        _: &mut Self,
        _: &DisplayHandle,
        _: &Client,
        resource: New<ZwpInputMethodManagerV2>,
        _: &(),
        data_init: &mut DataInit<'_, Self>,
    ) {
        data_init.init(resource, ());
    }
}

impl Dispatch<ZwpInputMethodManagerV2, ()> for State {
    fn request(
        state: &mut Self,
        _: &Client,
        _: &ZwpInputMethodManagerV2,
        request: zwp_input_method_manager_v2::Request,
        _: &(),
        _: &DisplayHandle,
        data_init: &mut DataInit<'_, Self>,
    ) {
        if let zwp_input_method_manager_v2::Request::GetInputMethod { input_method, .. } = request {
            state.ims.push(data_init.init(input_method, ()));
            state.log.lock().unwrap().push("bind".into());
        }
    }
}

impl Dispatch<ZwpInputMethodV2, ()> for State {
    fn request(
        state: &mut Self,
        _: &Client,
        im: &ZwpInputMethodV2,
        request: zwp_input_method_v2::Request,
        _: &(),
        _: &DisplayHandle,
        _: &mut DataInit<'_, Self>,
    ) {
        use zwp_input_method_v2::Request as R;
        let line = match request {
            R::SetPreeditString { text, .. } => format!("preedit:{text}"),
            R::CommitString { text } => format!("commit_string:{text}"),
            R::DeleteSurroundingText { before_length, .. } => format!("delete:{before_length}"),
            R::Commit { serial } => format!("commit:{serial}"),
            R::Destroy => {
                state.ims.retain(|i| i != im);
                "unbind".into()
            }
            other => format!("other:{other:?}"),
        };
        state.log.lock().unwrap().push(line);
    }
}

/// What the test asks the compositor to do to every bound input method.
enum Do {
    Focus { text: String, hidden: bool },
    Unfocus,
    Unavailable,
}

struct Compositor {
    log: Log,
    tx: Sender<Do>,
    stop: Arc<Mutex<bool>>,
    rt: PathBuf,
    display: PathBuf,
}

impl Compositor {
    fn start(name: &str) -> Self {
        Self::start_with(name, true)
    }

    fn start_with(name: &str, input_method: bool) -> Self {
        let rt = std::env::temp_dir().join(format!("shuush-ime-wl-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&rt);
        std::fs::create_dir_all(rt.join("voxtype")).unwrap();
        let display = rt.join("wayland-private");
        let log: Log = Arc::default();
        let stop = Arc::new(Mutex::new(false));
        let (tx, rx) = channel();
        let socket = ListeningSocket::bind_absolute(display.clone()).unwrap();
        let (thread_log, thread_stop) = (log.clone(), stop.clone());
        std::thread::spawn(move || serve(socket, rx, thread_log, thread_stop, input_method));
        Self {
            log,
            tx,
            stop,
            rt,
            display,
        }
    }

    fn cmd(&self, args: &[&str]) -> Command {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_shuush-ime"));
        cmd.args(args)
            .env_remove("WAYLAND_SOCKET")
            .env("WAYLAND_DISPLAY", &self.display)
            .env("XDG_RUNTIME_DIR", &self.rt)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        cmd
    }

    fn run(&self, args: &[&str], stdin: &str) -> (i32, String) {
        let mut child = self.cmd(args).stdin(Stdio::piped()).spawn().unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(stdin.as_bytes())
            .unwrap();
        let out = child.wait_with_output().unwrap();
        (
            out.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&out.stdout).into_owned(),
        )
    }

    fn serve(&self, args: &[&str]) -> Child {
        let mut all = vec!["serve"];
        all.extend_from_slice(args);
        let child = self.cmd(&all).spawn().unwrap();
        assert!(
            self.wait(|| self.rt.join("shuush/ime.sock").exists()),
            "socket"
        );
        child
    }

    fn log(&self) -> Vec<String> {
        std::mem::take(&mut self.log.lock().unwrap())
    }

    fn logged(&self, line: &str) -> bool {
        self.wait(|| self.log.lock().unwrap().iter().any(|l| l == line))
    }

    fn wait(&self, what: impl Fn() -> bool) -> bool {
        let until = Instant::now() + Duration::from_secs(5);
        while Instant::now() < until {
            if what() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        false
    }

    fn file(&self, name: &str) -> PathBuf {
        self.rt.join("voxtype").join(name)
    }

    fn say(&self, words: &str) {
        let path = self.file("live_text");
        let tmp = path.with_extension("tmp");
        std::fs::write(&tmp, words).unwrap();
        std::fs::rename(tmp, path).unwrap();
    }
}

impl Drop for Compositor {
    fn drop(&mut self) {
        *self.stop.lock().unwrap() = true;
    }
}

fn exits(child: &mut Child) -> Option<i32> {
    let until = Instant::now() + Duration::from_secs(5);
    while Instant::now() < until {
        if let Some(status) = child.try_wait().unwrap() {
            return status.code();
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let _ = child.kill();
    None
}

fn serve(
    socket: ListeningSocket,
    rx: Receiver<Do>,
    log: Log,
    stop: Arc<Mutex<bool>>,
    input_method: bool,
) {
    let mut display: Display<State> = Display::new().unwrap();
    let dh = display.handle();
    dh.create_global::<State, WlSeat, ()>(7, ());
    if input_method {
        dh.create_global::<State, ZwpInputMethodManagerV2, ()>(1, ());
    }
    let mut state = State {
        log,
        ims: Vec::new(),
    };
    let mut clients = HashMap::new();
    while !*stop.lock().unwrap() {
        if let Ok(Some(stream)) = socket.accept() {
            let stream: UnixStream = stream;
            let id = display
                .handle()
                .insert_client(stream, Arc::new(Peer))
                .unwrap()
                .id();
            clients.insert(id, ());
        }
        display.dispatch_clients(&mut state).unwrap();
        while let Ok(what) = rx.try_recv() {
            for im in &state.ims {
                match &what {
                    Do::Focus { text, hidden } => {
                        im.activate();
                        if *hidden {
                            im.content_type(ContentHint::SensitiveData, ContentPurpose::Password);
                        }
                        im.surrounding_text(text.clone(), text.len() as u32, text.len() as u32);
                        im.done();
                    }
                    Do::Unfocus => {
                        im.deactivate();
                        im.done();
                    }
                    Do::Unavailable => im.unavailable(),
                }
            }
        }
        display.flush_clients().unwrap();
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn focus(c: &Compositor, text: &str, hidden: bool) {
    c.tx.send(Do::Focus {
        text: text.into(),
        hidden,
    })
    .unwrap();
}

#[test]
fn probe_reads_the_registry_and_binds_nothing() {
    let c = Compositor::start("probe");
    let (code, out) = c.run(&["probe"], "");
    assert_eq!(code, 0, "{out}");
    std::thread::sleep(Duration::from_millis(100));
    assert!(c.log().is_empty(), "no input method was made");
}

#[test]
fn a_take_over_the_real_protocol() {
    let c = Compositor::start("take");
    c.say("");
    let mut helper = c.serve(&["--take", "5", "--live", "auto"]);
    assert!(c.logged("bind"));
    focus(&c, "Dear Sam, ", false);
    c.say("hello wold");
    assert!(c.logged("preedit:hello wold"), "{:?}", c.log());
    assert!(c.wait(|| c.file("live_in_field").exists()));

    let (code, _) = c.run(&["commit", "--take", "5"], "Hello, world.");
    assert_eq!(code, 0);
    assert!(c.logged("commit_string:Hello, world."));
    assert!(!c.file("live_in_field").exists());

    let (code, out) = c.run(&["read-field", "--wait", "2000"], "");
    assert_eq!(code, 0);
    let field: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(field["before"], "Dear Sam, ");
    assert!(
        c.wait(|| c.file("field_text.json").exists()),
        "for the edit watcher"
    );

    // Focus leaves the field: nothing left to do.
    c.tx.send(Do::Unfocus).unwrap();
    assert_eq!(exits(&mut helper), Some(0));
    assert!(c.logged("unbind"));
    assert!(!c.file("field_text.json").exists());
    assert!(!c.rt.join("shuush/ime.sock").exists());
    let (code, _) = c.run(&["status"], "");
    assert_eq!(code, 3, "no helper at rest");
}

#[test]
fn a_password_field_over_the_real_protocol() {
    let c = Compositor::start("password");
    c.say("");
    std::fs::create_dir_all(c.rt.join("voxtype/takes/6")).unwrap();
    let mut helper = c.serve(&["--take", "6", "--live", "auto"]);
    assert!(c.logged("bind"));
    focus(&c, "hunter", true);
    assert!(c.wait(|| c.file("sensitive_field").exists()));
    c.say("my secret");
    std::thread::sleep(Duration::from_millis(200));
    assert!(!c.log().iter().any(|l| l.contains("secret")));
    assert!(c.file("takes/6/pp_private").exists());
    assert!(!c.file("field_text.json").exists());
    let (code, _) = c.run(&["release", "--take", "6"], "");
    assert_eq!(code, 0);
    assert_eq!(exits(&mut helper), Some(0));
    assert!(!c.file("sensitive_field").exists());
}

#[test]
fn another_input_method_on_the_seat_means_typing() {
    let c = Compositor::start("unavailable");
    let mut helper = c.serve(&["--take", "7"]);
    assert!(c.logged("bind"));
    c.tx.send(Do::Unavailable).unwrap();
    std::thread::sleep(Duration::from_millis(100));
    let (code, _) = c.run(&["commit", "--take", "7"], "Hello.");
    assert_eq!(code, 3, "the caller types it");
    assert_eq!(exits(&mut helper), Some(0));
}

#[test]
fn sigterm_from_a_dying_parent_lets_go_cleanly() {
    let c = Compositor::start("sigterm");
    c.say("");
    let mut helper = c.serve(&["--take", "8", "--live", "auto"]);
    assert!(c.logged("bind"));
    focus(&c, "", false);
    c.say("hello");
    assert!(c.logged("preedit:hello"));
    // SAFETY: kill(2) on our own child's pid.
    unsafe { libc::kill(helper.id() as i32, libc::SIGTERM) };
    assert_eq!(exits(&mut helper), Some(0));
    assert!(c.logged("unbind"));
    assert!(!c.file("live_in_field").exists());
    assert!(!c.rt.join("shuush/ime.sock").exists());
}

#[test]
fn a_compositor_without_the_protocol_is_reported() {
    let c = Compositor::start_with("no-im", false);
    let mut helper = c.cmd(&["serve", "--take", "9"]).spawn().unwrap();
    assert_eq!(exits(&mut helper), Some(5));
    let (code, _) = c.run(&["probe"], "");
    assert_eq!(code, 1);
}

#[test]
fn no_compositor_is_reported() {
    let rt = std::env::temp_dir().join(format!("shuush-ime-wl-{}-none", std::process::id()));
    let _ = std::fs::remove_dir_all(&rt);
    std::fs::create_dir_all(&rt).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_shuush-ime"))
        .args(["serve", "--take", "9"])
        .env_remove("WAYLAND_SOCKET")
        .env("WAYLAND_DISPLAY", rt.join("nothing-here"))
        .env("XDG_RUNTIME_DIR", &rt)
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(6));
}
