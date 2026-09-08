use agent_code_tools::ExecuteCommand;
use std::io::Read;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::error::ToolError;
use crate::workspace::Workspace;

/// Characters kept from the start of a stream before it is truncated.
const OUTPUT_HEAD: usize = 2000;
/// Characters kept from the end of a stream before it is truncated.
const OUTPUT_TAIL: usize = 2000;

/// The bounded result of an execute_command call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub program: String,
    /// Exit code, or None when the process was killed by the timeout.
    pub exit_code: Option<i32>,
    pub timed_out: bool,
    pub stdout_head: String,
    pub stdout_tail: String,
    pub stderr_head: String,
    pub stderr_tail: String,
    pub truncated: bool,
    /// Workspace-relative path to the full (untruncated) output log.
    pub log_path: String,
}

impl Workspace {
    /// Run `program` with `args` (no shell), cwd contained to the workspace,
    /// inherited env plus explicit overrides, and a timeout that kills the
    /// whole process group. Full output is logged; head/tail are returned.
    pub fn execute_command(&self, req: &ExecuteCommand) -> Result<CommandOutput, ToolError> {
        let cwd = match &req.cwd {
            Some(c) => self.resolve(c)?,
            None => self.root().to_path_buf(),
        };

        let mut cmd = Command::new(&req.program);
        cmd.args(&req.args)
            .current_dir(&cwd)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for (k, v) in &req.env {
            cmd.env(k, v);
        }
        // New process group so a timeout can kill the whole tree.
        cmd.process_group(0);

        let mut child = cmd
            .spawn()
            .map_err(|e| ToolError::Io(format!("spawn {program}: {e}", program = req.program)))?;
        let pid = child.id() as i32;

        let out_pipe = child.stdout.take();
        let err_pipe = child.stderr.take();
        let out_handle = drain_pipe(out_pipe);
        let err_handle = drain_pipe(err_pipe);

        let (tx, rx) = std::sync::mpsc::channel::<Result<std::process::ExitStatus, String>>();
        let waiter = std::thread::spawn(move || {
            let status = child.wait().map_err(|e| e.to_string());
            let _ = tx.send(status);
        });

        let deadline = Instant::now() + Duration::from_secs(req.timeout_seconds);
        let remaining = deadline.saturating_duration_since(Instant::now());
        let (status, timed_out) = match rx.recv_timeout(remaining) {
            Ok(Ok(status)) => (status, false),
            Ok(Err(e)) => return Err(ToolError::Io(e)),
            Err(_) => {
                unsafe {
                    libc::killpg(pid, libc::SIGKILL);
                }
                match rx.recv() {
                    Ok(Ok(status)) => (status, true),
                    Ok(Err(e)) => return Err(ToolError::Io(e)),
                    Err(_) => return Err(ToolError::Io("waiter thread exited".into())),
                }
            }
        };
        let _ = waiter.join();

        let stdout = out_handle
            .join()
            .map_err(|_| ToolError::Io("stdout reader aborted".into()))?;
        let stderr = err_handle
            .join()
            .map_err(|_| ToolError::Io("stderr reader aborted".into()))?;

        let log_rel = self.write_output_log(pid, &req.program, &req.args, &stdout, &stderr)?;

        let (stdout_head, stdout_tail, t1) = head_tail(&stdout);
        let (stderr_head, stderr_tail, t2) = head_tail(&stderr);
        Ok(CommandOutput {
            program: req.program.clone(),
            exit_code: status.code(),
            timed_out,
            stdout_head,
            stdout_tail,
            stderr_head,
            stderr_tail,
            truncated: t1 || t2,
            log_path: log_rel,
        })
    }

    /// Persist the full command output to `.agent-logs/` and return its
    /// workspace-relative path.
    fn write_output_log(
        &self,
        pid: i32,
        program: &str,
        args: &[String],
        stdout: &str,
        stderr: &str,
    ) -> Result<String, ToolError> {
        let log_dir = self.root().join(".agent-logs");
        std::fs::create_dir_all(&log_dir).map_err(|e| ToolError::Io(e.to_string()))?;
        let rel = format!(".agent-logs/cmd-{pid}.log");
        let body = format!(
            "$ {program} {}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n",
            args.join(" ")
        );
        self.atomic_write(&rel, &body)?;
        Ok(rel)
    }
}

