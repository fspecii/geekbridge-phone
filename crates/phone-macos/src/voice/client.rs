// SPDX-License-Identifier: GPL-3.0-or-later
//! The control connection to the helper: requests with replies, events, and descriptors.
//!
//! One reader thread reads everything the helper sends. A reply is matched to its request by id;
//! if the reply says `"fd": true`, the descriptor that arrived with it is attached. Events go to
//! the listeners. When the helper goes away, every waiting request fails at once.

use super::fdpass::recv_with_fds;
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::io::{self, Write};
use std::os::unix::io::{AsRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

/// Longest line the helper may send (its own limit is 64 KiB).
const MAX_LINE: usize = 64 * 1024;

#[derive(Debug)]
pub enum ClientError {
    /// No reply in time. The request may still have been carried out.
    Timeout,
    /// The helper closed the connection.
    Closed,
    /// The helper answered with an error.
    Helper { code: String, message: String },
    Io(io::Error),
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClientError::Timeout => write!(f, "the Bluetooth helper did not answer in time"),
            ClientError::Closed => write!(f, "the Bluetooth helper has stopped"),
            ClientError::Helper { message, .. } => write!(f, "{message}"),
            ClientError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for ClientError {}

impl From<io::Error> for ClientError {
    fn from(e: io::Error) -> Self {
        ClientError::Io(e)
    }
}

/// A successful reply: its fields, and the descriptor that came with it, if any.
#[derive(Debug)]
pub struct Reply {
    pub fields: Value,
    pub fd: Option<RawFd>,
}

/// Something the helper reports on its own.
#[derive(Debug, Clone)]
pub struct Event {
    pub name: String,
    pub fields: Value,
}

type Pending = Result<Reply, ClientError>;
type Listener = Box<dyn Fn(&Event) + Send>;

struct Shared {
    next_id: AtomicU64,
    pending: Mutex<HashMap<u64, Sender<Pending>>>,
    fds: Mutex<VecDeque<RawFd>>,
    listeners: Mutex<Vec<(u64, Listener)>>,
    next_listener: AtomicU64,
    closed: AtomicBool,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn close_fd(fd: RawFd) {
    // SAFETY: closing a descriptor this module owns.
    unsafe { libc::close(fd) };
}

pub struct HelperClient {
    shared: Arc<Shared>,
    writer: Mutex<UnixStream>,
}

impl HelperClient {
    /// Connects to the helper's control socket and starts the reader thread.
    pub fn connect(path: &Path) -> io::Result<Self> {
        let stream = UnixStream::connect(path)?;
        let reader = stream.try_clone()?;
        let shared = Arc::new(Shared {
            next_id: AtomicU64::new(1),
            pending: Mutex::new(HashMap::new()),
            fds: Mutex::new(VecDeque::new()),
            listeners: Mutex::new(Vec::new()),
            next_listener: AtomicU64::new(1),
            closed: AtomicBool::new(false),
        });
        let thread_shared = shared.clone();
        std::thread::Builder::new()
            .name("gbp-helper-reader".into())
            .spawn(move || read_loop(reader, thread_shared))?;
        Ok(Self {
            shared,
            writer: Mutex::new(stream),
        })
    }

    pub fn is_closed(&self) -> bool {
        self.shared.closed.load(Ordering::SeqCst)
    }

    /// Sends a request and waits for its reply.
    pub fn request(&self, op: &str, params: Value, timeout: Duration) -> Result<Reply, ClientError> {
        if self.is_closed() {
            return Err(ClientError::Closed);
        }
        let id = self.shared.next_id.fetch_add(1, Ordering::SeqCst);
        let mut message = match params {
            Value::Object(map) => map,
            _ => serde_json::Map::new(),
        };
        message.insert("id".into(), json!(id));
        message.insert("op".into(), json!(op));
        let mut line = serde_json::to_vec(&Value::Object(message)).map_err(io::Error::other)?;
        line.push(b'\n');
        let (tx, rx) = channel();
        lock(&self.shared.pending).insert(id, tx);
        if let Err(e) = lock(&self.writer).write_all(&line) {
            lock(&self.shared.pending).remove(&id);
            return Err(ClientError::Io(e));
        }
        match rx.recv_timeout(timeout) {
            Ok(result) => result,
            Err(_) => {
                lock(&self.shared.pending).remove(&id);
                if self.is_closed() {
                    Err(ClientError::Closed)
                } else {
                    Err(ClientError::Timeout)
                }
            }
        }
    }

    /// Registers a listener for events; returns a token for `unsubscribe`.
    pub fn subscribe(&self, listener: impl Fn(&Event) + Send + 'static) -> u64 {
        let token = self.shared.next_listener.fetch_add(1, Ordering::SeqCst);
        lock(&self.shared.listeners).push((token, Box::new(listener)));
        token
    }

    pub fn unsubscribe(&self, token: u64) {
        lock(&self.shared.listeners).retain(|(t, _)| *t != token);
    }

    /// Closes the connection. The helper notices and starts its stop timer.
    pub fn close(&self) {
        // SAFETY: shutting down our own socket makes the reader thread return.
        unsafe { libc::shutdown(lock(&self.writer).as_raw_fd(), libc::SHUT_RDWR) };
    }
}

impl Drop for HelperClient {
    fn drop(&mut self) {
        self.close();
    }
}

fn fail_all(shared: &Shared) {
    shared.closed.store(true, Ordering::SeqCst);
    let pending: Vec<_> = lock(&shared.pending).drain().collect();
    for (_, tx) in pending {
        let _ = tx.send(Err(ClientError::Closed));
    }
    for fd in lock(&shared.fds).drain(..) {
        close_fd(fd);
    }
}

fn read_loop(socket: UnixStream, shared: Arc<Shared>) {
    let fd = socket.as_raw_fd();
    let mut buffer: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 8192];
    'outer: loop {
        let (n, fds) = match recv_with_fds(fd, &mut chunk) {
            Ok(r) => r,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        };
        lock(&shared.fds).extend(fds);
        if n == 0 {
            break;
        }
        buffer.extend_from_slice(&chunk[..n]);
        while let Some(newline) = buffer.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = buffer.drain(..=newline).collect();
            handle_line(&line[..line.len() - 1], &shared);
        }
        if buffer.len() > MAX_LINE {
            break 'outer;
        }
    }
    fail_all(&shared);
}

