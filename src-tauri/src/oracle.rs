use crate::paths::RuntimePaths;
use crate::state::{send, AppState};
use serde_json::Value;
use std::fs::File;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};
use tauri::AppHandle;

fn api_base(server_url: &str) -> String {
    server_url.trim_end_matches('/').to_string()
}

fn response_json(response: reqwest::blocking::Response) -> Result<Value, String> {
    let status = response.status();
    let text = response.text().map_err(|e| e.to_string())?;
    let data: Value = serde_json::from_str(&text).map_err(|_| {
        format!(
            "HTOS returned a non-JSON response ({status}): {}",
            &text[..text.len().min(200)]
        )
    })?;
    if !status.is_success() {
        return Err(data
            .get("error")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| format!("HTOS request failed ({status})")));
    }
    Ok(data)
}

fn submit_derivation(
    client: &reqwest::blocking::Client,
    server_url: &str,
    worker_key: &str,
    client_value: &str,
    server_value: &str,
) -> Result<String, String> {
    let form = reqwest::blocking::multipart::Form::new()
        .text("operation", "ppr_derive")
        .text("client_value", client_value.to_string())
        .text("server_value", server_value.to_string());
    let data = response_json(
        client
            .post(format!("{}/api/v1/jobs", api_base(server_url)))
            .header("X-Worker-Key", worker_key)
            .multipart(form)
            .send()
            .map_err(|e| e.to_string())?,
    )?;
    data.get("job_id")
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| "HTOS did not return a job ID".to_string())
}

fn derivation_result(
    app: &AppHandle,
    client: &reqwest::blocking::Client,
    server_url: &str,
    worker_key: &str,
    job_id: &str,
) -> Result<Vec<u8>, String> {
    let deadline = Instant::now() + Duration::from_secs(120);
    loop {
        let data = response_json(
            client
                .get(format!("{}/api/v1/jobs/{job_id}", api_base(server_url)))
                .header("X-Worker-Key", worker_key)
                .send()
                .map_err(|e| e.to_string())?,
        )?;
        for entry in data
            .get("logs")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if let Some(message) = entry.get("msg").and_then(Value::as_str) {
                send(
                    app,
                    "log",
                    serde_json::json!({ "message": format!("[worker] {message}") }),
                );
            }
        }
        match data.get("status").and_then(Value::as_str) {
            Some("done") => break,
            Some("failed") => {
                return Err(data
                    .get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("The volunteer worker failed")
                    .to_string());
            }
            _ if Instant::now() >= deadline => return Err("PPR derivation timed out".to_string()),
            _ => thread::sleep(Duration::from_secs(1)),
        }
    }
    let data = response_json(
        client
            .get(format!(
                "{}/api/v1/jobs/{job_id}/result",
                api_base(server_url)
            ))
            .header("X-Worker-Key", worker_key)
            .send()
            .map_err(|e| e.to_string())?,
    )?;
    let mut result = Vec::new();
    for name in ["key1", "key2", "nonce1", "nonce2"] {
        let value = data
            .get(name)
            .and_then(Value::as_str)
            .ok_or_else(|| format!("Worker result is missing {name}"))?;
        let expected = if name.starts_with("key") { 32 } else { 8 };
        if value.len() != expected || !value.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(format!("Worker returned an invalid {name}"));
        }
        result.extend(hex::decode(value).map_err(|e| e.to_string())?);
    }
    Ok(result)
}

fn remote_socket(app: AppHandle, mut socket: TcpStream, server_url: String, worker_key: String) {
    thread::spawn(move || {
        let result = (|| {
            let mut input = [0u8; 32];
            socket.read_exact(&mut input).map_err(|e| e.to_string())?;
            let client_value = hex::encode(&input[..16]);
            let server_value = hex::encode(&input[16..]);
            let client = reqwest::blocking::Client::builder()
                .timeout(Duration::from_secs(30))
                .build()
                .map_err(|e| e.to_string())?;
            let job_id = submit_derivation(
                &client,
                &server_url,
                &worker_key,
                &client_value,
                &server_value,
            )?;
            send(
                &app,
                "log",
                serde_json::json!({ "message": format!("[oracle] submitted PPR job {job_id}") }),
            );
            derivation_result(&app, &client, &server_url, &worker_key, &job_id)
        })();
        match result {
            Ok(result) => {
                let _ = socket.write_all(&result);
                let _ = socket.shutdown(Shutdown::Write);
            }
            Err(error) => {
                send(
                    &app,
                    "log",
                    serde_json::json!({ "message": format!("[oracle] {error}") }),
                );
                let _ = socket.shutdown(Shutdown::Both);
            }
        }
    });
}

