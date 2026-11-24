//! Subprocess helpers matching v2's `subprocess.run(..., capture_output=True,
//! timeout=...)` and `_SubprocessThread._run_process` (stdout+stderr merged, read
//! line by line), with v2's `_SUBPROCESS_FLAGS` (no console window on Windows).

use std::ffi::OsString;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::time::{Duration, Instant};

/// Windows `CREATE_NO_WINDOW` (v2 `_SUBPROCESS_FLAGS`).
pub const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Apply v2's `_SUBPROCESS_FLAGS` (only when `hide` — some v2 calls don't pass them).
pub fn hide_window(cmd: &mut Command, hide: bool) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        if hide {
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
    }
    let _ = (cmd, hide);
}

/// Result of [`run_capture`].
#[derive(Debug, Clone)]
pub struct Captured {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// `subprocess.run(args, capture_output=True, text=True, timeout=t)`: `None` when
/// the program can't start or the timeout expires (v2 catches `OSError` /
/// `SubprocessError` for those).
pub fn run_capture(program: &Path, args: &[&str], timeout: Duration, hide: bool) -> Option<Captured> {
    let mut cmd = Command::new(program);
    cmd.args(args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    hide_window(&mut cmd, hide);
    let mut child = cmd.spawn().ok()?;
    let out = read_all(child.stdout.take());
    let err = read_all(child.stderr.take());
    let start = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if start.elapsed() < timeout => std::thread::sleep(Duration::from_millis(5)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    Some(Captured {
        code: status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&out.join().unwrap_or_default()).into_owned(),
        stderr: String::from_utf8_lossy(&err.join().unwrap_or_default()).into_owned(),
    })
}

fn read_all(r: Option<impl Read + Send + 'static>) -> std::thread::JoinHandle<Vec<u8>> {
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(mut r) = r {
            let _ = r.read_to_end(&mut buf);
        }
        buf
    })
}

/// `shutil.which(name)`: search `PATH` (with `PATHEXT` on Windows).
pub fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let exts: Vec<String> = if cfg!(windows) {
        let pe = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC".into());
        let mut v: Vec<String> = pe.split(';').filter(|e| !e.is_empty()).map(|e| e.to_string()).collect();
        if v.iter().any(|e| name.to_lowercase().ends_with(&e.to_lowercase())) {
            v = vec![String::new()];
        }
        v
    } else {
        vec![String::new()]
    };
    for dir in std::env::split_paths(&path) {
        if dir.as_os_str().is_empty() {
            continue;
        }
        for ext in &exts {
            let cand = dir.join(format!("{name}{ext}"));
            if cand.is_file() && is_executable(&cand) {
                return Some(cand);
            }
        }
    }
    None
}

#[cfg(unix)]
fn is_executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p).is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
}
#[cfg(not(unix))]
fn is_executable(_p: &Path) -> bool {
    true
}

/// `Popen.terminate()`: SIGTERM on Unix, `TerminateProcess` on Windows.
pub fn terminate(child: &mut Child) {
    #[cfg(unix)]
    unsafe {
        libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
    }
    #[cfg(not(unix))]
    {
        let _ = child.kill();
    }
}

/// Split text into lines like Python's universal-newline text mode (`\n`, `\r\n`
/// and a lone `\r` all end a line). Returns complete lines; the remainder stays in `buf`.
fn take_lines(buf: &mut Vec<u8>, eof: bool) -> Vec<String> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < buf.len() {
        match buf[i] {
            b'\n' => {
                lines.push(String::from_utf8_lossy(&buf[start..i]).into_owned());
                start = i + 1;
            }
            b'\r' => {
                if i + 1 == buf.len() && !eof {
                    break; // might be the first half of \r\n
                }
                lines.push(String::from_utf8_lossy(&buf[start..i]).into_owned());
                if buf.get(i + 1) == Some(&b'\n') {
                    i += 1;
                }
                start = i + 1;
            }
            _ => {}
        }
        i += 1;
    }
    if eof && start < buf.len() {
        lines.push(String::from_utf8_lossy(&buf[start..]).into_owned());
        start = buf.len();
    }
    buf.drain(..start);
    lines
}

/// Port of `_SubprocessThread._run_process`: run `program args` with `env` (the
/// environment is replaced, as v2 passes `env=_child_env()`), stdout and stderr
/// merged, stdin closed, no console window. Each output line is `rstrip()`ped and
/// given to `on_line`; when it returns false and the line is non-empty it goes to
/// `log`. Setting `cancel` terminates the process (v2 `cancel()`). Returns the exit
/// code (`-1` when killed by a signal).
pub fn run_process(
    program: &Path,
    args: &[OsString],
    env: &[(OsString, OsString)],
    cancel: &AtomicBool,
    mut on_line: impl FnMut(&str) -> bool,
    mut log: impl FnMut(String),
) -> std::io::Result<i32> {
    let (reader, writer) = std::io::pipe()?;
    let mut cmd = Command::new(program);
    cmd.args(args)
        .env_clear()
        .envs(env.iter().map(|(k, v)| (k, v)))
        .stdin(Stdio::null())
        .stdout(writer.try_clone()?)
        .stderr(writer);
    hide_window(&mut cmd, true);
    let mut child = cmd.spawn()?;
    drop(cmd); // closes our copies of the pipe's write end, so EOF arrives
    let (tx, rx) = mpsc::channel::<Option<Vec<u8>>>();
    std::thread::spawn(move || {
        let mut reader = reader;
        let mut buf = [0u8; 8192];
        loop {
            match reader.read(&mut buf) {
                Ok(0) | Err(_) => {
                    let _ = tx.send(None);
                    break;
                }
                Ok(n) => {
                    if tx.send(Some(buf[..n].to_vec())).is_err() {
                        break;
                    }
                }
            }
        }
    });
    let mut pending = Vec::new();
    let mut terminated = false;
    let handle = |line: String, on_line: &mut dyn FnMut(&str) -> bool, log: &mut dyn FnMut(String)| {
        let line = line.trim_end().to_string();
        if on_line(&line) {
            return;
        }
        if !line.is_empty() {
            log(line);
        }
    };
    loop {
        if cancel.load(Ordering::SeqCst) && !terminated {
            terminate(&mut child);
            terminated = true;
        }
        match rx.recv_timeout(Duration::from_millis(100)) {
            Ok(Some(chunk)) => {
                pending.extend_from_slice(&chunk);
                for line in take_lines(&mut pending, false) {
                    handle(line, &mut on_line, &mut log);
                }
            }
            Ok(None) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }
    }
    for line in take_lines(&mut pending, true) {
        handle(line, &mut on_line, &mut log);
    }
    let status = child.wait()?;
    Ok(status.code().unwrap_or(-1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn universal_newlines() {
        let mut b = b"a\r\nb\rc\nd\r".to_vec();
        assert_eq!(take_lines(&mut b, false), vec!["a", "b", "c"]);
        assert_eq!(b, b"d\r");
        assert_eq!(take_lines(&mut b, true), vec!["d"]);
        let mut b = b"tail".to_vec();
        assert_eq!(take_lines(&mut b, true), vec!["tail"]);
    }
}