fn handle_line(line: &[u8], shared: &Shared) {
    let Ok(message) = serde_json::from_slice::<Value>(line) else {
        return;
    };
    if let Some(name) = message.get("event").and_then(Value::as_str) {
        let event = Event {
            name: name.to_string(),
            fields: message.clone(),
        };
        for (_, listener) in lock(&shared.listeners).iter() {
            listener(&event);
        }
        return;
    }
    let Some(id) = message.get("id").and_then(Value::as_u64) else {
        return;
    };
    let carries_fd = message.get("fd").and_then(Value::as_bool).unwrap_or(false);
    let fd = if carries_fd { lock(&shared.fds).pop_front() } else { None };
    let result = if message.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        Ok(Reply { fields: message, fd })
    } else {
        let error = message.get("error");
        let text = |key: &str| {
            error
                .and_then(|e| e.get(key))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        };
        if let Some(fd) = fd {
            close_fd(fd);
        }
        Err(ClientError::Helper {
            code: text("code"),
            message: text("message"),
        })
    };
    match lock(&shared.pending).remove(&id) {
        Some(tx) => {
            if let Err(std::sync::mpsc::SendError(Ok(reply))) = tx.send(result) {
                if let Some(fd) = reply.fd {
                    close_fd(fd);
                }
            }
        }
        None => {
            // A late reply for a request that gave up: do not leak its descriptor.
            if let Ok(reply) = result {
                if let Some(fd) = reply.fd {
                    close_fd(fd);
                }
            }
        }
    }
}

