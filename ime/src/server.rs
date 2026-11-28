//! The helper's event loop: the compositor's connection, the control socket
//! and a stop signal, in one `poll`. It runs only while a take is live or a
//! field is being read, and returns when there is nothing left to do.

use crate::core::Core;
use crate::driver::Sink;
use crate::protocol::{Req, MAX_REQUEST_BYTES};
use serde_json::json;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, Instant};

/// A client gets this long to send its request and read the answer.
pub const CLIENT_TIMEOUT: Duration = Duration::from_secs(2);

/// What the loop needs from a compositor connection: the Wayland one, or
/// the tests' fake.
pub trait Backend {
    fn with<R>(&mut self, f: impl FnOnce(&mut Core, &mut dyn Sink) -> R) -> R;
    /// Hand events already read to the core.
    fn dispatch_pending(&mut self) -> io::Result<()>;
    /// The descriptor to poll for events, if any.
    fn prepare_read(&mut self) -> Option<RawFd>;
    /// After the poll: read events when there are some, then dispatch them.
    fn finish_read(&mut self, readable: bool) -> io::Result<()>;
    fn flush(&mut self) -> io::Result<()>;
    /// Send everything and wait until the compositor has processed it.
    fn roundtrip(&mut self) -> io::Result<()>;
}

static STOP_FD: AtomicI32 = AtomicI32::new(-1);

extern "C" fn on_signal(_: libc::c_int) {
    let fd = STOP_FD.load(Ordering::Relaxed);
    if fd >= 0 {
        // SAFETY: write(2) is async-signal-safe; the buffer is a valid byte.
        unsafe {
            libc::write(fd, [1u8].as_ptr().cast(), 1);
        }
    }
}

/// SIGTERM (the parent died: `setpriv --pdeathsig`), SIGINT and SIGHUP end
/// the loop cleanly, through a socket the loop polls.
pub fn stop_on_signals() -> io::Result<UnixStream> {
    let (read, write) = UnixStream::pair()?;
    read.set_nonblocking(true)?;
    write.set_nonblocking(true)?;
    STOP_FD.store(
        std::os::fd::IntoRawFd::into_raw_fd(write),
        Ordering::Relaxed,
    );
    for sig in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
        // SAFETY: a zeroed sigaction with a plain handler is valid; the
        // handler only calls write(2).
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = on_signal as *const () as libc::sighandler_t;
            libc::sigemptyset(&mut action.sa_mask);
            if libc::sigaction(sig, &action, std::ptr::null_mut()) != 0 {
                return Err(io::Error::last_os_error());
            }
        }
    }
    Ok(read)
}

fn pollfd(fd: RawFd) -> libc::pollfd {
    libc::pollfd {
        fd,
        events: libc::POLLIN,
        revents: 0,
    }
}

/// Run until nothing is left to do or a stop arrives; then release the input
/// method and remove every file.
pub fn run<B: Backend>(
    backend: &mut B,
    listener: &UnixListener,
    stop: &UnixStream,
) -> io::Result<()> {
    listener.set_nonblocking(true)?;
    let result = serve(backend, listener, stop);
    backend.with(|core, sink| core.shutdown(sink));
    let _ = backend.roundtrip();
    result
}

fn serve<B: Backend>(
    backend: &mut B,
    listener: &UnixListener,
    stop: &UnixStream,
) -> io::Result<()> {
    loop {
        // Events read at the end of the last pass first: a request sent
        // before them would carry an out-of-date serial and be ignored.
        backend.dispatch_pending()?;
        let now = Instant::now();
        backend.with(|core, sink| core.tick(now, sink));
        if backend.with(|core, _| core.should_exit()) {
            return Ok(());
        }
        backend.flush()?;
        let timeout = backend.with(|core, _| core.deadline(now)).map_or(-1, |t| {
            t.saturating_duration_since(Instant::now())
                .as_millis()
                .min(i32::MAX as u128) as i32
        });
        let wayland = backend.prepare_read();
        let mut fds = vec![pollfd(listener.as_raw_fd()), pollfd(stop.as_raw_fd())];
        if let Some(fd) = wayland {
            fds.push(pollfd(fd));
        }
        // SAFETY: `fds` is a valid array of pollfd for the call's duration.
        let n = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, timeout) };
        if n < 0 {
            let err = io::Error::last_os_error();
            backend.finish_read(false)?;
            if err.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(err);
        }
        let wayland_ready = fds.get(2).is_some_and(|p| p.revents != 0);
        backend.finish_read(wayland_ready)?;
        if fds[1].revents != 0 {
            return Ok(());
        }
        if fds[0].revents != 0 {
            loop {
                match listener.accept() {
                    Ok((stream, _)) => handle(backend, stream)?,
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                    Err(e) => return Err(e),
                }
            }
        }
    }
}

