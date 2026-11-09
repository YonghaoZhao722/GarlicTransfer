#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import socket
import sqlite3
import struct
import time
from pathlib import Path

from poc.dt_ppr_client import derive_with_oracle
from poc.dt_ppr_power import CAPTURED_BODY
from poc.dt_psk_client import command_header, derive_keys, recv_exact, recv_record, send_record

SRCH = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030000\n"
LAUNCH4 = b"LAUNCH4 * HTTP/1.1\ndevice-discovery-protocol-version:00030000\n"


def launch_console(host, skip_launch=False):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        sock.sendto(SRCH, (host, 1006))
        response, _ = sock.recvfrom(4096)
        if not response.startswith(b"HTTP/1.1 200"):
            raise ValueError(f"discovery failed: {response!r}")
        discovery_response = response
        print(f"[ddp] {response.decode(errors='replace').strip()}")
        if skip_launch:
            print("[ddp] reusing the existing Data Transfer listener")
            return discovery_response
        sock.sendto(LAUNCH4, (host, 1006))
        try:
            response, _ = sock.recvfrom(4096)
        except socket.timeout:
            print("[ddp] LAUNCH4 timed out; trying the existing Data Transfer listener")
            return discovery_response
        if not response.startswith((b"HTTP/1.1 200", b"HTTP/1.1 620")):
            raise ValueError(f"launch failed: {response!r}")
        print(f"[ddp] {response.decode(errors='replace').strip()}")
        return discovery_response + response


def discovery_versions(response):
    for line in response.splitlines():
        if line.startswith(b"system-version:"):
            firmware = line.split(b":", 1)[1].decode()
            if len(firmware) != 8 or any(c not in "0123456789abcdefABCDEF" for c in firmware):
                break
            return int(f"{firmware[:2]}0", 16), int(firmware, 16)
    raise ValueError("discovery response has no valid system-version")


class DtChannel:
    def __init__(self, host, port, oracle_host, oracle_port, local_psk=False, version=0x130):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(310)
        client_value = os.urandom(16)
        self.sock.sendall(client_value + bytes(48))
        server_value = recv_exact(self.sock, 64)[:16]
        if local_psk:
            if ":" not in host:
                raise ValueError("local PSK requires an IPv6 channel")
            self.send_key, self.recv_key, self.send_nonce, self.recv_nonce = derive_keys(
                client_value, server_value
            )
        else:
            self.send_key, self.recv_key, self.send_nonce, self.recv_nonce = derive_with_oracle(
                oracle_host, oracle_port, client_value, server_value
            )
        send_record(self.sock, self.send_key, self.send_nonce, 0, b"")
        self.send_sequence = 1
        self.recv_sequence = 0
        self.sub = 1
        self.version = version

    def close(self):
        self.sock.close()

    def request(self, cmd, body=b""):
        sub = self.sub
        self.sub = (self.sub + 1) & 0xFF
        send_record(self.sock, self.send_key, self.send_nonce, self.send_sequence,
                    command_header(cmd, len(body), sub=sub, version=self.version))
        self.send_sequence += 1
        for offset in range(0, len(body), 0x200000):
            send_record(self.sock, self.send_key, self.send_nonce, self.send_sequence,
                        body[offset:offset + 0x200000])
            self.send_sequence += 1
        return sub

    def recv_body(self, size):
        body = bytearray()
        while len(body) < size:
            chunk = recv_record(self.sock, self.recv_key, self.recv_nonce, self.recv_sequence)
            self.recv_sequence += 1
            if len(chunk) > size - len(body):
                raise ValueError(f"body exceeds declared size: {len(body) + len(chunk)} > {size}")
            body.extend(chunk)
        return bytes(body)

    def response(self, cmd, sub):
        header = recv_record(self.sock, self.recv_key, self.recv_nonce, self.recv_sequence)
        self.recv_sequence += 1
        if len(header) != 16:
            raise ValueError(f"unexpected response record: {len(header)} bytes")
        response_cmd, response_sub, _, result, size = struct.unpack("<BBHIQ", header)
        if (response_cmd, response_sub) != (cmd, sub):
            raise ValueError(f"response mismatch: {response_cmd:#x}/{response_sub} != {cmd:#x}/{sub}")
        return result, self.recv_body(size)


