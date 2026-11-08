use crate::oracle::ensure_oracle;
use crate::paths::{
    discovery_routes, existing_pairing_prefix, pairing_prefix_path, title_name, RuntimePaths,
};
use crate::state::{finish as finish_session, send, stop_active, AppState};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
use std::sync::Arc;
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, State};

#[cfg(unix)]
fn user_id() -> u32 {
    unsafe { libc::getuid() }
}

#[cfg(not(unix))]
fn user_id() -> u32 {
    0
}

#[cfg(unix)]
fn group_id() -> u32 {
    unsafe { libc::getgid() }
}

#[cfg(not(unix))]
fn group_id() -> u32 {
    0
}

fn text(options: &Value, key: &str) -> String {
    options
        .get(key)
        .and_then(|value| {
            value
                .as_str()
                .map(str::to_string)
                .or_else(|| Some(value.to_string()))
        })
        .unwrap_or_default()
}

fn number(options: &Value, key: &str, default: u64) -> u64 {
    options
        .get(key)
        .and_then(|value| value.as_u64().or_else(|| value.as_str()?.parse().ok()))
        .unwrap_or(default)
}

fn environment(paths: &RuntimePaths) -> HashMap<String, String> {
    HashMap::from([(
        "PYTHONPATH".to_string(),
        paths.resource_root.to_string_lossy().into_owned(),
    )])
}

fn python(paths: &RuntimePaths, module: &str) -> Command {
    let mut command = Command::new(paths.python_bin());
    command
        .arg("-u")
        .arg("-m")
        .arg(module)
        .current_dir(&paths.work_dir)
        .envs(environment(paths))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

fn attach_child(
    state: &AppState,
    id: u64,
    mut child: Child,
) -> Result<(ChildStdout, ChildStderr), String> {
    let stdout = child.stdout.take().ok_or("backend stdout is unavailable")?;
    let stderr = child.stderr.take().ok_or("backend stderr is unavailable")?;
    let mut active = state
        .active
        .lock()
        .map_err(|_| "active session lock failed")?;
    let Some(session) = active.as_mut().filter(|session| session.id == id) else {
        let _ = child.kill();
        return Err("transfer was cancelled".to_string());
    };
    session.child = Some(child);
    Ok((stdout, stderr))
}

fn wait_for_child(state: &AppState, id: u64) -> Result<i32, String> {
    loop {
        {
            let mut active = state
                .active
                .lock()
                .map_err(|_| "active session lock failed")?;
            let session = active
                .as_mut()
                .filter(|session| session.id == id)
                .ok_or("transfer was cancelled")?;
            if let Some(status) = session
                .child
                .as_mut()
                .ok_or("backend process is unavailable")?
                .try_wait()
                .map_err(|e| e.to_string())?
            {
                return Ok(status.code().unwrap_or(-1));
            }
        }
        thread::sleep(Duration::from_millis(50));
    }
}

fn forward_stderr(app: AppHandle, stderr: ChildStderr, prefix: &'static str) {
    thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            send(&app, "log", json!({ "message": format!("{prefix}{line}") }));
        }
    });
}

fn decorate_event(paths: &RuntimePaths, mut event: Value) -> Value {
    if event.get("type").and_then(Value::as_str) == Some("entries") {
        if let Some(entries) = event.get_mut("entries").and_then(Value::as_array_mut) {
            for entry in entries {
                let title = entry
                    .get("title")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                entry["name"] = Value::String(title_name(&paths.resource_root, &title));
            }
        }
    }
    event
}

fn command_output(paths: &RuntimePaths, module: &str, args: &[String]) -> Result<String, String> {
    let output = python(paths, module)
        .args(args)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| e.to_string())?;
    let mut text = String::from_utf8_lossy(&output.stdout).into_owned();
    text.push_str(&String::from_utf8_lossy(&output.stderr));
    if !output.status.success() {
        return Err(if text.trim().is_empty() {
            format!("{module} failed")
        } else {
            text.trim().to_string()
        });
    }
    Ok(text)
}

