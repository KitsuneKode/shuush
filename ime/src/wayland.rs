//! The Wayland side: one connection, an input method object only while
//! [`Core`] wants one.
//!
//! Moved from voxtype's `src/output/preedit/wayland.rs`; the daemon thread and
//! its command channel became the helper's own event loop
//! ([`crate::server::run`]).

use crate::core::Core;
use crate::driver::Sink;
use crate::server::Backend;
use crate::state::{Event, Request};
use std::io;
use std::os::fd::{AsRawFd, RawFd};
use wayland_client::backend::ReadEventsGuard;
use wayland_client::globals::{registry_queue_init, GlobalListContents};
use wayland_client::protocol::{wl_registry, wl_seat::WlSeat};
use wayland_client::{delegate_noop, Connection, Dispatch, EventQueue, Proxy, QueueHandle, WEnum};
use wayland_protocols::wp::text_input::zv3::client::zwp_text_input_v3::{
    ContentHint, ContentPurpose,
};
use wayland_protocols_misc::zwp_input_method_v2::client::zwp_input_method_manager_v2::ZwpInputMethodManagerV2;
use wayland_protocols_misc::zwp_input_method_v2::client::zwp_input_method_v2::{
    self, ZwpInputMethodV2,
};

pub const MANAGER_INTERFACE: &str = "zwp_input_method_manager_v2";

struct App {
    core: Core,
    sink: WlSink,
}

struct WlSink {
    manager: ZwpInputMethodManagerV2,
    seat: WlSeat,
    qh: QueueHandle<App>,
    im: Option<ZwpInputMethodV2>,
}

impl Sink for WlSink {
    fn bind(&mut self) -> bool {
        self.im = Some(self.manager.get_input_method(&self.seat, &self.qh, ()));
        true
    }

    fn unbind(&mut self) {
        if let Some(im) = self.im.take() {
            im.destroy();
        }
    }

    fn send(&mut self, requests: Vec<Request>) {
        let Some(im) = self.im.as_ref() else {
            return;
        };
        for request in requests {
            match request {
                Request::Preedit(text) => {
                    let end = text.len() as i32;
                    im.set_preedit_string(text, end, end);
                }
                Request::CommitString(text) => im.commit_string(text),
                Request::DeleteBefore(len) => im.delete_surrounding_text(len, 0),
                Request::Commit(serial) => im.commit(serial),
            }
        }
    }
}

impl Dispatch<ZwpInputMethodV2, ()> for App {
    fn event(
        app: &mut Self,
        proxy: &ZwpInputMethodV2,
        event: zwp_input_method_v2::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        // Events still queued for the object of an earlier take.
        if app.sink.im.as_ref().map(Proxy::id) != Some(proxy.id()) {
            return;
        }
        let event = match event {
            zwp_input_method_v2::Event::Activate => Event::Activate,
            zwp_input_method_v2::Event::Deactivate => Event::Deactivate,
            zwp_input_method_v2::Event::ContentType { hint, purpose } => {
                Event::ContentHidden(secret(hint, purpose))
            }
            zwp_input_method_v2::Event::SurroundingText { text, cursor, .. } => {
                Event::Surrounding { text, cursor }
            }
            zwp_input_method_v2::Event::Done => Event::Done,
            zwp_input_method_v2::Event::Unavailable => Event::Unavailable,
            _ => return,
        };
        app.core.event(event, &mut app.sink);
    }
}

/// A field whose text must not be shown or read: password and PIN purposes,
/// and the sensitive-data and hidden-text hints.
fn secret(hint: WEnum<ContentHint>, purpose: WEnum<ContentPurpose>) -> bool {
    let secret_purpose = matches!(
        purpose,
        WEnum::Value(ContentPurpose::Password | ContentPurpose::Pin)
    );
    let secret_hint = matches!(
        hint,
        WEnum::Value(h) if h.intersects(ContentHint::SensitiveData | ContentHint::HiddenText)
    );
    secret_purpose || secret_hint
}

