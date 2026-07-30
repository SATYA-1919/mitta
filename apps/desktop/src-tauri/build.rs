use std::path::PathBuf;
use std::process::Command;

fn main() {
    tauri_build::build();
    embed_info_plist();
    build_voice();
}

/// Embed `Info.plist` directly into the executable.
///
/// Tauri merges that file into the `.app` when it *bundles*, and `make app`
/// does not bundle — it runs `cargo run`, which produces a bare Mach-O. macOS
/// TCC keys microphone and speech-recognition grants to a bundle identifier and
/// refuses to present a prompt at all without the matching usage description, so
/// an unbundled binary is anonymous to the permission system: the Speech
/// Recognition authorisation sits at `notDetermined` forever, and voice fails
/// with "permission has not been granted" no matter what the user ticks in
/// System Settings.
///
/// `-sectcreate __TEXT __info_plist` is how a plain executable carries a plist.
/// With it, the dev binary has the same identity and usage strings as the
/// bundled app, so the prompt appears once and the grant sticks.
///
/// It does not survive a rebuild changing the binary's ad-hoc signature — TCC
/// re-asks after a recompile, which is a development cost, not a bug.
fn embed_info_plist() {
    println!("cargo:rerun-if-changed=Info.plist");

    if !cfg!(target_os = "macos") {
        return;
    }

    let plist = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"))
        .join("Info.plist");
    if !plist.exists() {
        panic!(
            "Info.plist is missing at {}. Without it macOS cannot grant \
             microphone or speech-recognition access to this binary.",
            plist.display()
        );
    }

    println!(
        "cargo:rustc-link-arg=-Wl,-sectcreate,__TEXT,__info_plist,{}",
        plist.display()
    );
}

/// Compile the Swift speech layer into a static library and link it.
///
/// R7's speech requirement is `Speech` and `AVSpeechSynthesizer`, which are
/// Swift/ObjC-only, so some native code is unavoidable. Swift rather than
/// `objc2` message-sends because this code registers delegates, taps an audio
/// engine and handles callbacks on three different queues — the version of that
/// written as raw `unsafe` Objective-C runtime calls is far harder to read and
/// no safer.
///
/// Compiled here rather than vendored as a prebuilt `.a` so the source in the
/// tree is demonstrably the binary that ships.
fn build_voice() {
    println!("cargo:rerun-if-changed=swift/MittaVoice.swift");

    if !cfg!(target_os = "macos") {
        return;
    }

    let out = PathBuf::from(std::env::var("OUT_DIR").expect("OUT_DIR"));
    let lib = out.join("libmittavoice.a");

    // Match the deployment target in `tauri.conf.json`. A mismatch here is a
    // link-time warning that turns into a runtime crash on an older machine.
    let status = Command::new("swiftc")
        .args([
            "-emit-library",
            "-static",
            "-O",
            "-target",
            &format!(
                "{}-apple-macosx15.0",
                std::env::var("CARGO_CFG_TARGET_ARCH").unwrap()
            ),
            "-module-name",
            "MittaVoice",
            "-o",
        ])
        .arg(&lib)
        .arg("swift/MittaVoice.swift")
        .status();

    match status {
        Ok(status) if status.success() => {}
        Ok(status) => panic!("swiftc failed with {status}; the voice layer cannot be built"),
        Err(error) => panic!(
            "could not run swiftc ({error}). Xcode command line tools are required to build \
             MITTA's speech layer — `xcode-select --install`"
        ),
    }

    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=mittavoice");

    for framework in [
        "Speech",
        "AVFoundation",
        "AVFAudio",
        "Foundation",
        "CoreMedia",
    ] {
        println!("cargo:rustc-link-lib=framework={framework}");
    }

    // The Swift runtime ships with macOS since 10.14.4 and we require 15, so
    // link against the OS copy rather than statically embedding it.
    println!("cargo:rustc-link-search=native=/usr/lib/swift");
    println!("cargo:rustc-link-arg=-Wl,-rpath,/usr/lib/swift");
}
