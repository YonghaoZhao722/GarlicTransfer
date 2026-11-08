use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::net::Ipv4Addr;
use std::path::{Path, PathBuf};
use tauri::{AppHandle, Manager};

const FIELDS: &[&str] = &[
    "useRemoteOracle",
    "serverUrl",
    "workerKey",
    "oracleHost",
    "oraclePort",
    "interface",
    "hardware",
    "output",
    "metadata",
    "assets",
    "userId",
    "titleId",
    "entryId",
    "userName",
    "sourcePort",
    "dataPort",
    "discoveryPort",
    "preferIpv6",
];

#[derive(Clone, Debug)]
pub struct RuntimePaths {
    pub resource_root: PathBuf,
    pub work_dir: PathBuf,
    pub user_data: PathBuf,
    pub config_path: PathBuf,
    pub default_downloads: PathBuf,
}

impl RuntimePaths {
    pub fn new(app: &AppHandle) -> Result<Self, String> {
        let user_data = app.path().app_data_dir().map_err(|e| e.to_string())?;
        if cfg!(debug_assertions) {
            let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .ok_or("cannot locate project root")?
                .to_path_buf();
            return Ok(Self {
                resource_root: root.clone(),
                work_dir: root.clone(),
                user_data,
                config_path: root.join("config.ini"),
                default_downloads: root.join("dt_downloads"),
            });
        }
        let resource_root = app.path().resource_dir().map_err(|e| e.to_string())?;
        let documents = app.path().document_dir().map_err(|e| e.to_string())?;
        Ok(Self {
            resource_root,
            work_dir: user_data.clone(),
            config_path: user_data.join("config.ini"),
            user_data,
            default_downloads: documents.join("GarlicTransfer"),
        })
    }

    pub fn python_bin(&self) -> PathBuf {
        if cfg!(windows) {
            let bundled = self.resource_root.join("python").join("python.exe");
            if bundled.is_file() {
                return bundled;
            }
            return PathBuf::from("python");
        }
        if cfg!(target_os = "macos") {
            let bundled = self
                .resource_root
                .join("python")
                .join("bin")
                .join("python3");
            if bundled.is_file() {
                return bundled;
            }
        }
        PathBuf::from("python3")
    }

    pub fn oracle_elf(&self) -> PathBuf {
        self.resource_root
            .join("poc")
            .join("ncdt_oracle")
            .join("ncdt_oracle_server.elf")
    }
}

pub fn parse_ini(text: &str) -> HashMap<String, String> {
    let mut result = HashMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with(';') || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if FIELDS.contains(&key) {
            result.insert(key.to_string(), value.trim().to_string());
        }
    }
    result
}

