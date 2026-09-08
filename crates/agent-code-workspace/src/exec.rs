use agent_code_tools::ExecuteCommand;
use std::collections::VecDeque;
use std::io::{Read, Write};
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::error::ToolError;
use crate::workspace::Workspace;

/// Bytes kept from the start of a stream in memory.
const OUTPUT_HEAD: usize = 2000;
/// Bytes kept from the end of a stream in memory.
const OUTPUT_TAIL: usize = 2000;

/// The bounded result of an execute_command call. Only fixed-size head/tail
/// and a total byte count are held in memory; the full output is streamed to
/// the log files, so a runaway command cannot exhaust memory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub program: String,
    /// Exit code, or None when the process was killed by the timeout.
    pub exit_code: Option<i32>,
    pub timed_out: bool,
    pub stdout_head: String,
    pub stdout_tail: String,
    /// Total bytes the command wrote to stdout.
    pub stdout_total: u64,
    pub stderr_head: String,
    pub stderr_tail: String,
    /// Total bytes the command wrote to stderr.
    pub stderr_total: u64,
    pub truncated: bool,
    /// Workspace-relative path to the full stdout log.
    pub stdout_log: String,
    /// Workspace-relative path to the full stderr log.
    pub stderr_log: String,
}

/// Streams a pipe to a log file while retaining only a bounded head/tail in
/// memory plus a running total.
struct BoundedSink {
    file: std::fs::File,
    head: Vec<u8>,
    tail: VecDeque<u8>,
    total: u64,
}

impl BoundedSink {
    fn new(file: std::fs::File) -> Self {
        Self {
            file,
            head: Vec::new(),
            tail: VecDeque::new(),
            total: 0,
        }
    }

    fn write(&mut self, chunk: &[u8]) -> std::io::Result<()> {
        self.file.write_all(chunk)?;
        let room = OUTPUT_HEAD.saturating_sub(self.head.len());
        if room > 0 {
            let take = room.min(chunk.len());
            self.head.extend_from_slice(&chunk[..take]);
        }
        for &b in chunk {
            self.tail.push_back(b);
            if self.tail.len() > OUTPUT_TAIL {
                self.tail.pop_front();
            }
        }
        self.total += chunk.len() as u64;
        Ok(())
    }

    fn head(&self) -> String {
        String::from_utf8_lossy(&self.head).into_owned()
    }

    fn tail(&self) -> String {
        let bytes: Vec<u8> = self.tail.iter().copied().collect();
        String::from_utf8_lossy(&bytes).into_owned()
    }

    fn total(&self) -> u64 {
        self.total
    }

    fn truncated(&self) -> bool {
        self.total > (OUTPUT_HEAD + OUTPUT_TAIL) as u64
    }
}

impl Workspace {
    /// Run `program` with `args` (no shell), cwd contained to the workspace,
    /// inherited env plus explicit overrides, and a timeout that kills the
    /// whole process group. Output is streamed to logs; head/tail are kept.
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

        let log_dir = self.root().join(".agent-logs");
        std::fs::create_dir_all(&log_dir).map_err(|e| ToolError::Io(e.to_string()))?;
        let out_log_rel = format!(".agent-logs/cmd-{pid}-out.log");
        let err_log_rel = format!(".agent-logs/cmd-{pid}-err.log");
        let out_log = std::fs::File::create(log_dir.join(format!("cmd-{pid}-out.log")))
            .map_err(|e| ToolError::Io(e.to_string()))?;
        let err_log = std::fs::File::create(log_dir.join(format!("cmd-{pid}-err.log")))
            .map_err(|e| ToolError::Io(e.to_string()))?;

        let out_handle = drain_pipe(out_pipe, out_log);
        let err_handle = drain_pipe(err_pipe, err_log);

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

        let out_sink = out_handle
            .join()
            .map_err(|_| ToolError::Io("stdout reader aborted".into()))?;
        let err_sink = err_handle
            .join()
            .map_err(|_| ToolError::Io("stderr reader aborted".into()))?;

        Ok(CommandOutput {
            program: req.program.clone(),
            exit_code: status.code(),
            timed_out,
            stdout_head: out_sink.head(),
            stdout_tail: out_sink.tail(),
            stdout_total: out_sink.total(),
            stderr_head: err_sink.head(),
            stderr_tail: err_sink.tail(),
            stderr_total: err_sink.total(),
            truncated: out_sink.truncated() || err_sink.truncated(),
            stdout_log: out_log_rel,
            stderr_log: err_log_rel,
        })
    }
}

/// Read a pipe to completion on a background thread so a chatty child never
/// deadlocks on a full pipe buffer, streaming to `log` and retaining only a
/// bounded head/tail.
fn drain_pipe<R: Read + Send + 'static>(
    pipe: Option<R>,
    log: std::fs::File,
) -> std::thread::JoinHandle<BoundedSink> {
    std::thread::spawn(move || {
        let mut sink = BoundedSink::new(log);
        let mut buf = [0u8; 8192];
        if let Some(mut p) = pipe {
            loop {
                match p.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        let _ = sink.write(&buf[..n]);
                    }
                    Err(_) => break,
                }
            }
        }
        sink
    })
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
        assert!(std::path::Path::new(ws.root().join(&out.stdout_log).to_str().unwrap()).exists());
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
        // Print 10000 lines so stdout far exceeds the head+tail cap.
        let out = ws
            .execute_command(&cmd("sh", &["-c", "seq 1 10000"], 10))
            .unwrap();
        assert!(out.truncated);
        assert!(out.stdout_head.starts_with("1\n"));
        assert!(out.stdout_tail.ends_with("10000\n"));
        assert!(out.stdout_head.len() <= OUTPUT_HEAD);
        assert!(out.stdout_total > (OUTPUT_HEAD + OUTPUT_TAIL) as u64);
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