def download_metadata(channel, output):
    sub = channel.request(0xA0, struct.pack("<Q", 11))
    deadline = time.monotonic() + 120
    while True:
        result, _ = channel.response(0xA0, sub)
        if result == 0:
            break
        if result != 0x23:
            raise ValueError(f"A0 failed: {result:#x}")
        if time.monotonic() >= deadline:
            raise ValueError("A0 did not complete")
        time.sleep(0.25)

    sub = channel.request(0xB2, struct.pack("<II", 1, 1))
    result, metadata = channel.response(0xB2, sub)
    if result != 0:
        raise ValueError(f"metadata request failed: {result:#x}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "metadata.db").write_bytes(metadata)
    print(f"[metadata] {len(metadata)} bytes -> {output / 'metadata.db'}")


def download_entry(channel, entry_id, output, package_only=False):
    sub = channel.request(0xB2, struct.pack("<II", 2, entry_id))
    result, _ = channel.response(0xB2, sub)
    if result != 0:
        raise ValueError(f"entry {entry_id} request failed: {result:#x}")

    entry_output = output / str(entry_id)
    entry_output.mkdir(parents=True, exist_ok=True)
    files = []
    while True:
        header = recv_record(channel.sock, channel.recv_key, channel.recv_nonce, channel.recv_sequence)
        channel.recv_sequence += 1
        if len(header) != 1304 or struct.unpack_from("<I", header)[0] != 0x35504647:
            raise ValueError(f"invalid GFP5 header: {len(header)} bytes")
        file_id = struct.unpack_from("<I", header, 4)[0]
        if file_id != entry_id:
            raise ValueError(f"file ID mismatch: {file_id} != {entry_id}")
        if header[274]:
            break

        source_path = header[276:1300].split(b"\0", 1)[0].decode(errors="strict")
        size = struct.unpack_from("<Q", header, 240)[0]
        name = Path(source_path).name
        if not name:
            raise ValueError(f"invalid source path: {source_path!r}")
        keep = not package_only or source_path.lower().endswith(".pkg")
        destination = entry_output / name
        partial = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        remaining = size
        print(f"[file] {source_path} ({size} bytes)")
        with (partial.open("wb") if keep else open(os.devnull, "wb")) as stream:
            while remaining:
                chunk = recv_record(channel.sock, channel.recv_key, channel.recv_nonce, channel.recv_sequence)
                channel.recv_sequence += 1
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                stream.write(chunk)
                if keep:
                    digest.update(chunk)
                remaining -= len(chunk)
        if not keep:
            print(f"[file] ignored non-PKG payload {source_path}")
            continue
        partial.replace(destination)
        files.append({
            "path": source_path,
            "file": name,
            "size": size,
            "sha256": digest.hexdigest(),
            "header": header.hex(),
        })
        print(f"[file] saved {destination} sha256={digest.hexdigest()}")

    (entry_output / "manifest.json").write_text(json.dumps({
        "entry_id": entry_id,
        "files": files,
        str(entry_id): {
            "files": files,
            "final_header": header.hex(),
        },
    }, indent=2) + "\n")
    print(f"[done] entry {entry_id}: {len(files)} files; no restore commands sent")


def browse_entries(metadata_path):
    db = sqlite3.connect(metadata_path)
    rows = db.execute(
        "SELECT id, path, actual_size FROM dir_entry_info_tbl WHERE target_type = 3 ORDER BY id"
    ).fetchall()
    db.close()
    print("[browser] save entries:")
    for entry_id, path, size in rows:
        parts = path.strip("/").split("/")
        owner = parts[2] if len(parts) > 2 else "unknown"
        title = Path(path.rstrip("/")).name
        print(f"  {entry_id:4d}  {title:12}  {size / 0x100000:9.1f} MiB  {owner}")
    choice = input("[browser] entry ID to download, or q to quit: ").strip()
    if choice.lower() in ("", "q", "quit"):
        return None
    return int(choice, 0)


def main():
    parser = argparse.ArgumentParser(description="Download PS5 Data Transfer metadata and save containers")
    parser.add_argument("host", help="source PS5 address")
    parser.add_argument("--channel-host", help="optional IPv6 address for the encrypted channel")
    parser.add_argument("--oracle-host", default="192.168.0.125")
    parser.add_argument("--oracle-port", type=int, default=9022)
    parser.add_argument("--entry-id", type=lambda value: int(value, 0),
                        help="directory entry ID to download; omit for metadata only")
    parser.add_argument("--browse", action="store_true", help="list save entries and prompt for a download ID")
    parser.add_argument("--out", type=Path, default=Path("dt-downloads"))
    parser.add_argument("--no-launch", action="store_true", help="skip DDP SRCH/LAUNCH4")
    parser.add_argument("--no-power", action="store_true", help="skip 0x91 when already in Data Transfer mode")
    parser.add_argument("--command-port", type=int, default=1004)
    parser.add_argument("--data-port", type=int, default=1005)
    parser.add_argument("--hardware", type=int, default=3)
    parser.add_argument("--local-psk", action="store_true", help="use the separate local PSK variant instead of the PPR oracle")
    args = parser.parse_args()

    if not args.no_launch:
        launch_console(args.host)

    channel_host = args.channel_host or args.host
    command = None
    data = None
    try:
        if not args.no_power:
            body = bytearray(CAPTURED_BODY)
            struct.pack_into("<I", body, 44, args.hardware)
            command = DtChannel(channel_host, args.command_port, args.oracle_host, args.oracle_port, args.local_psk)
            sub = command.request(0x91, bytes(body))
            result, _ = command.response(0x91, sub)
            if result == 33:
                print("[power] press the source console power button briefly to enter Data Transfer mode")
                result, _ = command.response(0x91, sub)
            if result != 0:
                raise ValueError(f"power request failed: {result:#x}")

        data = DtChannel(channel_host, args.data_port, args.oracle_host, args.oracle_port, args.local_psk)
        download_metadata(data, args.out)
        entry_id = args.entry_id
        if args.browse:
            entry_id = browse_entries(args.out / "metadata.db")
        if entry_id is not None:
            download_entry(data, entry_id, args.out)
    finally:
        if data is not None:
            data.close()
        if command is not None:
            command.close()


if __name__ == "__main__":
    main()
