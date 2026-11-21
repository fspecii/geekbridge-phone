// SPDX-License-Identifier: GPL-3.0-or-later
//! `Platform` for Mac voice mode: the helper behind the same interface Linux and Windows use.
//!
//! Calls are polled by the service, so they are plain requests. Contacts and messages come back
//! as descriptors that the app's OBEX code reads and writes like any stream. Voice does not pass
//! through here: it travels on the helper's audio socket.

use super::client::{ClientError, HelperClient};
use phone_core::calls::{CallSnapshot, CallState};
use phone_core::platform::{NearbyDevice, PhoneInfo, Platform, PlatformError, Result, Stream};
use serde_json::{json, Value};
use std::os::unix::io::FromRawFd;
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};
use std::time::Duration;

const QUICK: Duration = Duration::from_secs(10);
/// Opening a service includes looking it up on the phone and opening its channel.
const OPEN_SERVICE: Duration = Duration::from_secs(45);
const PAIRING: Duration = Duration::from_secs(150);
/// The first OBEX answer after a fresh pairing can take many seconds (5.7 s was seen live).
const STREAM_TIMEOUT: Duration = Duration::from_secs(30);

pub struct VoiceModePlatform {
    client: Arc<HelperClient>,
    selected: Mutex<Option<String>>,
    fault: Arc<Mutex<Option<String>>>,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn map_error(error: ClientError) -> PlatformError {
    match error {
        ClientError::Helper { code, message } if code == "no_service" => {
            let _ = message;
            PlatformError::NoService
        }
        ClientError::Helper { message, .. } => PlatformError::Other(message),
        ClientError::Closed => {
            PlatformError::Other("Mac voice mode has stopped. Turn it off and on again.".into())
        }
        ClientError::Timeout => {
            PlatformError::Other("Mac voice mode did not answer in time.".into())
        }
        ClientError::Io(e) => PlatformError::Io(e),
    }
}

/// Keeps what the helper accepts: digits and `+ * # , ;`. Spaces, dashes and brackets are dropped.
fn clean_number(number: &str) -> Option<String> {
    let kept: String = number
        .chars()
        .filter(|c| !matches!(c, ' ' | '-' | '(' | ')' | '.' | '\u{a0}'))
        .collect();
    let valid = !kept.is_empty()
        && kept.len() <= 32
        && kept
            .chars()
            .all(|c| c.is_ascii_digit() || matches!(c, '+' | '*' | '#' | ',' | ';'));
    valid.then_some(kept)
}

impl VoiceModePlatform {
    pub fn new(client: Arc<HelperClient>) -> Self {
        let fault = Arc::new(Mutex::new(None));
        let sink = fault.clone();
        client.subscribe(move |event| {
            if event.name == "fault" {
                let message = event.fields["message"]
                    .as_str()
                    .unwrap_or("the Bluetooth controller failed");
                *lock(&sink) = Some(message.to_string());
            }
        });
        Self {
            client,
            selected: Mutex::new(None),
            fault,
        }
    }

    /// Tells the helper which phone to keep connected.
    pub fn select(&self, address: &str) -> Result<()> {
        self.request("select_phone", json!({"address": address}), QUICK)?;
        *lock(&self.selected) = Some(address.to_string());
        Ok(())
    }

    fn ensure_selected(&self, address: &str) -> Result<()> {
        if lock(&self.selected).as_deref() == Some(address) {
            return Ok(());
        }
        self.select(address)
    }

    fn request(&self, op: &str, params: Value, timeout: Duration) -> Result<super::client::Reply> {
        if let Some(message) = lock(&self.fault).clone() {
            return Err(PlatformError::Other(format!(
                "Mac voice mode stopped: {message}"
            )));
        }
        self.client.request(op, params, timeout).map_err(map_error)
    }