fn start_remote_oracle(
    app: &AppHandle,
    state: &AppState,
    server_url: &str,
    worker_key: &str,
) -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let (stop, stopped) = mpsc::channel();
    *state.relay_stop.lock().map_err(|_| "relay lock failed")? = Some(stop);
    let app = app.clone();
    let server_url = server_url.to_string();
    let worker_key = worker_key.to_string();
    thread::spawn(move || loop {
        if stopped.try_recv().is_ok() {
            break;
        }
        match listener.accept() {
            Ok((socket, _)) => {
                remote_socket(app.clone(), socket, server_url.clone(), worker_key.clone())
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(100));
            }
            Err(_) => break,
        }
    });
    Ok(port)
}

fn option_port(options: &Value) -> u16 {
    options
        .get("oraclePort")
        .and_then(|value| value.as_u64().or_else(|| value.as_str()?.parse().ok()))
        .unwrap_or(9022) as u16
}

fn oracle_ready(host: &str, port: u16) -> bool {
    let address = format!("{host}:{port}");
    address
        .parse()
        .ok()
        .and_then(|address| TcpStream::connect_timeout(&address, Duration::from_millis(1200)).ok())
        .is_some()
}

fn deploy_oracle(
    app: &AppHandle,
    state: &AppState,
    paths: &RuntimePaths,
    host: &str,
) -> Result<(), String> {
    let mut stream = TcpStream::connect((host, 9021)).map_err(|e| e.to_string())?;
    let mut payload = File::open(paths.oracle_elf()).map_err(|e| e.to_string())?;
    std::io::copy(&mut payload, &mut stream).map_err(|e| e.to_string())?;
    send(
        app,
        "log",
        serde_json::json!({ "message": format!("[oracle] deployed to {host}:9021") }),
    );
    let mut reader = BufReader::new(stream.try_clone().map_err(|e| e.to_string())?);
    let log_app = app.clone();
    thread::spawn(move || {
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => send(
                    &log_app,
                    "log",
                    serde_json::json!({ "message": line.trim() }),
                ),
            }
        }
    });
    *state
        .oracle_stream
        .lock()
        .map_err(|_| "oracle lock failed")? = Some(stream);
    Ok(())
}

pub fn ensure_oracle(
    app: &AppHandle,
    state: &AppState,
    paths: &RuntimePaths,
    options: &mut Value,
) -> Result<(), String> {
    if options
        .get("useRemoteOracle")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        let server_url = options
            .get("serverUrl")
            .and_then(Value::as_str)
            .unwrap_or("https://garlicsaves.com");
        let worker_key = options
            .get("workerKey")
            .and_then(Value::as_str)
            .unwrap_or("");
        if worker_key.trim().is_empty() {
            return Err(
                "no server key set — check Oracle settings or disable the remote oracle"
                    .to_string(),
            );
        }
        send(
            app,
            "log",
            serde_json::json!({ "message": format!("[oracle] using volunteer workers through {server_url}") }),
        );
        let port = start_remote_oracle(app, state, server_url, worker_key)?;
        options["oracleHost"] = Value::String("127.0.0.1".to_string());
        options["oraclePort"] = Value::from(port);
        return Ok(());
    }

    let host = options
        .get("oracleHost")
        .and_then(Value::as_str)
        .unwrap_or("192.168.0.125")
        .to_string();
    let port = option_port(options);
    if oracle_ready(&host, port) {
        send(
            app,
            "log",
            serde_json::json!({ "message": format!("[oracle] using {host}:{port}") }),
        );
        return Ok(());
    }
    deploy_oracle(app, state, paths, &host)?;
    let deadline = Instant::now() + Duration::from_secs(8);
    while Instant::now() < deadline {
        if oracle_ready(&host, port) {
            send(
                app,
                "log",
                serde_json::json!({ "message": "[oracle] ready" }),
            );
            return Ok(());
        }
        thread::sleep(Duration::from_millis(200));
    }
    Err("oracle did not open port 9022; check the jailbroken console".to_string())
}