/// The connecting process runs as this user (the socket's directory is
/// private too; this is the second lock).
fn same_user(stream: &UnixStream) -> bool {
    let mut cred: libc::ucred = libc::ucred {
        pid: 0,
        uid: u32::MAX,
        gid: u32::MAX,
    };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: `cred` and `len` are valid for writes of the sizes given.
    let rc = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut cred as *mut libc::ucred).cast(),
            &mut len,
        )
    };
    // SAFETY: getuid has no preconditions.
    rc == 0 && cred.uid == unsafe { libc::getuid() }
}

fn read_request(stream: &mut UnixStream) -> io::Result<Vec<u8>> {
    let mut raw = Vec::new();
    stream
        .take(MAX_REQUEST_BYTES as u64 + 1)
        .read_to_end(&mut raw)?;
    if raw.len() > MAX_REQUEST_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "request too long",
        ));
    }
    Ok(raw)
}

fn handle<B: Backend>(backend: &mut B, mut stream: UnixStream) -> io::Result<()> {
    if !same_user(&stream) {
        return Ok(());
    }
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(CLIENT_TIMEOUT));
    let _ = stream.set_write_timeout(Some(CLIENT_TIMEOUT));
    let req = match read_request(&mut stream) {
        Ok(raw) => Req::decode(&raw),
        // A client that went away or never finished: nothing to answer.
        Err(_) => return Ok(()),
    };
    let req = match req {
        Ok(req) => req,
        Err(error) => {
            let _ = writeln!(stream, "{}", json!({"ok": false, "error": error}));
            return Ok(());
        }
    };
    backend.dispatch_pending()?;
    let (reply, wait) = backend.with(|core, sink| core.request(req, sink));
    // Answer even if the connection drops now: a commit may have landed,
    // and "unavailable" would type it a second time.
    let sent = if wait {
        backend.roundtrip()
    } else {
        backend.flush()
    };
    let _ = writeln!(stream, "{reply}");
    sent
}

#[cfg(test)]
pub mod fake {
    //! A compositor in the test process: a recording sink and events the
    //! test injects through a channel, with a socket to wake the poll.
    use super::*;
    use crate::driver::tests::RecSink;
    use crate::state::Event;
    use std::sync::mpsc::{Receiver, Sender};
    use std::sync::{Arc, Mutex};

    pub struct FakeBackend {
        pub core: Core,
        pub sink: Shared,
        events: Receiver<Event>,
        wake: UnixStream,
    }

    /// The sink's log, readable from the test thread.
    #[derive(Clone, Default)]
    pub struct Shared(pub Arc<Mutex<RecSink>>);

    impl Sink for Shared {
        fn bind(&mut self) -> bool {
            self.0.lock().unwrap().bind()
        }
        fn unbind(&mut self) {
            self.0.lock().unwrap().unbind()
        }
        fn send(&mut self, requests: Vec<crate::state::Request>) {
            self.0.lock().unwrap().send(requests)
        }
    }

    /// Hands events to the fake compositor's loop.
    #[derive(Clone)]
    pub struct Compositor {
        tx: Sender<Event>,
        wake: Arc<UnixStream>,
    }

    impl Compositor {
        pub fn send(&self, event: Event) {
            self.tx.send(event).unwrap();
            let _ = (&*self.wake).write(&[1]);
        }
        pub fn focus(&self) {
            self.send(Event::Activate);
            self.send(Event::Done);
        }
    }

    pub fn backend(core: Core) -> (FakeBackend, Compositor, Shared) {
        let (tx, rx) = std::sync::mpsc::channel();
        let (wake_rx, wake_tx) = UnixStream::pair().unwrap();
        wake_rx.set_nonblocking(true).unwrap();
        let sink = Shared::default();
        (
            FakeBackend {
                core,
                sink: sink.clone(),
                events: rx,
                wake: wake_rx,
            },
            Compositor {
                tx,
                wake: Arc::new(wake_tx),
            },
            sink,
        )
    }

    impl Backend for FakeBackend {
        fn with<R>(&mut self, f: impl FnOnce(&mut Core, &mut dyn Sink) -> R) -> R {
            f(&mut self.core, &mut self.sink)
        }
        fn dispatch_pending(&mut self) -> io::Result<()> {
            while let Ok(event) = self.events.try_recv() {
                self.core.event(event, &mut self.sink);
            }
            Ok(())
        }
        fn prepare_read(&mut self) -> Option<RawFd> {
            Some(self.wake.as_raw_fd())
        }
        fn finish_read(&mut self, readable: bool) -> io::Result<()> {
            if readable {
                let mut buf = [0u8; 64];
                while matches!((&self.wake).read(&mut buf), Ok(n) if n > 0) {}
            }
            self.dispatch_pending()
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
        fn roundtrip(&mut self) -> io::Result<()> {
            self.dispatch_pending()
        }
    }
}