/// Read a pipe to completion on a background thread so a chatty child never
/// deadlocks on a full pipe buffer.
fn drain_pipe<R: Read + Send + 'static>(pipe: Option<R>) -> std::thread::JoinHandle<String> {
    std::thread::spawn(move || {
        let Some(mut p) = pipe else {
            return String::new();
        };
        let mut buf = String::new();
        let _ = p.read_to_string(&mut buf);
        buf
    })
}

/// Split a string into (head, tail, truncated), keeping the first OUTPUT_HEAD
/// and last OUTPUT_TAIL characters on a char boundary.
fn head_tail(s: &str) -> (String, String, bool) {
    if s.len() <= OUTPUT_HEAD + OUTPUT_TAIL {
        return (s.to_string(), String::new(), false);
    }
    let head_end = char_boundary_before(s, OUTPUT_HEAD);
    let tail_start = char_boundary_after(s, s.len() - OUTPUT_TAIL);
    (s[..head_end].to_string(), s[tail_start..].to_string(), true)
}

fn char_boundary_before(s: &str, idx: usize) -> usize {
    let mut i = idx.min(s.len());
    while i > 0 && !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}

fn char_boundary_after(s: &str, idx: usize) -> usize {
    let mut i = idx;
    while i < s.len() && !s.is_char_boundary(i) {
        i += 1;
    }
    i
}

#[cfg(test)]
mod tests {
    use agent_code_tools::ExecuteCommand;

    use super::*;

    fn ws() -> (Workspace, std::path::PathBuf) {
        use std::sync::atomic::{AtomicU32, Ordering};
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("agent_code_exec_{}_{seq}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        (Workspace::new(&dir).unwrap(), dir)
    }

    fn cmd(program: &str, args: &[&str], timeout: u64) -> ExecuteCommand {
        ExecuteCommand {
            program: program.into(),
            args: args.iter().map(|s| s.to_string()).collect(),
            cwd: None,
            timeout_seconds: timeout,
            env: std::collections::BTreeMap::new(),
        }
    }

    #[test]
    fn runs_and_captures_stdout() {
        let (ws, _) = ws();
        let out = ws.execute_command(&cmd("echo", &["hello"], 10)).unwrap();
        assert!(!out.timed_out);
        assert_eq!(out.exit_code, Some(0));
        assert!(out.stdout_head.contains("hello"));
        assert!(std::path::Path::new(ws.root().join(&out.log_path).to_str().unwrap()).exists());
    }

    #[test]
    fn reports_nonzero_exit() {
        let (ws, _) = ws();
        let out = ws
            .execute_command(&cmd("sh", &["-c", "exit 3"], 10))
            .unwrap();
        assert_eq!(out.exit_code, Some(3));
    }

    #[test]
    fn truncates_long_output_to_head_and_tail() {
        let (ws, _) = ws();
        // Print 10000 distinct lines so stdout far exceeds the head+tail cap.
        let out = ws
            .execute_command(&cmd("sh", &["-c", "seq 1 10000"], 10))
            .unwrap();
        assert!(out.truncated);
        assert!(out.stdout_head.starts_with("1\n"));
        assert!(out.stdout_tail.ends_with("10000\n"));
        assert!(out.stdout_head.len() <= OUTPUT_HEAD + 1);
    }

    #[test]
    fn timeout_kills_grandchildren() {
        let (ws, _) = ws();
        // sh spawns a sleep (grandchild) that outlives the timeout; killing
        // the process group must terminate it too.
        let out = ws
            .execute_command(&cmd("sh", &["-c", "sleep 5 & wait"], 1))
            .unwrap();
        assert!(out.timed_out);
        assert!(out.exit_code.is_none());
    }
}