impl Dispatch<wl_registry::WlRegistry, GlobalListContents> for App {
    fn event(
        _: &mut Self,
        _: &wl_registry::WlRegistry,
        _: wl_registry::Event,
        _: &GlobalListContents,
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

delegate_noop!(App: ignore WlSeat);
delegate_noop!(App: ZwpInputMethodManagerV2);

/// Why the helper cannot talk to the compositor.
#[derive(Debug)]
pub enum ConnectError {
    /// No Wayland connection (not in a session, or the socket is gone).
    NoWayland(String),
    /// The compositor lacks input-method-v2 or a seat.
    Unsupported,
}

pub struct WlBackend {
    queue: EventQueue<App>,
    app: App,
    guard: Option<ReadEventsGuard>,
}

/// Connect and check the compositor speaks input-method-v2. Nothing is bound
/// as an input method here: that waits for [`Sink::bind`].
pub fn connect(core: Core) -> Result<WlBackend, ConnectError> {
    let conn = Connection::connect_to_env().map_err(|e| ConnectError::NoWayland(e.to_string()))?;
    let (globals, queue) =
        registry_queue_init::<App>(&conn).map_err(|e| ConnectError::NoWayland(e.to_string()))?;
    let qh = queue.handle();
    let manager: ZwpInputMethodManagerV2 = globals
        .bind(&qh, 1..=1, ())
        .map_err(|_| ConnectError::Unsupported)?;
    let seat: WlSeat = globals
        .bind(&qh, 1..=1, ())
        .map_err(|_| ConnectError::Unsupported)?;
    Ok(WlBackend {
        queue,
        app: App {
            core,
            sink: WlSink {
                manager,
                seat,
                qh,
                im: None,
            },
        },
        guard: None,
    })
}

/// Whether the compositor offers input-method-v2, from the registry alone:
/// nothing is bound.
pub fn probe() -> Result<bool, ConnectError> {
    let conn = Connection::connect_to_env().map_err(|e| ConnectError::NoWayland(e.to_string()))?;
    let (globals, _queue) =
        registry_queue_init::<Probe>(&conn).map_err(|e| ConnectError::NoWayland(e.to_string()))?;
    Ok(globals
        .contents()
        .with_list(|list| list.iter().any(|g| g.interface == MANAGER_INTERFACE)))
}

struct Probe;

impl Dispatch<wl_registry::WlRegistry, GlobalListContents> for Probe {
    fn event(
        _: &mut Self,
        _: &wl_registry::WlRegistry,
        _: wl_registry::Event,
        _: &GlobalListContents,
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

fn io_err(e: impl std::fmt::Display) -> io::Error {
    io::Error::other(e.to_string())
}

impl Backend for WlBackend {
    fn with<R>(&mut self, f: impl FnOnce(&mut Core, &mut dyn Sink) -> R) -> R {
        f(&mut self.app.core, &mut self.app.sink)
    }

    fn dispatch_pending(&mut self) -> io::Result<()> {
        self.queue.dispatch_pending(&mut self.app).map_err(io_err)?;
        Ok(())
    }

    fn prepare_read(&mut self) -> Option<RawFd> {
        self.guard = self.queue.prepare_read();
        self.guard.as_ref().map(|g| g.connection_fd().as_raw_fd())
    }

    fn finish_read(&mut self, readable: bool) -> io::Result<()> {
        if let Some(guard) = self.guard.take() {
            if readable {
                match guard.read() {
                    Ok(_) => {}
                    Err(wayland_client::backend::WaylandError::Io(e))
                        if e.kind() == io::ErrorKind::WouldBlock => {}
                    Err(e) => return Err(io_err(e)),
                }
            }
        }
        self.dispatch_pending()
    }

    fn flush(&mut self) -> io::Result<()> {
        self.queue.flush().map_err(io_err)
    }

    fn roundtrip(&mut self) -> io::Result<()> {
        self.queue.roundtrip(&mut self.app).map_err(io_err)?;
        Ok(())
    }
}