#[tauri::command]
pub fn discover(paths: State<'_, RuntimePaths>, options: Value) -> Result<Value, String> {
    let target = options.get("targetHost").and_then(Value::as_str);
    let routes = discovery_routes(target)?;
    let port = number(&options, "discoveryPort", 1006).to_string();
    let output = command_output(
        &paths,
        "poc.dt_discovery",
        &[
            "--routes".to_string(),
            serde_json::to_string(&routes).map_err(|e| e.to_string())?,
            "--port".to_string(),
            port,
        ],
    )?;
    let parsed: Value = serde_json::from_str(&output).map_err(|e| e.to_string())?;
    Ok(parsed.get("consoles").cloned().unwrap_or_else(|| json!([])))
}

#[tauri::command]
pub fn users(paths: State<'_, RuntimePaths>, metadata_path: String) -> Result<Value, String> {
    let output = command_output(
        &paths,
        "poc.dt_backend",
        &["users".to_string(), metadata_path],
    )?;
    let last = output
        .lines()
        .last()
        .ok_or("metadata backend returned no users")?;
    let event: Value = serde_json::from_str(last).map_err(|e| e.to_string())?;
    Ok(event.get("users").cloned().unwrap_or_else(|| json!([])))
}

#[tauri::command]
pub fn inventory(paths: State<'_, RuntimePaths>, metadata_path: String) -> Result<Value, String> {
    let output = command_output(
        &paths,
        "poc.dt_backend",
        &["inventory".to_string(), metadata_path],
    )?;
    let last = output
        .lines()
        .last()
        .ok_or("metadata backend returned no inventory")?;
    let event: Value = serde_json::from_str(last).map_err(|e| e.to_string())?;
    let mut entries = event.get("entries").cloned().unwrap_or_else(|| json!([]));
    for entry in entries.as_array_mut().into_iter().flatten() {
        let title = entry
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        entry["name"] = Value::String(title_name(&paths.resource_root, &title));
    }
    Ok(json!({
        "users": event.get("users").cloned().unwrap_or_else(|| json!([])),
        "entries": entries,
    }))
}

#[tauri::command]
pub fn select(state: State<'_, Arc<AppState>>, entry_ids: Vec<u64>) -> Result<(), String> {
    if entry_ids.is_empty() {
        return Err("select at least one save".to_string());
    }
    write_session(
        &state,
        "download",
        true,
        &format!(
            "{}\n",
            serde_json::to_string(&json!({ "entryIds": entry_ids })).map_err(|e| e.to_string())?
        ),
    )
}

#[tauri::command]
pub fn finish(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    write_session(&state, "download", true, "{\"action\":\"finish\"}\n")
}

#[tauri::command]
pub fn power(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    write_session(&state, "upload", false, "power\n")
}

