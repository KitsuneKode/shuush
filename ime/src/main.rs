//! shuush-ime: Shuush's Wayland input method.
//!
//! While a take is live it binds as the seat's input method
//! (`zwp_input_method_v2`), shows the words heard so far as preedit in the
//! focused text field, commits the final text in place of them with
//! `commit_string` (no synthesized keys, any keyboard layout), and reads the
//! field's text around the cursor for Shuush's edit learning. At rest it is
//! not running: it exits as soon as no take is live and no field is read,
//! so it never holds the seat against a real input method (fcitx5, ibus).
//!
//! Moved out of the companion voxtype fork (`src/output/preedit/`), where it
//! was the daemon's `[output] preedit` / `read_field`. MIT, see LICENSE.

mod client;
mod core;
mod driver;
mod instance;
mod live;
mod log;
mod protocol;
mod publish;
mod server;
mod state;
mod wayland;

#[cfg(test)]
mod e2e_tests;

use client::ClientError;
use live::Format;
use protocol::{Outcome, Req, MAX_REQUEST_BYTES};
use publish::Paths;
use std::io::Read;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

const USAGE: &str = "\
shuush-ime: live words as preedit, the final text in one commit, the focused field's text

Usage:
  shuush-ime serve [--take ID [--live PATH|auto] [--live-format text]] [--read]
        Run the helper (Shuush's toggle starts it under setpriv --pdeathsig).
        A helper already running gets the request instead. Exits when no take
        is live and no field is read.
  shuush-ime begin --take ID [--live PATH|auto] [--live-format text]
        --live: the file with the words heard so far (auto: voxtype/live_text)
  shuush-ime commit --take ID < text
        exit 0 in the field; 1 not taken (no text-input field, newlines, too
        long): type it, the preedit is gone; 3 no input method; 4 cancelled
  shuush-ime clear                  remove the preedit now
  shuush-ime end [--take ID]        the take went another way: clear, keep reading the field
  shuush-ime release [--take ID]    the take was cancelled: clear and let go now
  shuush-ime read-field [--wait MS] the field around the cursor as JSON (exit 1: none)
  shuush-ime status
  shuush-ime probe                  does the compositor offer input-method-v2? (binds nothing)
  shuush-ime --version

Exit 3 from a client command: no helper is running (nothing to do).
";

const EXIT_NOT_TAKEN: u8 = 1;
const EXIT_USAGE: u8 = 2;
const EXIT_NO_HELPER: u8 = 3;
const EXIT_STALE: u8 = 4;
const EXIT_UNSUPPORTED: u8 = 5;
const EXIT_NO_WAYLAND: u8 = 6;

#[derive(Default)]
struct Args {
    take: Option<String>,
    live: Option<PathBuf>,
    format: Option<String>,
    read: bool,
    wait_ms: u64,
}

fn parse(rest: &[String]) -> Result<Args, String> {
    let mut args = Args::default();
    let mut it = rest.iter();
    while let Some(a) = it.next() {
        let mut value = |name: &str| {
            it.next()
                .cloned()
                .ok_or_else(|| format!("{name} needs a value"))
        };
        match a.as_str() {
            "--take" => {
                let take = value("--take")?;
                if !publish::valid_take(&take) {
                    return Err("a take id is digits".into());
                }
                args.take = Some(take);
            }
            "--live" => args.live = Some(PathBuf::from(value("--live")?)),
            "--live-format" => args.format = Some(value("--live-format")?),
            "--read" => args.read = true,
            "--wait" => {
                args.wait_ms = value("--wait")?
                    .parse()
                    .map_err(|_| "--wait takes milliseconds".to_string())?
            }
            other => return Err(format!("unknown argument {other}")),
        }
    }
    Ok(args)
}

fn format_of(args: &Args) -> Result<Format, String> {
    match args.format.as_deref() {
        None => Ok(Format::Text),
        Some(name) => Format::parse(name).ok_or_else(|| format!("unknown live format {name}")),
    }
}

fn usage_error(why: &str) -> ExitCode {
    eprintln!("shuush-ime: {why}");
    eprint!("{USAGE}");
    ExitCode::from(EXIT_USAGE)
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = argv.first().map(String::as_str) else {
        return usage_error("which command?");
    };
    if matches!(cmd, "-h" | "--help" | "help") {
        print!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    if matches!(cmd, "-V" | "--version" | "version") {
        println!("shuush-ime {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }
    let args = match parse(&argv[1..]) {
        Ok(a) => a,
        Err(why) => return usage_error(&why),
    };
    let paths = Paths::from_env();
    // "auto": the fork's preview file, `[output] streaming_preview_file = "auto"`.
    let args = Args {
        live: args.live.map(|p| {
            if p.as_os_str() == "auto" {
                paths.live_text()
            } else {
                p
            }
        }),
        ..args
    };
    let format = match format_of(&args) {
        Ok(f) => f,
        Err(why) => return usage_error(&why),
    };
    match cmd {
        "serve" => {
            let initial = match (&args.take, args.read) {
                (Some(take), _) => Some(Req::Begin {
                    take: take.clone(),
                    live: args.live.clone(),
                    format,
                }),
                (None, true) => Some(Req::Read),
                (None, false) => None,
            };
            serve(&paths, initial)
        }
        "begin" => {
            let Some(take) = args.take else {
                return usage_error("begin needs --take");
            };
            simple(
                &paths,
                &Req::Begin {
                    take,
                    live: args.live,
                    format,
                },
            )
        }
        "commit" => {
            let Some(take) = args.take else {
                return usage_error("commit needs --take");
            };
            commit(&paths, take)
        }
        "clear" => simple(&paths, &Req::Clear),
        "end" => simple(&paths, &Req::End { take: args.take }),
        "release" => simple(&paths, &Req::Release { take: args.take }),
        "read-field" => read_field(&paths, Duration::from_millis(args.wait_ms)),
        "status" => match client::request(&paths, &Req::Status) {
            Ok(v) => {
                println!("{v}");
                ExitCode::SUCCESS
            }
            Err(e) => client_error(e),
        },
        "probe" => match wayland::probe() {
            Ok(true) => {
                println!("the compositor offers {}", wayland::MANAGER_INTERFACE);
                ExitCode::SUCCESS
            }
            Ok(false) => {
                println!("the compositor lacks {}", wayland::MANAGER_INTERFACE);
                ExitCode::from(EXIT_NOT_TAKEN)
            }
            Err(e) => {
                eprintln!("shuush-ime: no Wayland connection ({e:?})");
                ExitCode::from(EXIT_NO_WAYLAND)
            }
        },
        other => usage_error(&format!("unknown command {other}")),
    }
}

fn client_error(e: ClientError) -> ExitCode {
    match e {
        ClientError::NoHelper => ExitCode::from(EXIT_NO_HELPER),
        other => {
            eprintln!("shuush-ime: {other}");
            ExitCode::FAILURE
        }
    }
}

fn simple(paths: &Paths, req: &Req) -> ExitCode {
    match client::request(paths, req) {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => client_error(e),
    }
}

fn commit(paths: &Paths, take: String) -> ExitCode {
    let mut text = String::new();
    if std::io::stdin()
        .take(MAX_REQUEST_BYTES as u64)
        .read_to_string(&mut text)
        .is_err()
    {
        eprintln!("shuush-ime: the text on stdin is not UTF-8");
        return ExitCode::FAILURE;
    }
    match client::request(paths, &Req::Commit { take, text }) {
        Ok(answer) => {
            let outcome = answer["outcome"].as_str().and_then(Outcome::from_name);
            ExitCode::from(match outcome {
                Some(Outcome::Committed) => 0,
                Some(Outcome::NotActive | Outcome::NotCommittable) => EXIT_NOT_TAKEN,
                Some(Outcome::Stale) => EXIT_STALE,
                Some(Outcome::Unavailable) | None => EXIT_NO_HELPER,
            })
        }
        Err(e) => client_error(e),
    }
}

fn read_field(paths: &Paths, wait: Duration) -> ExitCode {
    let until = Instant::now() + wait;
    loop {
        match client::request(paths, &Req::Field) {
            Ok(answer) if !answer["field"].is_null() => {
                println!("{}", answer["field"]);
                return ExitCode::SUCCESS;
            }
            Ok(_) if Instant::now() < until => std::thread::sleep(Duration::from_millis(50)),
            Ok(_) => return ExitCode::from(EXIT_NOT_TAKEN),
            Err(e) => return client_error(e),
        }
    }
}

fn serve(paths: &Paths, initial: Option<Req>) -> ExitCode {
    let inst = match instance::start(paths, initial.as_ref()) {
        Ok(instance::Start::Server(inst)) => inst,
        Ok(instance::Start::Forwarded(answer)) => {
            if answer["ok"] == true {
                return ExitCode::SUCCESS;
            }
            eprintln!(
                "shuush-ime: {}",
                answer["error"].as_str().unwrap_or("refused")
            );
            return ExitCode::FAILURE;
        }
        Err(e) => {
            warn!("cannot start: {e}");
            return ExitCode::FAILURE;
        }
    };
    let stop = match server::stop_on_signals() {
        Ok(s) => s,
        Err(e) => {
            warn!("cannot watch for signals: {e}");
            return ExitCode::FAILURE;
        }
    };
    let mut backend = match wayland::connect(core::Core::new(paths.clone())) {
        Ok(b) => b,
        Err(wayland::ConnectError::Unsupported) => {
            info!(
                "the compositor lacks {}: Shuush types instead",
                wayland::MANAGER_INTERFACE
            );
            return ExitCode::from(EXIT_UNSUPPORTED);
        }
        Err(wayland::ConnectError::NoWayland(why)) => {
            warn!("no Wayland connection ({why})");
            return ExitCode::from(EXIT_NO_WAYLAND);
        }
    };
    use server::Backend;
    if let Some(req) = initial {
        let (answer, wait) = backend.with(|core, sink| core.request(req, sink));
        if answer["ok"] != true {
            warn!("{}", answer["error"].as_str().unwrap_or("refused"));
        }
        let sent = if wait {
            backend.roundtrip()
        } else {
            backend.flush()
        };
        if let Err(e) = sent {
            warn!("Wayland connection lost ({e})");
            return ExitCode::FAILURE;
        }
    }
    let result = server::run(&mut backend, &inst.listener, &stop);
    drop(inst);
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            warn!("Wayland connection lost ({e})");
            ExitCode::FAILURE
        }
    }
}
