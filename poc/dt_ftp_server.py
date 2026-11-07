#!/usr/bin/env python3
import argparse
import re
import socket
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from poc.dt_backend import (
    build_power_request_body,
    discovery_host_id,
    hardware_id,
    power_request_prefix,
)
from poc.dt_download import DtChannel, download_entry, download_metadata, launch_console, discovery_versions


CATEGORY_NAMES = {
    0: "profiles",
    1: "apps",
    2: "media",
    3: "saves",
}


@dataclass(frozen=True)
class Entry:
    id: int
    category: str
    name: str
    source_path: str
    size: int


def safe_name(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip(".")
    return cleaned or "entry"


def ftp_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%d%H%M%S")


def normalize(path):
    parts = []
    for part in PurePosixPath(path).parts:
        if part in ("", "/", "."):
            continue
        if part == "..":
            raise ValueError("parent paths are not allowed")
        parts.append(part)
    return parts


class TransferBackend:
    def __init__(self, options, metadata, output):
        self.options = options
        self.metadata = metadata
        self.output = output
        self.command = None
        self.data = None

    def start(self):
        host = self.options.console_host
        response = launch_console(host, skip_launch=self.options.no_launch)
        protocol, firmware = discovery_versions(response)
        if not self.options.no_power:
            prefix = power_request_prefix(
                self.options.account_id,
                self.metadata if self.metadata.is_file() else None,
                self.options.pairing_prefix,
            )
            if prefix is None:
                raise RuntimeError("an account ID or pairing prefix is required for Data Transfer approval")
            host_id = discovery_host_id(response)
            body = build_power_request_body(
                firmware,
                hardware_id({"hardware": self.options.hardware}, response),
                host_id,
                prefix,
            )
            self.command = DtChannel(
                host, 1004, self.options.oracle_host, self.options.oracle_port, version=protocol
            )
            sub = self.command.request(0x91, body)
            result, _ = self.command.response(0x91, sub)
            if result == 33:
                print("[dt] press the console power button briefly", flush=True)
                result, _ = self.command.response(0x91, sub)
            if result != 0:
                raise RuntimeError(f"Data Transfer approval failed: {result:#x}")
        self.data = DtChannel(
            host, 1005, self.options.oracle_host, self.options.oracle_port, version=protocol
        )
        if not self.metadata.is_file():
            download_metadata(self.data, self.output)

    def download(self, entry):
        if self.data is None:
            raise RuntimeError("Data Transfer is not connected")
        download_entry(self.data, entry.id, self.output)

    def close(self):
        if self.data is not None:
            self.data.close()
        if self.command is not None:
            self.command.close()


class Catalog:
    def __init__(self, metadata, output, backend=None):
        self.output = Path(output)
        self.backend = backend
        self.download_lock = threading.Lock()
        self.entries = {}
        with sqlite3.connect(f"file:{metadata}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT id, path, actual_size, target_type FROM dir_entry_info_tbl ORDER BY id"
            ).fetchall()
        for entry_id, source_path, size, target_type in rows:
            category = CATEGORY_NAMES.get(target_type, "other")
            title = PurePosixPath(source_path.rstrip("/")).name or str(entry_id)
            name = f"{entry_id}_{safe_name(title)}"
            self.entries[(category, name)] = Entry(entry_id, category, name, source_path, size)

    def entry_directory(self, entry):
        return self.output / str(entry.id)

    def ensure_entry(self, entry):
        directory = self.entry_directory(entry)
        if (directory / "manifest.json").is_file():
            return directory
        if self.backend is None:
            raise FileNotFoundError(f"entry {entry.id} is not cached")
        with self.download_lock:
            if not (directory / "manifest.json").is_file():
                self.backend.download(entry)
        return directory

    def list_directory(self, path):
        parts = normalize(path)
        if not parts:
            return sorted(set(CATEGORY_NAMES.values()) | {"other"})
        if parts[0] not in set(CATEGORY_NAMES.values()) | {"other"}:
            raise FileNotFoundError(path)
        if len(parts) == 1:
            category = parts[0]
            return sorted(name for cat, name in self.entries if cat == category)
        entry = self.entries.get((parts[0], parts[1]))
        if entry is None:
            raise FileNotFoundError(path)
        directory = self.ensure_entry(entry)
        if len(parts) == 2:
            return sorted(
                item.name for item in directory.iterdir()
                if item.is_file() and not item.name.endswith(".part")
            )
        raise NotADirectoryError(path)

    def is_directory(self, path):
        parts = normalize(path)
        if not parts:
            return True
        if parts[0] not in set(CATEGORY_NAMES.values()) | {"other"}:
            raise FileNotFoundError(path)
        if len(parts) < 3:
            if len(parts) == 2 and (parts[0], parts[1]) not in self.entries:
                raise FileNotFoundError(path)
            return True
        self.file_path(path)
        return False

    def file_path(self, path):
        parts = normalize(path)
        if len(parts) != 3:
            raise FileNotFoundError(path)
        entry = self.entries.get((parts[0], parts[1]))
        if entry is None:
            raise FileNotFoundError(path)
        directory = self.ensure_entry(entry)
        result = directory / parts[2]
        if not result.is_file() or result.name.endswith(".part"):
            raise FileNotFoundError(path)
        return result


class FtpSession:
    def __init__(self, control, catalog):
        self.control = control
        self.catalog = catalog
        self.cwd = "/"
        self.listener = None

    def reply(self, message):
        self.control.sendall(message.encode("utf-8") + b"\r\n")

    def path(self, value=""):
        if not value or value == ".":
            return self.cwd
        if value.startswith("/"):
            return "/" + "/".join(normalize(value))
        return "/" + "/".join(normalize(self.cwd + "/" + value))

    def open_data(self):
        if self.listener is None:
            raise RuntimeError("no passive listener")
        self.listener.settimeout(15)
        connection, _ = self.listener.accept()
        self.listener.close()
        self.listener = None
        return connection

    def passive(self, extended):
        if self.listener is not None:
            self.listener.close()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.control.getsockname()[0], 0))
        self.listener.listen(1)
        port = self.listener.getsockname()[1]
        if extended:
            self.reply(f"229 Entering Extended Passive Mode (|||{port}|)")
        else:
            address = self.control.getsockname()[0].split(".")
            self.reply(
                "227 Entering Passive Mode (" + ",".join(address + [str(port // 256), str(port % 256)]) + ")"
            )

    def list(self, destination, names, detailed, machine=False):
        self.reply("150 Opening data connection")
        connection = self.open_data()
        with connection:
            for name in names:
                path = destination.rstrip("/") + "/" + name
                is_directory = self.catalog.is_directory(path)
                if machine:
                    if is_directory:
                        line = f"type=dir;modify=19700101000000; {name}\r\n"
                    else:
                        stat = self.catalog.file_path(path).stat()
                        line = f"type=file;size={stat.st_size};modify={ftp_time(stat.st_mtime)}; {name}\r\n"
                elif detailed:
                    mode = "drwxr-xr-x" if is_directory else "-r--r--r--"
                    line = f"{mode} 1 owner group 0 Jan 01 00:00 {name}\r\n"
                else:
                    line = f"{name}\r\n"
                connection.sendall(line.encode("utf-8"))
        self.reply("226 Transfer complete")

    def run(self):
        self.reply("220 GarlicTransfer read-only FTP")
        try:
            while True:
                data = bytearray()
                while not data.endswith(b"\r\n"):
                    chunk = self.control.recv(4096)
                    if not chunk:
                        return
                    data.extend(chunk)
                    if len(data) > 8192:
                        raise ValueError("FTP command too long")
                command = data[:-2].decode("utf-8", "replace")
                print(f"[ftp] {command}", flush=True)
                verb, _, argument = command.partition(" ")
                verb = verb.upper()
                if verb == "USER":
                    self.reply("331 Anonymous login accepted")
                elif verb == "PASS":
                    self.reply("230 Logged in")
                elif verb == "FEAT":
                    self.reply("211-Features:\r\n EPSV\r\n PASV\r\n MLSD\r\n SIZE\r\n MDTM\r\n UTF8\r\n211 End")
                elif verb in ("SYST", "TYPE", "NOOP", "OPTS", "STRU", "MODE", "CLNT", "PBSZ", "PROT"):
                    self.reply("200 OK" if verb != "SYST" else "215 UNIX Type: L8")
                elif verb in ("PWD", "XPWD"):
                    self.reply(f'257 "{self.cwd}"')
                elif verb == "CDUP":
                    self.cwd = "/" + "/".join(normalize(self.cwd)[:-1])
                    self.reply("250 Directory changed")
                elif verb == "CWD":
                    destination = self.path(argument)
                    self.catalog.list_directory(destination)
                    self.cwd = destination
                    self.reply("250 Directory changed")
                elif verb in ("PASV", "EPSV"):
                    self.passive(verb == "EPSV")
                elif verb in ("LIST", "NLST", "MLSD"):
                    target = next((item for item in argument.split() if not item.startswith("-")), "")
                    destination = self.path(target)
                    names = self.catalog.list_directory(destination)
                    self.list(destination, names, verb == "LIST", machine=verb == "MLSD")
                elif verb == "SIZE":
                    path = self.catalog.file_path(self.path(argument))
                    self.reply(f"213 {path.stat().st_size}")
                elif verb == "MDTM":
                    path = self.catalog.file_path(self.path(argument))
                    self.reply(f"213 {ftp_time(path.stat().st_mtime)}")
                elif verb == "REST":
                    self.reply("350 Restart accepted" if argument.upper() in ("0", "STREAM") else "501 Invalid restart point")
                elif verb == "RETR":
                    path = self.catalog.file_path(self.path(argument))
                    self.reply("150 Opening data connection")
                    connection = self.open_data()
                    with connection, path.open("rb") as source:
                        while chunk := source.read(0x200000):
                            connection.sendall(chunk)
                    self.reply("226 Transfer complete")
                elif verb == "ABOR":
                    self.reply("226 No transfer in progress")
                elif verb == "QUIT":
                    self.reply("221 Goodbye")
                    return
                else:
                    self.reply("502 Command not implemented")
        except (OSError, ValueError, RuntimeError) as error:
            try:
                self.reply(f"550 {error}")
            except OSError:
                pass
        finally:
            if self.listener is not None:
                self.listener.close()
            self.control.close()


class FtpServer:
    def __init__(self, host, port, catalog):
        self.catalog = catalog
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((host, port))
        self.socket.listen(20)
        self.socket.settimeout(1)
        self.host, self.port = self.socket.getsockname()
        self.running = False

    def serve_forever(self):
        self.running = True
        while self.running:
            try:
                control, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=FtpSession(control, self.catalog).run, daemon=True).start()

    def close(self):
        self.running = False
        self.socket.close()


def main():
    parser = argparse.ArgumentParser(description="Read-only FTP server for PS5 Data Transfer files")
    parser.add_argument("--host", default="127.0.0.1", help="FTP listen address")
    parser.add_argument("--port", type=int, default=2121)
    parser.add_argument("--console-host", help="PS5 address; omit with --offline")
    parser.add_argument("--oracle-host", default="192.168.0.125")
    parser.add_argument("--oracle-port", type=int, default=9022)
    parser.add_argument("--output", type=Path, default=Path("dt-ftp-cache"))
    parser.add_argument("--metadata", type=Path, help="existing transfer metadata.db")
    parser.add_argument("--account-id")
    parser.add_argument("--pairing-prefix", type=Path)
    parser.add_argument("--hardware", type=int, default=0)
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument("--no-power", action="store_true")
    parser.add_argument("--offline", action="store_true", help="serve cached files only")
    args = parser.parse_args()
    metadata = args.metadata or args.output / "metadata.db"
    backend = None
    try:
        if not args.offline:
            if not args.console_host:
                parser.error("--console-host is required unless --offline is used")
            backend = TransferBackend(args, metadata, args.output)
            backend.start()
        catalog = Catalog(metadata, args.output, backend)
        server = FtpServer(args.host, args.port, catalog)
        print(f"FTP listening on {server.host}:{server.port}; root categories: saves, apps, profiles, media", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if backend is not None:
            backend.close()


if __name__ == "__main__":
    main()