pub fn load_settings(paths: &RuntimePaths) -> Result<Value, String> {
    let defaults = json!({
        "useRemoteOracle": true,
        "serverUrl": "https://garlicsaves.com",
        "workerKey": "d398596b0dd4ad811173e3324a94d1cc90246a1c7f75b405a5696eed200b353e",
        "oracleHost": "192.168.0.125",
        "oraclePort": 9022,
        "interface": "wlp4s0",
        "hardware": 0,
        "output": paths.default_downloads.to_string_lossy(),
        "metadata": "",
        "assets": paths.default_downloads.to_string_lossy(),
        "userId": "",
        "titleId": "",
        "entryId": 137,
        "userName": "user",
        "sourcePort": 1004,
        "dataPort": 1005,
        "discoveryPort": 1006,
        "preferIpv6": false,
    });
    let mut result = defaults.as_object().cloned().unwrap_or_default();
    let stored = match fs::read_to_string(&paths.config_path) {
        Ok(text) => parse_ini(&text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => HashMap::new(),
        Err(error) => return Err(error.to_string()),
    };
    for (key, value) in &stored {
        result.insert(key.clone(), Value::String(value.clone()));
    }
    for key in [
        "oraclePort",
        "hardware",
        "entryId",
        "sourcePort",
        "dataPort",
        "discoveryPort",
    ] {
        let default = defaults.get(key).and_then(Value::as_i64).unwrap_or(0);
        let value = stored
            .get(key)
            .and_then(|v| v.parse::<i64>().ok())
            .unwrap_or(default);
        result.insert(key.to_string(), Value::from(value));
    }
    result.insert(
        "useRemoteOracle".to_string(),
        Value::from(stored.get("useRemoteOracle").map_or(true, |v| v == "true")),
    );
    result.insert(
        "preferIpv6".to_string(),
        Value::from(stored.get("preferIpv6").is_some_and(|v| v == "true")),
    );
    if stored.get("workerKey").is_none_or(|v| v.trim().is_empty()) {
        result.insert("workerKey".to_string(), defaults["workerKey"].clone());
    }
    Ok(Value::Object(result))
}

pub fn save_settings(paths: &RuntimePaths, settings: &Value) -> Result<(), String> {
    let object = settings.as_object().ok_or("settings must be an object")?;
    let mut lines = vec!["; GarlicTransfer settings".to_string()];
    for field in FIELDS {
        let value = match object.get(*field) {
            Some(Value::Bool(value)) => value.to_string(),
            Some(Value::Number(value)) => value.to_string(),
            Some(Value::String(value)) => value.clone(),
            Some(Value::Null) | None => String::new(),
            Some(_) => return Err(format!("invalid value for {field}")),
        };
        lines.push(format!("{field}={value}"));
    }
    if let Some(parent) = paths.config_path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    fs::write(&paths.config_path, format!("{}\n", lines.join("\n"))).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&paths.config_path, fs::Permissions::from_mode(0o600))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn valid_hex(value: &str, length: usize) -> bool {
    value.len() == length && value.bytes().all(|b| b.is_ascii_hexdigit())
}

pub fn pairing_prefix_path(user_data: &Path, identity: &Value) -> Result<PathBuf, String> {
    let host_id = identity
        .get("hostId")
        .and_then(Value::as_str)
        .filter(|value| valid_hex(value, 12))
        .ok_or("Discover a console with a valid host ID before pairing")?;
    let firmware = identity
        .get("systemVersion")
        .and_then(Value::as_str)
        .filter(|value| valid_hex(value, 8))
        .ok_or("Discover the console firmware before pairing")?;
    Ok(user_data
        .join("pairings")
        .join(format!(
            "{}-{}",
            host_id.to_uppercase(),
            firmware.to_uppercase()
        ))
        .join("pairing-prefix.bin"))
}

pub fn existing_pairing_prefix(
    user_data: &Path,
    identity: &Value,
) -> Result<Option<PathBuf>, String> {
    let path = pairing_prefix_path(user_data, identity)?;
    match fs::metadata(&path) {
        Ok(metadata) if metadata.is_file() && metadata.len() == 32 => Ok(Some(path)),
        Ok(_) => Ok(None),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

fn ipv4_number(address: Ipv4Addr) -> u32 {
    u32::from_be_bytes(address.octets())
}

fn broadcast_address(address: Ipv4Addr, netmask: Ipv4Addr) -> Ipv4Addr {
    Ipv4Addr::from((ipv4_number(address) & ipv4_number(netmask)) | !ipv4_number(netmask))
}

pub fn discovery_routes(target_host: Option<&str>) -> Result<Value, String> {
    if let Some(target) = target_host.filter(|value| value.parse::<Ipv4Addr>().is_ok()) {
        return Ok(json!([{ "bind": "", "target": target }]));
    }
    let interfaces = if_addrs::get_if_addrs().map_err(|e| e.to_string())?;
    let mut seen = HashSet::new();
    let mut routes = Vec::new();
    for interface in interfaces {
        if interface.is_loopback() {
            continue;
        }
        let if_addrs::IfAddr::V4(address) = interface.addr else {
            continue;
        };
        let target = broadcast_address(address.ip, address.netmask).to_string();
        let bind = address.ip.to_string();
        if seen.insert(format!("{bind}/{target}")) {
            routes.push(json!({ "bind": bind, "target": target }));
        }
    }
    if routes.is_empty() {
        routes.push(json!({ "bind": "", "target": "255.255.255.255" }));
    }
    Ok(Value::Array(routes))
}

pub fn title_name(resource_root: &Path, title_id: &str) -> String {
    let wanted = title_id.to_uppercase();
    for filename in ["PS5_Titles.tsv", "PS4_Titles.tsv"] {
        let Ok(contents) = fs::read_to_string(resource_root.join("data").join(filename)) else {
            continue;
        };
        for line in contents.lines().skip(1) {
            let mut fields = line.splitn(3, '\t');
            let Some(id) = fields.next() else { continue };
            let _ = fields.next();
            let Some(name) = fields.next() else { continue };
            if id
                .split('_')
                .next()
                .unwrap_or(id)
                .eq_ignore_ascii_case(&wanted)
            {
                return name.to_string();
            }
        }
    }
    "Unknown title".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_only_supported_settings() {
        let parsed = parse_ini("serverUrl=https://example.test\nunknown=value\n; comment\n");
        assert_eq!(parsed.get("serverUrl").unwrap(), "https://example.test");
        assert!(!parsed.contains_key("unknown"));
    }

    #[test]
    fn pairing_identity_is_validated() {
        let identity = json!({"hostId": "5c843cc6d727", "systemVersion": "01360000"});
        let path = pairing_prefix_path(Path::new("test"), &identity).unwrap();
        assert!(path.to_string_lossy().contains("5C843CC6D727-01360000"));
        assert!(pairing_prefix_path(
            Path::new("test"),
            &json!({"hostId": "../bad", "systemVersion": "01360000"})
        )
        .is_err());
    }

    #[test]
    fn computes_broadcast_address() {
        assert_eq!(
            broadcast_address(
                Ipv4Addr::new(192, 168, 1, 20),
                Ipv4Addr::new(255, 255, 255, 0)
            ),
            Ipv4Addr::new(192, 168, 1, 255)
        );
    }
}