fn write_session(state: &AppState, kind: &str, awaiting: bool, input: &str) -> Result<(), String> {
    let mut active = state
        .active
        .lock()
        .map_err(|_| "active session lock failed")?;
    let session = active.as_mut().ok_or("no matching transfer is active")?;
    if session.kind != kind || (awaiting && !session.awaiting_selection) {
        return Err("the transfer is not waiting for this action".to_string());
    }
    if awaiting {
        session.awaiting_selection = false;
    }
    session
        .child
        .as_mut()
        .and_then(|child| child.stdin.as_mut())
        .ok_or("backend input is unavailable")?
        .write_all(input.as_bytes())
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn cancel(state: State<'_, Arc<AppState>>) {
    stop_active(&state);
}

#[tauri::command]
pub fn download(
    app: AppHandle,
    state: State<'_, Arc<AppState>>,
    paths: State<'_, RuntimePaths>,
    mut options: Value,
) -> Result<(), String> {
    if let Some(object) = options.as_object_mut() {
        object.remove("approvalPrefixPath");
    }
    let paired =
        if !text(&options, "hostId").is_empty() && !text(&options, "systemVersion").is_empty() {
            existing_pairing_prefix(&paths.user_data, &options)?
        } else {
            None
        };
    if let Some(prefix) = paired.clone() {
        options["approvalPrefixPath"] = Value::String(prefix.to_string_lossy().into_owned());
    }
    let output = text(&options, "output");
    let metadata = if text(&options, "metadata").is_empty() {
        PathBuf::from(if output.is_empty() {
            "dt_downloads".to_string()
        } else {
            output
        })
        .join("metadata.db")
    } else {
        PathBuf::from(text(&options, "metadata"))
    };
    if text(&options, "accountId").trim().is_empty() && paired.is_none() && !metadata.exists() {
        return pair(app, state, paths, options);
    }
    let id = state.reserve("download")?;
    let thread_state = state.inner().clone();
    let thread_paths = paths.inner().clone();
    thread::spawn(move || download_worker(app, thread_state, thread_paths, id, options));
    Ok(())
}

fn download_worker(
    app: AppHandle,
    state: Arc<AppState>,
    paths: RuntimePaths,
    id: u64,
    mut options: Value,
) {
    let result = (|| {
        ensure_oracle(&app, &state, &paths, &mut options)?;
        let mut child = python(&paths, "poc.dt_backend")
            .arg("download")
            .arg("-")
            .spawn()
            .map_err(|e| e.to_string())?;
        child
            .stdin
            .as_mut()
            .ok_or("backend input is unavailable")?
            .write_all(
                format!(
                    "{}\n",
                    serde_json::to_string(&options).map_err(|e| e.to_string())?
                )
                .as_bytes(),
            )
            .map_err(|e| e.to_string())?;
        attach_child(&state, id, child)
    })();
    let (stdout, stderr) = match result {
        Ok(handles) => handles,
        Err(error) => {
            finish_session(&app, &state, Some(error));
            return;
        }
    };
    forward_stderr(app.clone(), stderr, "");
    let mut completed = false;
    for line in BufReader::new(stdout).lines().map_while(Result::ok) {
        if !state.is_current(id) {
            return;
        }
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(&line) {
            Ok(event) => {
                let event_type = event
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if let Ok(mut active) = state.active.lock() {
                    if let Some(session) = active.as_mut().filter(|session| session.id == id) {
                        if event_type == "done" {
                            session.completed = true;
                            completed = true;
                        }
                        if event_type == "ready" {
                            session.awaiting_selection = true;
                        }
                    }
                }
                let event = decorate_event(&paths, event);
                send(&app, &event_type, event);
            }
            Err(_) => send(&app, "log", json!({ "message": line })),
        }
    }
    match wait_for_child(&state, id) {
        Ok(code) if code == 0 || completed => finish_session(&app, &state, None),
        Ok(code) => finish_session(
            &app,
            &state,
            Some(format!("download backend exited with status {code}")),
        ),
        Err(_) if completed => finish_session(&app, &state, None),
        Err(error) => finish_session(&app, &state, Some(error)),
    }
}

#[tauri::command]
pub fn pair(
    app: AppHandle,
    state: State<'_, Arc<AppState>>,
    paths: State<'_, RuntimePaths>,
    options: Value,
) -> Result<(), String> {
    let host = text(&options, "clientHost");
    if host.parse::<std::net::Ipv4Addr>().is_err() {
        return Err("Discover and select a PS5 IPv4 address first".to_string());
    }
    let prefix = pairing_prefix_path(&paths.user_data, &options)?;
    if let Some(parent) = prefix.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let id = state.reserve("pair")?;
    send(&app, "pairingStarted", json!({}));
    let thread_state = state.inner().clone();
    let thread_paths = paths.inner().clone();
    thread::spawn(move || pair_worker(app, thread_state, thread_paths, id, options, prefix));
    Ok(())
}

fn pair_worker(
    app: AppHandle,
    state: Arc<AppState>,
    paths: RuntimePaths,
    id: u64,
    mut options: Value,
    prefix: PathBuf,
) {
    let result = (|| {
        ensure_oracle(&app, &state, &paths, &mut options)?;
        let python_bin = paths.python_bin();
        let mut command;
        if cfg!(target_os = "linux") && user_id() != 0 {
            command = Command::new("pkexec");
            command
                .arg("/usr/bin/env")
                .arg(format!(
                    "PYTHONPATH={}",
                    paths.resource_root.to_string_lossy()
                ))
                .arg(format!("DT_PAIRING_UID={}", user_id()))
                .arg(format!("DT_PAIRING_GID={}", group_id()))
                .arg(python_bin)
                .arg("-u")
                .arg("-m")
                .arg("poc.dt_pair")
                .arg("-");
        } else {
            command = python(&paths, "poc.dt_pair");
            command.arg("-");
        }
        let mut child = command
            .current_dir(&paths.work_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| e.to_string())?;
        let input = json!({
            "ps5Ip": text(&options, "clientHost"),
            "hostId": text(&options, "hostId"),
            "systemVersion": text(&options, "systemVersion"),
            "oracleHost": text(&options, "oracleHost"),
            "oraclePort": number(&options, "oraclePort", 9022),
            "prefixPath": prefix.to_string_lossy(),
            "duration": 300,
        });
        child
            .stdin
            .as_mut()
            .ok_or("pairing input is unavailable")?
            .write_all(
                format!(
                    "{}\n",
                    serde_json::to_string(&input).map_err(|e| e.to_string())?
                )
                .as_bytes(),
            )
            .map_err(|e| e.to_string())?;
        attach_child(&state, id, child)
    })();
    let (stdout, stderr) = match result {
        Ok(handles) => handles,
        Err(error) => {
            finish_session(&app, &state, Some(error));
            return;
        }
    };
    forward_stderr(app.clone(), stderr, "[helper] ");
    for line in BufReader::new(stdout).lines().map_while(Result::ok) {
        if !state.is_current(id) {
            return;
        }
        match serde_json::from_str::<Value>(&line) {
            Ok(event) => match event.get("type").and_then(Value::as_str) {
                Some("status") => send(
                    &app,
                    "status",
                    json!({ "message": event.get("message").cloned().unwrap_or_default() }),
                ),
                Some("error") => {
                    if let Ok(mut active) = state.active.lock() {
                        if let Some(session) = active.as_mut().filter(|session| session.id == id) {
                            session.error = Some(text(&event, "message"));
                        }
                    }
                }
                Some("paired") => {
                    let valid = existing_pairing_prefix(&paths.user_data, &options)
                        .ok()
                        .flatten()
                        .is_some();
                    if let Ok(mut active) = state.active.lock() {
                        if let Some(session) = active.as_mut().filter(|session| session.id == id) {
                            if valid {
                                session.completed = true;
                            } else {
                                session.error = Some(
                                    "Pairing helper did not save a valid identifier".to_string(),
                                );
                            }
                        }
                    }
                }
                _ => {}
            },
            Err(_) => {
                if let Ok(mut active) = state.active.lock() {
                    if let Some(session) = active.as_mut().filter(|session| session.id == id) {
                        session.error =
                            Some("Pairing helper sent an unreadable response".to_string());
                    }
                }
            }
        }
    }
    let code = wait_for_child(&state, id).unwrap_or(-1);
    let (completed, error) = state
        .active
        .lock()
        .ok()
        .and_then(|active| {
            active
                .as_ref()
                .map(|session| (session.completed, session.error.clone()))
        })
        .unwrap_or((false, None));
    if completed && code == 0 {
        finish_session(&app, &state, None);
        send(
            &app,
            "paired",
            json!({ "message": "Pairing saved for this PS5. Return to its home screen, then browse saves." }),
        );
    } else {
        finish_session(
            &app,
            &state,
            Some(error.unwrap_or_else(|| format!("Pairing exited with status {code}"))),
        );
    }
}

fn create_metadata(paths: &RuntimePaths, options: &mut Value) -> Result<(), String> {
    let output = text(options, "output");
    fs::create_dir_all(&output).map_err(|e| e.to_string())?;
    let generated_metadata = Path::new(&output).join("generated-metadata.db");
    let generated_manifest = Path::new(&output).join("generated-upload-manifest.json");
    if text(options, "uploadKind") == "external" {
        options["assets"] = Value::String(
            Path::new(&output)
                .join("external-save")
                .to_string_lossy()
                .into_owned(),
        );
    }
    let mut args = vec![
        "--output".to_string(),
        generated_metadata.to_string_lossy().into_owned(),
        "--user-id".to_string(),
        text(options, "userId"),
        "--assets".to_string(),
        text(options, "assets"),
        "--user-name".to_string(),
        if text(options, "userName").is_empty() {
            "user".to_string()
        } else {
            text(options, "userName")
        },
        "--manifest-output".to_string(),
        generated_manifest.to_string_lossy().into_owned(),
    ];
    let source_metadata = if text(options, "metadata").is_empty() {
        Path::new(&output)
            .join("metadata.db")
            .to_string_lossy()
            .into_owned()
    } else {
        text(options, "metadata")
    };
    match text(options, "uploadKind").as_str() {
        "external" => args.extend([
            "--external-image".to_string(),
            text(options, "externalImage"),
            "--title-id".to_string(),
            text(options, "titleId"),
            "--source".to_string(),
            source_metadata,
        ]),
        "other" => args.extend([
            "--other".to_string(),
            "--source".to_string(),
            source_metadata,
        ]),
        _ => args.extend(["--source".to_string(), source_metadata]),
    }
    if let Some(ids) = options.get("entryIds").and_then(Value::as_array) {
        for id in ids {
            args.extend(["--entry-id".to_string(), id.to_string()]);
        }
    }
    command_output(paths, "poc.dt_metadata", &args)?;
    options["metadata"] = Value::String(generated_metadata.to_string_lossy().into_owned());
    options["uploadManifest"] = Value::String(generated_manifest.to_string_lossy().into_owned());
    Ok(())
}

#[tauri::command]
pub fn upload(
    app: AppHandle,
    state: State<'_, Arc<AppState>>,
    paths: State<'_, RuntimePaths>,
    options: Value,
) -> Result<(), String> {
    let id = state.reserve("upload")?;
    let thread_state = state.inner().clone();
    let thread_paths = paths.inner().clone();
    thread::spawn(move || upload_worker(app, thread_state, thread_paths, id, options));
    Ok(())
}

fn absolute(path: &str) -> String {
    fs::canonicalize(path)
        .unwrap_or_else(|_| PathBuf::from(path))
        .to_string_lossy()
        .into_owned()
}

fn upload_worker(
    app: AppHandle,
    state: Arc<AppState>,
    paths: RuntimePaths,
    id: u64,
    mut options: Value,
) {
    let result = (|| {
        create_metadata(&paths, &mut options)?;
        ensure_oracle(&app, &state, &paths, &mut options)?;
        let prefix = pairing_prefix_path(&paths.user_data, &options)?;
        if let Some(parent) = prefix.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let mut env = environment(&paths);
        let output = text(&options, "output");
        for (key, value) in [
            ("DT_METADATA", absolute(&text(&options, "metadata"))),
            ("DT_ASSETS", absolute(&text(&options, "assets"))),
            ("DT_MANIFEST", absolute(&text(&options, "uploadManifest"))),
            (
                "DT_UPLOAD_KIND",
                if text(&options, "uploadKind").is_empty() {
                    "save".to_string()
                } else {
                    text(&options, "uploadKind")
                },
            ),
            (
                "DT_LOG",
                Path::new(&output)
                    .join("dt-source.log")
                    .to_string_lossy()
                    .into_owned(),
            ),
            ("DT_PAIRING_PREFIX", prefix.to_string_lossy().into_owned()),
            ("DT_EXPECTED_HOST_ID", text(&options, "hostId")),
            ("DT_EXIT_ON_EOF", "1".to_string()),
            ("DT_AUTO_POWER", "1".to_string()),
            ("DT_PORT", number(&options, "sourcePort", 1004).to_string()),
            (
                "DT_DATA_PORT",
                number(&options, "dataPort", 1005).to_string(),
            ),
            (
                "DT_DISCOVERY_PORT",
                number(&options, "discoveryPort", 1006).to_string(),
            ),
            (
                "DT_ORACLE_HOST",
                if text(&options, "oracleHost").is_empty() {
                    "127.0.0.1".to_string()
                } else {
                    text(&options, "oracleHost")
                },
            ),
            (
                "DT_ORACLE_PORT",
                number(&options, "oraclePort", 9022).to_string(),
            ),
        ] {
            env.insert(key.to_string(), value);
        }
        if !cfg!(windows) {
            env.insert("DT_PAIRING_UID".to_string(), user_id().to_string());
            env.insert("DT_PAIRING_GID".to_string(), group_id().to_string());
        }
        if !text(&options, "advertiseAddress").is_empty() {
            env.insert(
                "DT_ADVERTISE".to_string(),
                text(&options, "advertiseAddress"),
            );
        }
        if !text(&options, "systemVersion").is_empty() {
            env.insert(
                "DT_SYSTEM_VERSION".to_string(),
                text(&options, "systemVersion"),
            );
        }
        let needs_root = cfg!(target_os = "linux")
            && user_id() != 0
            && [
                number(&options, "sourcePort", 1004),
                number(&options, "dataPort", 1005),
                number(&options, "discoveryPort", 1006),
            ]
            .iter()
            .any(|port| *port < 1024);
        let python_bin = paths.python_bin();
        let mut command;
        if needs_root {
            command = Command::new("pkexec");
            command.arg("/usr/bin/env").arg(format!(
                "PYTHONPATH={}",
                paths.resource_root.to_string_lossy()
            ));
            command.args(env.iter().map(|(key, value)| format!("{key}={value}")));
            command
                .arg(python_bin)
                .arg("-u")
                .arg("-m")
                .arg("poc.dt_source");
            send(
                &app,
                "log",
                json!({ "message": "[privilege] authenticate to bind Data Transfer ports 1004–1006" }),
            );
        } else {
            command = python(&paths, "poc.dt_source");
            command.envs(env);
        }
        command.current_dir(&paths.work_dir);
        let child = command.spawn().map_err(|e| e.to_string())?;
        attach_child(&state, id, child)
    })();
    let (stdout, stderr) = match result {
        Ok(handles) => handles,
        Err(error) => {
            finish_session(&app, &state, Some(error));
            return;
        }
    };
    forward_stderr(app.clone(), stderr, "");
    for line in BufReader::new(stdout).lines().map_while(Result::ok) {
        if !state.is_current(id) {
            return;
        }
        send(&app, "log", json!({ "message": line }));
        if line.contains("type 'power'") {
            send(
                &app,
                "status",
                json!({ "step": 3, "message": "Sending the source power signal automatically in 1 second." }),
            );
        }
        if line.to_lowercase().contains("cmd=0xb3") {
            if let Ok(mut active) = state.active.lock() {
                if let Some(session) = active.as_mut().filter(|session| session.id == id) {
                    session.transfer_finished = true;
                }
            }
            send(
                &app,
                "status",
                json!({ "step": 3, "message": "Save data sent. Waiting for the console to disconnect." }),
            );
        }
        let transfer_finished = state
            .active
            .lock()
            .ok()
            .and_then(|active| active.as_ref().map(|session| session.transfer_finished))
            .unwrap_or(false);
        if transfer_finished && line.to_lowercase().contains("cmd=0x94") {
            if let Ok(mut active) = state.active.lock() {
                if let Some(session) = active.as_mut().filter(|session| session.id == id) {
                    session.completed = true;
                }
            }
            send(
                &app,
                "done",
                json!({ "message": "Save upload completed. The client may reboot to finish." }),
            );
            stop_active(&state);
            return;
        }
    }
    let code = match wait_for_child(&state, id) {
        Ok(code) => code,
        Err(error) => {
            finish_session(&app, &state, Some(error));
            return;
        }
    };
    let (completed, transfer_finished) = state
        .active
        .lock()
        .ok()
        .and_then(|active| {
            active
                .as_ref()
                .map(|session| (session.completed, session.transfer_finished))
        })
        .unwrap_or((false, false));
    if transfer_finished && !completed && code == 0 {
        if let Ok(mut active) = state.active.lock() {
            if let Some(session) = active.as_mut().filter(|session| session.id == id) {
                session.completed = true;
            }
        }
        send(
            &app,
            "done",
            json!({ "message": "Save upload completed. The console closed the transfer session." }),
        );
        finish_session(&app, &state, None);
    } else if !completed && code != 0 {
        finish_session(
            &app,
            &state,
            Some(format!("source emulator exited with status {code}")),
        );
    } else {
        finish_session(&app, &state, None);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn executes_python_backend_commands() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        let paths = RuntimePaths {
            resource_root: root.clone(),
            work_dir: root.clone(),
            user_data: root.clone(),
            config_path: root.join("config.ini"),
            default_downloads: root.join("dt_downloads"),
        };
        let output = command_output(
            &paths,
            "poc.dt_backend",
            &[
                "users".to_string(),
                root.join("poc/templates/single-save.db")
                    .to_string_lossy()
                    .into_owned(),
            ],
        )
        .unwrap();
        assert!(
            output.contains("\"users\""),
            "unexpected backend output: {output}"
        );
    }
}
