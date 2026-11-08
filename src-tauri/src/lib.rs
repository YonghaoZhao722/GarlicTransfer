mod oracle;
mod paths;
mod process;
mod state;

use paths::{
    existing_pairing_prefix, load_settings, save_settings as write_settings, RuntimePaths,
};
use serde_json::Value;
use state::{stop_active, AppState};
use std::sync::Arc;
use tauri::{Manager, State};

#[tauri::command]
fn get_settings(paths: State<'_, RuntimePaths>) -> Result<Value, String> {
    load_settings(&paths)
}

#[tauri::command]
fn save_settings(paths: State<'_, RuntimePaths>, settings: Value) -> Result<(), String> {
    write_settings(&paths, &settings)
}

#[tauri::command]
fn app_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[tauri::command]
fn pairing_status(paths: State<'_, RuntimePaths>, identity: Value) -> Result<bool, String> {
    if identity
        .get("hostId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .is_empty()
        || identity
            .get("systemVersion")
            .and_then(Value::as_str)
            .unwrap_or("")
            .is_empty()
    {
        return Ok(false);
    }
    Ok(existing_pairing_prefix(&paths.user_data, &identity)?.is_some())
}

#[tauri::command]
fn run_firewall_helper() -> Result<(), String> {
    if !cfg!(windows) {
        return Err("the firewall helper is only available on Windows".to_string());
    }
    #[cfg(windows)]
    {
        let helper = if cfg!(debug_assertions) {
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .ok_or("cannot locate project root")?
                .join("Garlic-Firewall-Helper.bat")
        } else {
            std::env::current_exe()
                .map_err(|e| e.to_string())?
                .parent()
                .ok_or("cannot locate GarlicTransfer.exe")?
                .join("Garlic-Firewall-Helper.bat")
        };
        if !helper.is_file() {
            return Err(
                "Garlic-Firewall-Helper.bat is missing beside GarlicTransfer.exe".to_string(),
            );
        }
        std::process::Command::new(
            std::env::var("ComSpec").unwrap_or_else(|_| "cmd.exe".to_string()),
        )
        .args(["/d", "/s", "/c", "start", ""])
        .arg(&helper)
        .arg("/attach")
        .arg(std::process::id().to_string())
        .current_dir(helper.parent().unwrap())
        .spawn()
        .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[allow(unreachable_code)]
    Err("the firewall helper is only available on Windows".to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let paths = RuntimePaths::new(&app.handle())?;
            app.manage(paths);
            app.manage(Arc::new(AppState::default()));
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed) {
                stop_active(&window.state::<Arc<AppState>>());
            }
        })
        .invoke_handler(tauri::generate_handler![
            app_version,
            get_settings,
            save_settings,
            pairing_status,
            run_firewall_helper,
            process::discover,
            process::users,
            process::inventory,
            process::download,
            process::pair,
            process::upload,
            process::select,
            process::finish,
            process::power,
            process::cancel,
        ])
        .run(tauri::generate_context!())
        .expect("error while running GarlicTransfer");
}