#[cfg(test)]
pub(crate) mod testing {
    //! A fake helper for tests: a Unix socket server that plays scripted replies.
    use super::super::fdpass::send_with_fd;
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::io::AsRawFd;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Mutex};

    pub struct FakeHelper {
        pub path: PathBuf,
        pub requests: Arc<Mutex<Vec<serde_json::Value>>>,
        pub connection: Arc<Mutex<Option<UnixStream>>>,
        _dir: tempfile::TempDir,
    }

    /// `script(request) -> (reply json, optional descriptor to pass)`; `None` means stay silent.
    pub type Script = dyn Fn(&serde_json::Value) -> Option<(serde_json::Value, Option<UnixStream>)> + Send + Sync;

    impl FakeHelper {
        pub fn start(script: Arc<Script>) -> Self {
            let dir = tempfile::tempdir_in("/tmp").unwrap();
            let path = dir.path().join("btd.sock");
            let listener = UnixListener::bind(&path).unwrap();
            let requests = Arc::new(Mutex::new(Vec::new()));
            let connection: Arc<Mutex<Option<UnixStream>>> = Arc::new(Mutex::new(None));
            let (reqs, conn) = (requests.clone(), connection.clone());
            std::thread::spawn(move || {
                let Ok((stream, _)) = listener.accept() else { return };
                *conn.lock().unwrap() = Some(stream.try_clone().unwrap());
                let mut writer = stream.try_clone().unwrap();
                for line in BufReader::new(stream).lines() {
                    let Ok(line) = line else { break };
                    let request: serde_json::Value = serde_json::from_str(&line).unwrap();
                    reqs.lock().unwrap().push(request.clone());
                    if let Some((mut reply, fd)) = script(&request) {
                        reply["id"] = request["id"].clone();
                        let data = format!("{reply}\n");
                        match fd {
                            Some(passed) => {
                                send_with_fd(writer.as_raw_fd(), data.as_bytes(), passed.as_raw_fd()).unwrap()
                            }
                            None => writer.write_all(data.as_bytes()).unwrap(),
                        }
                    }
                }
            });
            Self { path, requests, connection, _dir: dir }
        }

        pub fn send_event(&self, event: serde_json::Value) {
            let mut guard = self.connection.lock().unwrap();
            let stream = guard.as_mut().expect("no client connected");
            stream.write_all(format!("{event}\n").as_bytes()).unwrap();
        }

        pub fn hang_up_on_client(&self) {
            if let Some(stream) = self.connection.lock().unwrap().take() {
                let _ = stream.shutdown(std::net::Shutdown::Both);
            }
        }

        pub fn socket(&self) -> &Path {
            &self.path
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::FakeHelper;
    use super::*;
    use serde_json::json;
    use std::io::{Read, Write};
    use std::os::unix::io::FromRawFd;
    use std::os::unix::net::UnixStream;
    use std::sync::Arc;

    fn ok(extra: Value) -> Option<(Value, Option<UnixStream>)> {
        let mut reply = json!({"ok": true});
        if let (Some(r), Some(e)) = (reply.as_object_mut(), extra.as_object()) {
            r.extend(e.clone());
        }
        Some((reply, None))
    }

    #[test]
    fn a_request_gets_its_reply() {
        let helper = FakeHelper::start(Arc::new(|_| ok(json!({"state": "idle"}))));
        let client = HelperClient::connect(helper.socket()).unwrap();
        let reply = client.request("status", json!({}), Duration::from_secs(2)).unwrap();
        assert_eq!(reply.fields["state"], "idle");
        assert_eq!(helper.requests.lock().unwrap()[0]["op"], "status");
    }

    #[test]
    fn parameters_are_sent_with_the_operation_and_an_id() {
        let helper = FakeHelper::start(Arc::new(|_| ok(json!({}))));
        let client = HelperClient::connect(helper.socket()).unwrap();
        client.request("dial", json!({"number": "+441234567"}), Duration::from_secs(2)).unwrap();
        let sent = helper.requests.lock().unwrap()[0].clone();
        assert_eq!(sent["op"], "dial");
        assert_eq!(sent["number"], "+441234567");
        assert!(sent["id"].as_u64().is_some());
    }

    #[test]
    fn a_helper_error_becomes_a_clear_error() {
        let helper = FakeHelper::start(Arc::new(|_| {
            Some((json!({"ok": false, "error": {"code": "no_phone", "message": "no phone is connected"}}), None))
        }));
        let client = HelperClient::connect(helper.socket()).unwrap();
        match client.request("answer", json!({}), Duration::from_secs(2)) {
            Err(ClientError::Helper { code, message }) => {
                assert_eq!(code, "no_phone");
                assert_eq!(message, "no phone is connected");
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn a_silent_helper_times_out() {
        let helper = FakeHelper::start(Arc::new(|_| None));
        let client = HelperClient::connect(helper.socket()).unwrap();
        assert!(matches!(
            client.request("status", json!({}), Duration::from_millis(100)),
            Err(ClientError::Timeout)
        ));
    }

    #[test]
    fn a_descriptor_arrives_with_the_reply_that_owns_it() {
        let helper = FakeHelper::start(Arc::new(|_| {
            let (mut helper_end, app_end) = UnixStream::pair().unwrap();
            helper_end.write_all(b"phonebook").unwrap();
            // The real helper keeps its end open for the whole channel; so does this test.
            std::mem::forget(helper_end);
            Some((json!({"ok": true, "service": "pbap", "fd": true}), Some(app_end)))
        }));
        let client = HelperClient::connect(helper.socket()).unwrap();
        let reply = client.request("open_service", json!({"service": "pbap"}), Duration::from_secs(2)).unwrap();
        let mut stream = unsafe { UnixStream::from_raw_fd(reply.fd.expect("a descriptor")) };
        let mut got = [0u8; 9];
        stream.read_exact(&mut got).unwrap();
        assert_eq!(&got, b"phonebook");
    }

    #[test]
    fn a_reply_without_the_marker_has_no_descriptor() {
        let helper = FakeHelper::start(Arc::new(|_| ok(json!({}))));
        let client = HelperClient::connect(helper.socket()).unwrap();
        assert!(client.request("status", json!({}), Duration::from_secs(2)).unwrap().fd.is_none());
    }

    #[test]
    fn events_reach_listeners_until_they_unsubscribe() {
        let helper = FakeHelper::start(Arc::new(|_| ok(json!({}))));
        let client = HelperClient::connect(helper.socket()).unwrap();
        client.request("status", json!({}), Duration::from_secs(2)).unwrap(); // the helper now knows us
        let (tx, rx) = std::sync::mpsc::channel();
        let token = client.subscribe(move |e| {
            let _ = tx.send(e.clone());
        });
        helper.send_event(json!({"event": "call_state", "state": "incoming", "number": "+44"}));
        let event = rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(event.name, "call_state");
        assert_eq!(event.fields["number"], "+44");
        client.unsubscribe(token);
        helper.send_event(json!({"event": "call_state", "state": "ended"}));
        assert!(rx.recv_timeout(Duration::from_millis(200)).is_err());
    }

    #[test]
    fn replies_and_events_may_interleave_and_arrive_out_of_order() {
        let helper = FakeHelper::start(Arc::new(|r| {
            if r["op"] == "slow" {
                std::thread::sleep(Duration::from_millis(150));
            }
            ok(json!({"op": r["op"]}))
        }));
        let client = Arc::new(HelperClient::connect(helper.socket()).unwrap());
        let slow = {
            let c = client.clone();
            std::thread::spawn(move || c.request("slow", json!({}), Duration::from_secs(3)).unwrap())
        };
        std::thread::sleep(Duration::from_millis(30));
        let fast = client.request("fast", json!({}), Duration::from_secs(3)).unwrap();
        assert_eq!(fast.fields["op"], "fast");
        assert_eq!(slow.join().unwrap().fields["op"], "slow");
    }

    #[test]
    fn when_the_helper_goes_away_waiting_requests_fail_at_once() {
        let helper = Arc::new(FakeHelper::start(Arc::new(|r| {
            if r["op"] == "hang" { None } else { ok(json!({})) }
        })));
        let client = Arc::new(HelperClient::connect(helper.socket()).unwrap());
        client.request("status", json!({}), Duration::from_secs(2)).unwrap();
        let waiting = {
            let c = client.clone();
            std::thread::spawn(move || c.request("hang", json!({}), Duration::from_secs(10)))
        };
        std::thread::sleep(Duration::from_millis(100));
        helper.hang_up_on_client();
        let started = std::time::Instant::now();
        assert!(matches!(waiting.join().unwrap(), Err(ClientError::Closed)));
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(client.is_closed());
        assert!(matches!(
            client.request("status", json!({}), Duration::from_secs(1)),
            Err(ClientError::Closed)
        ));
    }

    #[test]
    fn a_late_reply_does_not_leak_its_descriptor() {
        let helper = FakeHelper::start(Arc::new(|r| {
            if r["op"] == "open_service" {
                std::thread::sleep(Duration::from_millis(300));
                let (_keep, app_end) = UnixStream::pair().unwrap();
                return Some((json!({"ok": true, "fd": true}), Some(app_end)));
            }
            ok(json!({}))
        }));
        let client = HelperClient::connect(helper.socket()).unwrap();
        let before = std::fs::read_dir("/dev/fd").unwrap().count();
        assert!(matches!(
            client.request("open_service", json!({"service": "map"}), Duration::from_millis(50)),
            Err(ClientError::Timeout)
        ));
        std::thread::sleep(Duration::from_millis(600)); // the late reply arrives and is dropped
        client.request("status", json!({}), Duration::from_secs(2)).unwrap();
        let after = std::fs::read_dir("/dev/fd").unwrap().count();
        assert!(after <= before + 1, "descriptors before {before}, after {after}");
    }
}
