use serde_json::{json, Value};
use std::net::{Shutdown, TcpStream};
use std::process::Child;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use tauri::{AppHandle, Emitter};

pub struct ActiveSession {
    pub id: u64,
    pub kind: &'static str,
    pub child: Option<Child>,
    pub completed: bool,
    pub awaiting_selection: bool,
    pub transfer_finished: bool,
    pub error: Option<String>,
}

impl ActiveSession {
    fn new(id: u64, kind: &'static str) -> Self {
        Self {
            id,
            kind,
            child: None,
            completed: false,
            awaiting_selection: false,
            transfer_finished: false,
            error: None,
        }
    }
}

#[derive(Clone)]
pub struct AppState {
    pub active: Arc<Mutex<Option<ActiveSession>>>,
    pub relay_stop: Arc<Mutex<Option<mpsc::Sender<()>>>>,
    pub oracle_stream: Arc<Mutex<Option<TcpStream>>>,
    next_id: Arc<AtomicU64>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            active: Arc::new(Mutex::new(None)),
            relay_stop: Arc::new(Mutex::new(None)),
            oracle_stream: Arc::new(Mutex::new(None)),
            next_id: Arc::new(AtomicU64::new(1)),
        }
    }
}

impl AppState {
    pub fn reserve(&self, kind: &'static str) -> Result<u64, String> {
        let mut active = self
            .active
            .lock()
            .map_err(|_| "active session lock failed")?;
        if active.is_some() {
            return Err("another transfer is already running".to_string());
        }
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        *active = Some(ActiveSession::new(id, kind));
        Ok(id)
    }

    pub fn is_current(&self, id: u64) -> bool {
        self.active
            .lock()
            .ok()
            .and_then(|active| active.as_ref().map(|session| session.id == id))
            == Some(true)
    }
}

pub fn send(app: &AppHandle, event_type: &str, mut detail: Value) {
    if !detail.is_object() {
        detail = json!({});
    }
    detail["type"] = Value::String(event_type.to_string());
    let _ = app.emit("transfer:event", detail);
}

pub fn stop_oracle(state: &AppState) {
    if let Ok(mut relay) = state.relay_stop.lock() {
        if let Some(stop) = relay.take() {
            let _ = stop.send(());
        }
    }
    if let Ok(mut stream) = state.oracle_stream.lock() {
        if let Some(stream) = stream.take() {
            let _ = stream.shutdown(Shutdown::Both);
        }
    }
}

pub fn stop_active(state: &AppState) {
    if let Ok(mut active) = state.active.lock() {
        if let Some(mut session) = active.take() {
            if let Some(child) = session.child.as_mut() {
                let _ = child.kill();
            }
        }
    }
    stop_oracle(state);
}

pub fn finish(app: &AppHandle, state: &AppState, error: Option<String>) {
    let should_report_stop = state
        .active
        .lock()
        .ok()
        .is_some_and(|active| active.as_ref().is_some_and(|session| !session.completed));
    stop_active(state);
    if let Some(error) = error {
        send(app, "error", json!({ "message": error }));
    } else if should_report_stop {
        send(app, "status", json!({ "message": "Transfer stopped." }));
    }
}
