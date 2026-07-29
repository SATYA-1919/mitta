// Suppress the console window on Windows in release. Harmless on macOS, and
// keeping it here means the Windows port does not need to remember it.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    mitta_lib::run()
}
