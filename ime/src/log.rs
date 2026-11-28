//! Logging to stderr, which the toggle sends to the journal or nowhere.
//! The field's own text never reaches a log.

use std::sync::OnceLock;

pub fn debug_enabled() -> bool {
    static ON: OnceLock<bool> = OnceLock::new();
    *ON.get_or_init(|| std::env::var_os("SHUUSH_IME_DEBUG").is_some_and(|v| v == "1"))
}

#[macro_export]
macro_rules! warn {
    ($($arg:tt)*) => { eprintln!("shuush-ime: {}", format_args!($($arg)*)) };
}

#[macro_export]
macro_rules! info {
    ($($arg:tt)*) => { eprintln!("shuush-ime: {}", format_args!($($arg)*)) };
}

#[macro_export]
macro_rules! debug {
    ($($arg:tt)*) => {
        if $crate::log::debug_enabled() {
            eprintln!("shuush-ime: {}", format_args!($($arg)*))
        }
    };
}