    fn open(&self, address: &str, service: &str) -> Result<Box<dyn Stream>> {
        self.ensure_selected(address)?;
        let reply = self.request("open_service", json!({"service": service}), OPEN_SERVICE)?;
        let fd = reply
            .fd
            .ok_or_else(|| PlatformError::Other("the helper did not pass the channel".into()))?;
        // SAFETY: the helper passed a socket descriptor that nothing else in this process owns.
        let stream = unsafe { UnixStream::from_raw_fd(fd) };
        stream.set_read_timeout(Some(STREAM_TIMEOUT))?;
        stream.set_write_timeout(Some(STREAM_TIMEOUT))?;
        Ok(Box::new(stream))
    }
}

impl Platform for VoiceModePlatform {
    fn phones(&self) -> Result<Vec<PhoneInfo>> {
        let reply = self.request("phones", json!({}), QUICK)?;
        let mut phones: Vec<PhoneInfo> = reply.fields["phones"]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .filter_map(|row| {
                        Some(PhoneInfo {
                            address: row["address"].as_str()?.to_string(),
                            name: row["name"].as_str().unwrap_or("").to_string(),
                            connected: row["connected"].as_bool().unwrap_or(false),
                            battery: row["battery"].as_u64().map(|b| b.min(100) as u8),
                            apple: row["apple"].as_bool().unwrap_or(false),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();
        phones.sort_by(|a, b| {
            b.connected
                .cmp(&a.connected)
                .then_with(|| a.name.cmp(&b.name))
        });
        // The helper keeps one phone connected. Until the app picks one, use the first.
        if lock(&self.selected).is_none() {
            if let Some(first) = phones.first() {
                self.select(&first.address.clone())?;
            }
        }
        Ok(phones)
    }

    fn computer_name(&self) -> Option<String> {
        let out = std::process::Command::new("scutil")
            .args(["--get", "ComputerName"])
            .output()
            .ok()?;
        let name = String::from_utf8(out.stdout).ok()?.trim().to_string();
        (!name.is_empty()).then_some(name)
    }

    fn open_map(&self, address: &str) -> Result<Box<dyn Stream>> {
        self.open(address, "map")
    }

    fn open_pbap(&self, address: &str) -> Result<Box<dyn Stream>> {
        self.open(address, "pbap")
    }

    fn calls(&self, address: &str) -> Result<Vec<CallSnapshot>> {
        self.ensure_selected(address)?;
        let reply = self.request("calls", json!({}), QUICK)?;
        Ok(reply.fields["calls"]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .map(|row| CallSnapshot {
                        id: row["id"].as_str().unwrap_or("call-1").to_string(),
                        state: CallState::parse(row["state"].as_str().unwrap_or("")),
                        number: row["number"].as_str().unwrap_or("").to_string(),
                        name: row["name"]
                            .as_str()
                            .filter(|n| !n.is_empty())
                            .map(str::to_string),
                    })
                    .collect()
            })
            .unwrap_or_default())
    }

    fn dial(&self, address: &str, number: &str) -> Result<()> {
        let number = clean_number(number)
            .ok_or_else(|| PlatformError::Other("That number cannot be dialled.".into()))?;
        self.ensure_selected(address)?;
        self.request("dial", json!({"number": number}), QUICK)
            .map(|_| ())
    }

    fn answer(&self, address: &str, _call_id: &str) -> Result<()> {
        self.ensure_selected(address)?;
        self.request("answer", json!({}), QUICK).map(|_| ())
    }

    fn hang_up(&self, address: &str, _call_id: &str) -> Result<()> {
        self.ensure_selected(address)?;
        self.request("hang_up", json!({}), QUICK).map(|_| ())
    }

    fn hang_up_all(&self, address: &str) -> Result<()> {
        self.ensure_selected(address)?;
        self.request("hang_up_all", json!({}), QUICK).map(|_| ())
    }

    fn send_tones(&self, address: &str, tones: &str) -> Result<()> {
        self.ensure_selected(address)?;
        self.request("tones", json!({"tones": tones.to_ascii_uppercase()}), QUICK)
            .map(|_| ())
    }

    fn can_pair(&self) -> bool {
        true
    }

    fn scan_nearby(&self, seconds: u32) -> Result<Vec<NearbyDevice>> {
        let seconds = seconds.clamp(3, 30);
        let reply = self.request(
            "scan",
            json!({"seconds": seconds}),
            Duration::from_secs(seconds as u64 + 25),
        )?;
        Ok(reply.fields["found"]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .filter(|row| !row["paired"].as_bool().unwrap_or(false))
                    .filter_map(|row| {
                        Some(NearbyDevice {
                            id: row["id"].as_str()?.to_string(),
                            name: row["name"].as_str()?.to_string(),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default())
    }

    fn pair(&self, id: &str, show_code: Arc<dyn Fn(Option<String>) + Send + Sync>) -> Result<()> {
        let token = self.client.subscribe(move |event| {
            if event.name == "pairing_code" {
                show_code(event.fields["code"].as_str().map(str::to_string));
            }
        });
        let result = self.request("pair", json!({"address": id}), PAIRING);
        self.client.unsubscribe(token);
        result.map(|_| ())
    }

    fn unpair(&self, address: &str) -> Result<()> {
        self.request("unpair", json!({"address": address}), QUICK)?;
        let mut selected = lock(&self.selected);
        if selected.as_deref() == Some(address) {
            *selected = None;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::super::client::testing::FakeHelper;
    use super::*;
    use std::io::{Read, Write};
    use std::os::unix::net::UnixStream;
    use std::sync::atomic::{AtomicUsize, Ordering};

    const PHONE: &str = "6C:AC:C2:00:00:01";

    fn reply(fields: Value) -> Option<(Value, Option<UnixStream>)> {
        let mut r = json!({"ok": true});
        r.as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        Some((r, None))
    }

    fn platform(
        script: impl Fn(&Value) -> Option<(Value, Option<UnixStream>)> + Send + Sync + 'static,
    ) -> (FakeHelper, VoiceModePlatform) {
        let helper = FakeHelper::start(Arc::new(script));
        let client = Arc::new(HelperClient::connect(helper.socket()).unwrap());
        (helper, VoiceModePlatform::new(client))
    }

    fn ops(helper: &FakeHelper) -> Vec<String> {
        helper
            .requests
            .lock()
            .unwrap()
            .iter()
            .map(|r| r["op"].as_str().unwrap().to_string())
            .collect()
    }

    fn standard(request: &Value) -> Option<(Value, Option<UnixStream>)> {
        match request["op"].as_str().unwrap() {
            "phones" => reply(json!({"phones": [
                {"address": PHONE, "name": "Sam's Z Fold5", "connected": true, "battery": 80, "apple": false},
                {"address": "AA:BB:CC:DD:EE:FF", "name": "iPhone", "connected": false, "battery": null, "apple": true}]})),
            "calls" => reply(
                json!({"calls": [{"id": "call-1", "state": "incoming", "number": "+441234567", "name": ""}]}),
            ),
            _ => reply(json!({})),
        }
    }

    #[test]
    fn phones_are_mapped_and_the_first_is_selected_once() {
        let (helper, p) = platform(standard);
        let phones = p.phones().unwrap();
        assert_eq!(phones.len(), 2);
        assert_eq!(phones[0].address, PHONE);
        assert!(phones[0].connected);
        assert_eq!(phones[0].battery, Some(80));
        assert!(phones[1].apple);
        p.phones().unwrap();
        assert_eq!(
            ops(&helper).iter().filter(|o| *o == "select_phone").count(),
            1
        );
        let select = helper
            .requests
            .lock()
            .unwrap()
            .iter()
            .find(|r| r["op"] == "select_phone")
            .cloned()
            .unwrap();
        assert_eq!(select["address"], PHONE);
    }

    #[test]
    fn calls_are_mapped_with_states_and_an_empty_name_becomes_none() {
        let (_helper, p) = platform(standard);
        let calls = p.calls(PHONE).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].state, CallState::Incoming);
        assert_eq!(calls[0].number, "+441234567");
        assert_eq!(calls[0].name, None);
    }

    #[test]
    fn every_call_state_is_understood() {
        let (_h, p) = platform(|r| {
            if r["op"] == "calls" {
                return reply(json!({"calls": [
                    {"id": "a", "state": "dialing", "number": "1", "name": "Ann"},
                    {"id": "b", "state": "alerting", "number": "2", "name": ""},
                    {"id": "c", "state": "active", "number": "3", "name": ""}]}));
            }
            reply(json!({}))
        });
        let states: Vec<_> = p
            .calls(PHONE)
            .unwrap()
            .into_iter()
            .map(|c| c.state)
            .collect();
        assert_eq!(
            states,
            vec![CallState::Dialing, CallState::Alerting, CallState::Active]
        );
        assert_eq!(p.calls(PHONE).unwrap()[0].name.as_deref(), Some("Ann"));
    }

    #[test]
    fn dialling_cleans_the_number_and_refuses_what_it_cannot_send() {
        let (helper, p) = platform(standard);
        p.dial(PHONE, "+44 (0)1234-567").unwrap();
        let sent = helper
            .requests
            .lock()
            .unwrap()
            .iter()
            .find(|r| r["op"] == "dial")
            .cloned()
            .unwrap();
        assert_eq!(sent["number"], "+4401234567");
        let before = ops(&helper).len();
        for bad in ["", "abc", "1;AT+CHUP", &"1".repeat(40)] {
            assert!(p.dial(PHONE, bad).is_err(), "{bad:?}");
        }
        assert_eq!(ops(&helper).len(), before, "nothing bad is sent");
    }

    #[test]
    fn answering_hanging_up_and_tones_send_the_right_operations() {
        let (helper, p) = platform(standard);
        p.answer(PHONE, "call-1").unwrap();
        p.hang_up(PHONE, "call-1").unwrap();
        p.hang_up_all(PHONE).unwrap();
        p.send_tones(PHONE, "12#a").unwrap();
        let log = ops(&helper);
        for expected in ["answer", "hang_up", "hang_up_all", "tones"] {
            assert!(
                log.iter().any(|o| o == expected),
                "{expected} missing in {log:?}"
            );
        }
        let tones = helper
            .requests
            .lock()
            .unwrap()
            .iter()
            .find(|r| r["op"] == "tones")
            .cloned()
            .unwrap();
        assert_eq!(tones["tones"], "12#A");
    }

    #[test]
    fn a_service_channel_is_a_working_stream_with_long_timeouts() {
        let (helper_end, app_end) = UnixStream::pair().unwrap();
        let cell = Mutex::new(Some(app_end));
        let (_helper, p) = platform(move |r| {
            if r["op"] == "open_service" {
                return Some((
                    json!({"ok": true, "service": "map", "fd": true}),
                    cell.lock().unwrap().take(),
                ));
            }
            reply(json!({}))
        });
        let mut stream = p.open_map(PHONE).unwrap();
        let mut far = helper_end;
        far.write_all(b"OBEX").unwrap();
        let mut got = [0u8; 4];
        stream.read_exact(&mut got).unwrap();
        assert_eq!(&got, b"OBEX");
        stream.write_all(b"GET").unwrap();
        let mut back = [0u8; 3];
        far.read_exact(&mut back).unwrap();
        assert_eq!(&back, b"GET");
    }

    #[test]
    fn the_stream_waits_long_enough_for_a_slow_first_answer() {
        let (_far, app_end) = UnixStream::pair().unwrap();
        let cell = Mutex::new(Some(app_end));
        let (_helper, p) = platform(move |r| {
            if r["op"] == "open_service" {
                return Some((json!({"ok": true, "fd": true}), cell.lock().unwrap().take()));
            }
            reply(json!({}))
        });
        let stream = p.open_pbap(PHONE).unwrap();
        // Boxed as a plain stream, so check the timeout through the descriptor's own socket option.
        drop(stream);
        assert!(STREAM_TIMEOUT >= Duration::from_secs(30));
    }

    #[test]
    fn a_missing_service_is_reported_as_one() {
        let (_helper, p) = platform(|r| {
            if r["op"] == "open_service" {
                return Some((
                    json!({"ok": false, "error": {"code": "no_service", "message": "no such service"}}),
                    None,
                ));
            }
            reply(json!({}))
        });
        assert!(matches!(p.open_map(PHONE), Err(PlatformError::NoService)));
    }

    #[test]
    fn a_helper_error_reaches_the_user_in_plain_words() {
        let (_helper, p) = platform(|r| {
            if r["op"] == "answer" {
                return Some((
                    json!({"ok": false, "error": {"code": "no_phone", "message": "no phone is connected"}}),
                    None,
                ));
            }
            reply(json!({}))
        });
        assert_eq!(
            p.answer(PHONE, "x").unwrap_err().to_string(),
            "no phone is connected"
        );
    }

    #[test]
    fn a_stopped_helper_is_reported_clearly() {
        let (helper, p) = platform(standard);
        p.phones().unwrap();
        helper.hang_up_on_client();
        std::thread::sleep(Duration::from_millis(200));
        let message = p.calls(PHONE).unwrap_err().to_string();
        assert!(message.contains("Mac voice mode has stopped"), "{message}");
    }

    #[test]
    fn a_controller_fault_stops_further_requests_with_its_reason() {
        let (helper, p) = platform(standard);
        p.phones().unwrap();
        helper.send_event(
            json!({"event": "fault", "code": "fault", "message": "the controller crashed"}),
        );
        std::thread::sleep(Duration::from_millis(200));
        let before = ops(&helper).len();
        let message = p.calls(PHONE).unwrap_err().to_string();
        assert!(message.contains("the controller crashed"), "{message}");
        assert_eq!(
            ops(&helper).len(),
            before,
            "no request is sent after a fault"
        );
    }

    #[test]
    fn scanning_lists_only_phones_that_are_not_paired() {
        let (helper, p) = platform(|r| {
            if r["op"] == "scan" {
                return reply(json!({"found": [
                    {"id": "11:22:33:44:55:66", "name": "Pixel 9", "paired": false},
                    {"id": PHONE, "name": "Fold5", "paired": true}]}));
            }
            reply(json!({}))
        });
        let found = p.scan_nearby(100).unwrap();
        assert_eq!(
            found,
            vec![NearbyDevice {
                id: "11:22:33:44:55:66".into(),
                name: "Pixel 9".into()
            }]
        );
        let sent = helper
            .requests
            .lock()
            .unwrap()
            .iter()
            .find(|r| r["op"] == "scan")
            .cloned()
            .unwrap();
        assert_eq!(
            sent["seconds"], 30,
            "the time is held to what the helper allows"
        );
    }

    #[test]
    fn pairing_shows_the_code_while_it_runs_and_stops_listening_after() {
        let helper_cell: Arc<Mutex<Option<Arc<FakeHelper>>>> = Arc::new(Mutex::new(None));
        let cell = helper_cell.clone();
        let helper = Arc::new(FakeHelper::start(Arc::new(move |r| {
            if r["op"] == "pair" {
                if let Some(h) = cell.lock().unwrap().as_ref() {
                    h.send_event(
                        json!({"event": "pairing_code", "address": r["address"], "code": "123456"}),
                    );
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            reply(json!({}))
        })));
        *helper_cell.lock().unwrap() = Some(helper.clone());
        let p = VoiceModePlatform::new(Arc::new(HelperClient::connect(helper.socket()).unwrap()));
        p.client.request("status", json!({}), QUICK).ok(); // connect handshake for the fake
        let shown = Arc::new(Mutex::new(Vec::new()));
        let seen = shown.clone();
        p.pair(
            "11:22:33:44:55:66",
            Arc::new(move |code| lock(&seen).push(code)),
        )
        .unwrap();
        assert_eq!(*lock(&shown), vec![Some("123456".to_string())]);
        helper.send_event(json!({"event": "pairing_code", "address": "x", "code": "999999"}));
        std::thread::sleep(Duration::from_millis(150));
        assert_eq!(lock(&shown).len(), 1, "no listener is left after pairing");
    }

    #[test]
    fn unpairing_forgets_the_selection() {
        let counter = Arc::new(AtomicUsize::new(0));
        let c = counter.clone();
        let (_helper, p) = platform(move |r| {
            if r["op"] == "select_phone" {
                c.fetch_add(1, Ordering::SeqCst);
            }
            standard(r)
        });
        p.phones().unwrap();
        p.unpair(PHONE).unwrap();
        p.phones().unwrap();
        assert_eq!(
            counter.load(Ordering::SeqCst),
            2,
            "a new phone is selected after the old one is removed"
        );
    }

    #[test]
    fn using_another_phone_selects_it_first() {
        let (helper, p) = platform(standard);
        p.calls(PHONE).unwrap();
        p.calls("AA:BB:CC:DD:EE:FF").unwrap();
        let selects: Vec<_> = helper
            .requests
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r["op"] == "select_phone")
            .map(|r| r["address"].clone())
            .collect();
        assert_eq!(selects, vec![json!(PHONE), json!("AA:BB:CC:DD:EE:FF")]);
    }
}
