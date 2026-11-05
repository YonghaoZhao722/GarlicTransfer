#!/usr/bin/env python3
import argparse
import ipaddress
import json
import os
import socket
import sqlite3
import struct
import sys
import threading
from pathlib import Path

from poc.dt_metadata import validate_payload
from poc.dt_ppr_client import derive_with_oracle
from poc.dt_psk_client import derive_keys, recv_exact, recv_record, send_record


class Tee:
    def __init__(self, path):
        self.stream = open(path, "w", buffering=1)

    def write(self, data):
        sys.__stdout__.write(data)
        self.stream.write(data)
        return len(data)

    def flush(self):
        sys.__stdout__.flush()
        self.stream.flush()


COMMAND_STATE = {"lock": threading.Lock(), "active": False, "sequence": 0}
POWER_EVENT = threading.Event()


def submit_packet(plaintext):
    with COMMAND_STATE["lock"]:
        if not COMMAND_STATE["active"]:
            print("[dt] no active command session")
            return
        sequence = COMMAND_STATE["sequence"]
        send_record(
            COMMAND_STATE["sock"],
            COMMAND_STATE["key"],
            COMMAND_STATE["nonce"],
            sequence,
            plaintext,
        )
        COMMAND_STATE["sequence"] += 1
        print(f"[dt] submitted {len(plaintext)} bytes; next sequence {sequence + 1}")


def send_command_record(sock, key, nonce, sequence, plaintext):
    with COMMAND_STATE["lock"]:
        send_record(sock, key, nonce, sequence, plaintext)
        COMMAND_STATE["sequence"] = sequence + 1


def interactive_console():
    while True:
        try:
            line = input("[dt] ").strip()
        except EOFError:
            if os.environ.get("DT_EXIT_ON_EOF") == "1":
                os._exit(0)
            return
        if line == "status":
            print(f"[dt] active={COMMAND_STATE['active']} next_sequence={COMMAND_STATE['sequence']} sub={COMMAND_STATE.get('sub')}")
        elif line == "metadata":
            print(f"[dt] using {METADATA_PATH}")
        elif line == "klog":
            path = Path("klog.txt")
            if path.exists():
                lines = [x for x in path.read_text(errors="replace").splitlines() if "DATA TRANSFER" in x]
                for entry in lines[-40:]:
                    print(entry)
        elif line == "power":
            POWER_EVENT.set()
            print("[dt] power-button simulation signalled")
        elif line.startswith("submit "):
            try:
                submit_packet(bytes.fromhex(line.split(maxsplit=1)[1]))
            except ValueError as exc:
                print(f"[dt] invalid hex: {exc}")
        elif line.startswith("respond "):
            try:
                parts = line.split()
                cmd = int(parts[1], 0)
                result = int(parts[2], 0)
                body = bytes.fromhex(parts[3]) if len(parts) > 3 else b""
                version = int(parts[4], 0) if len(parts) > 4 else 0x130
                sub = int(parts[5], 0) if len(parts) > 5 else COMMAND_STATE.get("sub", 2)
                submit_packet(struct.pack("<BBHIQ", cmd, sub, version, result, len(body)) + body)
            except (ValueError, IndexError) as exc:
                print(f"[dt] usage: respond <cmd> <result> [body hex] [version] ({exc})")
        elif line in ("quit", "exit"):
            os._exit(0)
        elif line:
            print("[dt] commands: status, metadata, klog, power, submit <hex>, respond <cmd> <result> [body] [version], quit")


HOST_ID = b"020000000001"
HOST_NAME = b"GarlicTransfer"
SYSTEM_VERSION = os.environ.get("DT_SYSTEM_VERSION", "14200007").encode()
METADATA_PATH = os.environ.get("DT_METADATA", "poc/dt-crypto/metadata-125-only137.db")
# The address we tell the console to fetch save data from.  This must be THIS
# PC's address on the console's network, so it is detected from the discovery
# packet that arrives (see discovery_server) unless it is pinned with
# --advertise-address / DT_ADVERTISE.  The literal below is only a last-resort
# fallback for a machine with no usable route.
DATA_ADDRESS = bytes.fromhex("00000000000000000000ffffc0a8006f")
ADDRESS_PINNED = False
COMMAND_PORT = int(os.environ.get("DT_PORT", "1004"))
DATA_PORT = int(os.environ.get("DT_DATA_PORT", str(COMMAND_PORT + 1)))


def local_ipv4_toward(peer):
    """The local IPv4 the OS would use to reach `peer` ('' if it cannot tell)."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((peer, 9))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return ""


def ipv4_mapped(ip):
    """An IPv4 string -> the 16-byte IPv4-mapped form the DDP host-address uses."""
    try:
        return ipaddress.IPv6Address(f"::ffff:{ip}").packed
    except (ValueError, ipaddress.AddressValueError):
        return None


def packed_address(text):
    """Accept an IPv4 or IPv6 literal and return the 16 bytes DDP expects."""
    addr = ipaddress.ip_address(text)
    if addr.version == 4:
        return ipaddress.IPv6Address(f"::ffff:{addr}").packed
    return addr.packed


def derive_session(sock, oracle_host, oracle_port, client_value, server_value):
    if sock.family == socket.AF_INET6:
        return derive_keys(client_value, server_value)
    return derive_with_oracle(oracle_host, oracle_port, client_value, server_value)


def discovery_server(port, ipv6=False):
    global DATA_ADDRESS
    family = socket.AF_INET6 if ipv6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("::" if ipv6 else "0.0.0.0", port))
    print(f"[ddp] listening on UDP {port}", flush=True)
    while True:
        data, address = sock.recvfrom(4096)
        command = data.split(b" ", 1)[0]
        print(f"[ddp] {address[0]}: {command.decode(errors='replace')}", flush=True)
        if command not in (b"SRCH", b"LAUNCH4"):
            continue
        # Advertise the interface this console actually reaches us on.  The
        # LAUNCH4 host-address (and the device-info body) is where the console
        # will open its data connection, so a stale or foreign address makes
        # discovery look fine and the transfer die immediately afterwards.
        if not ADDRESS_PINNED and not ipv6:
            detected = local_ipv4_toward(address[0])
            mapped = ipv4_mapped(detected) if detected else None
            if mapped and mapped != DATA_ADDRESS:
                DATA_ADDRESS = mapped
                print(f"[ddp] advertising data address {detected} (route toward "
                      f"{address[0]}), data port {DATA_PORT}", flush=True)
        if command == b"SRCH":
            response = (
                b"HTTP/1.1 200 Ok\n"
                b"host-id:" + HOST_ID + b"\n"
                b"host-request-port:" + str(COMMAND_PORT).encode() + b"\n"
                b"device-discovery-protocol-version:00030010\n"
                b"host-type:PS5\n"
                b"host-name:" + HOST_NAME + b"\n"
                b"system-version:" + SYSTEM_VERSION + b"\n"
            )
        else:
            response = (
                b"HTTP/1.1 200 Ok\n"
                b"host-id:" + HOST_ID + b"\n"
                b"host-request-port:" + str(COMMAND_PORT).encode() + b"\n"
                b"device-discovery-protocol-version:00030010\n"
                b"host-network:0\n"
                b"host-address:" + DATA_ADDRESS.hex().encode() + b"\n"
            )
        sock.sendto(response, address)


def console_info(hardware, product_code, np_env, version_a, version_b):
    body = bytearray(72)
    body[:6] = bytes.fromhex(HOST_ID.decode())
    body[6] = 1
    struct.pack_into("<I", body, 8, hardware)
    struct.pack_into("<I", body, 12, product_code)
    struct.pack_into("<H", body, 16, 1)
    struct.pack_into("<H", body, 18, 1)
    body[20:20 + len(np_env)] = np_env
    struct.pack_into("<I", body, 32, version_a)
    struct.pack_into("<I", body, 40, 1)
    struct.pack_into("<I", body, 44, version_b)
    body[48:64] = DATA_ADDRESS
    return bytes(body)


def recv_body(sock, key, nonce_prefix, sequence, size):
    body = bytearray()
    while len(body) < size:
        chunk = recv_record(sock, key, nonce_prefix, sequence)
        if not chunk:
            raise ValueError("empty body chunk")
        body.extend(chunk)
        sequence += 1
    return bytes(body), sequence


def send_save_chunks(sock, key, nonce, sequence, file_type, file_id):
    root = Path(os.environ.get("DT_ASSETS", "poc/dt-crypto/extracted"))
    manifest_path = Path(os.environ.get("DT_MANIFEST", str(root / "manifest.json")))
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    save = manifest.get(str(file_id), {})
    entries = save.get("files", []) if file_type in (2, 3) else []
    assets = []
    for entry in entries:
        candidates = (root / str(file_id) / entry["file"], root / entry["file"])
        path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
        assets.append((entry, path))
    if assets and all(path.is_file() for _, path in assets):
        for entry, path in assets:
            validate_payload(path, file_id, entry)
            print(f"[data] GFP5 id={file_id} type={file_type} path={entry['path']} size={entry['size']}", flush=True)
            send_record(sock, key, nonce, sequence, bytes.fromhex(entry["header"]))
            sequence += 1
            if file_type == 2:
                sent = 0
                try:
                    with path.open("rb") as stream:
                        while chunk := stream.read(0x200000):
                            send_record(sock, key, nonce, sequence, chunk)
                            sequence += 1
                            sent += len(chunk)
                            print(f"[data] GFP5 chunk id={file_id} offset={sent} bytes={len(chunk)}", flush=True)
                    print(f"[data] GFP5 file complete id={file_id} bytes={sent}", flush=True)
                except Exception as exc:
                    print(f"[data] GFP5 stream error id={file_id} offset={sent} error={exc}", flush=True)
                    raise
        final_header = bytes.fromhex(save["final_header"])
    else:
        print(f"[data] GFP5 id={file_id} type={file_type} noent", flush=True)
        final_header = bytearray(1304)
        struct.pack_into("<I", final_header, 0, 0x35504647)
        struct.pack_into("<I", final_header, 4, file_id)
        final_header[274] = 1
        final_header = bytes(final_header)
    send_record(sock, key, nonce, sequence, final_header)
    print(f"[data] GFP5 final header id={file_id} bytes={len(final_header)}", flush=True)
    return sequence + 1


def user_registry_value(body):
    user_id, key, value_type, _, size = struct.unpack("<IIIIQ", body)
    value = b""
    if key == 0x101 and value_type == 1:
        db = sqlite3.connect(METADATA_PATH)
        row = db.execute("SELECT login_id FROM user_info_tbl WHERE user_id = ?", (user_id,)).fetchone()
        db.close()
        if row and row[0]:
            value = row[0].encode() + b"\0"
    if not value:
        print(f"[data] no registry value for user={user_id:#x} key={key:#x} type={value_type}", flush=True)
    return value[:size].ljust(size, b"\0")


def serve_command(sock, oracle_host, oracle_port):
    server_value = os.urandom(16)
    sock.sendall(server_value + bytes(48))
    client_value = recv_exact(sock, 64)[:16]
    recv_key, send_key, recv_nonce, send_nonce = derive_session(
        sock, oracle_host, oracle_port, client_value, server_value
    )
    COMMAND_STATE.update(sock=sock, key=send_key, nonce=send_nonce, active=True, sequence=0)
    completion = recv_record(sock, recv_key, recv_nonce, 0)
    print(f"[ae] completion: {completion.hex()}", flush=True)
    recv_sequence = 1
    send_sequence = 0
    while True:
        header = recv_record(sock, recv_key, recv_nonce, recv_sequence)
        recv_sequence += 1
        cmd, sub, version, flags, size = struct.unpack("<BBHIQ", header)
        COMMAND_STATE["sub"] = sub
        print(f"[cmd] {cmd:#x} sub={sub} version={version:#x} body={size}", flush=True)
        body = b""
        if size and not (cmd == 0x92 and size == 4000000):
            body = recv_record(sock, recv_key, recv_nonce, recv_sequence)
            recv_sequence += 1
            print(f"[cmd] body: {body.hex()}", flush=True)
        if cmd in (0x91, 0x93):
            hardware = struct.unpack_from("<I", body, 44)[0]
            product_code = struct.unpack_from("<I", body, 48)[0]
            version_a = struct.unpack_from("<I", body, 76)[0]
            version_b = struct.unpack_from("<I", body, 84)[0]
            response_body = console_info(hardware, product_code, body[56:73], version_a, version_b)
            result = 0
            if cmd == 0x91:
                waiting = struct.pack("<BBHIQ", cmd, sub, version, 33, 0)
                send_command_record(sock, send_key, send_nonce, send_sequence, waiting)
                send_sequence += 1
                POWER_EVENT.clear()
                print("[power] type 'power' to simulate the source console power button", flush=True)
                if not POWER_EVENT.wait(310):
                    raise TimeoutError("power-button simulation timed out")
        elif cmd == 0x92:
            response_body = body
            result = 0
        elif cmd == 0xA1:
            response_body = b""
            result = 0
        elif cmd == 0x94:
            response_body = b""
            result = 0
        elif cmd in (0xB0, 0xB1, 0xB3, 0xB4):
            response_body = b""
            result = 0
        else:
            response_body = b""
            result = 2
        response = struct.pack("<BBHIQ", cmd, sub, version, result, len(response_body))
        print(f"[cmd] response cmd={cmd:#x} result={result} body={len(response_body)} seq={send_sequence}", flush=True)
        send_command_record(sock, send_key, send_nonce, send_sequence, response)
        send_sequence += 1
        for offset in range(0, len(response_body), 0x200000):
            print(f"[cmd] response body offset={offset} seq={send_sequence}", flush=True)
            send_command_record(sock, send_key, send_nonce, send_sequence, response_body[offset:offset + 0x200000])
            send_sequence += 1
        if cmd == 0xA1:
            send_command_record(sock, send_key, send_nonce, send_sequence, struct.pack("<I", 1000))
            send_sequence += 1
        print(f"[cmd] response complete cmd={cmd:#x}", flush=True)
        if cmd == 0x92:
            break
        if cmd not in (0x91, 0x93, 0xA1, 0xB3):
            break
    COMMAND_STATE["active"] = False


def data_server(listener, oracle_host, oracle_port):
    while True:
        client, address = listener.accept()
        print(f"[data] connection from {address}", flush=True)
        with client:
            client.settimeout(310)
            client_value = recv_exact(client, 64)[:16]
            server_value = os.urandom(16)
            client.sendall(server_value + bytes(48))
            recv_key, send_key, recv_nonce, send_nonce = derive_session(
                client, oracle_host, oracle_port, client_value, server_value
            )
            completion = recv_record(client, recv_key, recv_nonce, 0)
            print(f"[data] completion: {completion.hex()}", flush=True)
            sequence = 1
            send_sequence = 0
            while True:
                header = recv_record(client, recv_key, recv_nonce, sequence)
                sequence += 1
                cmd, sub, version, flags, size = struct.unpack("<BBHIQ", header)
                print(f"[data] cmd={cmd:#x} sub={sub} version={version:#x} body={size}", flush=True)
                if cmd == 0x92:
                    response = struct.pack("<BBHIQ", cmd, sub, version, 0, size)
                    send_record(client, send_key, send_nonce, send_sequence, response)
                    send_sequence += 1
                    remaining = size
                    while remaining:
                        chunk = recv_record(client, recv_key, recv_nonce, sequence)
                        sequence += 1
                        send_record(client, send_key, send_nonce, send_sequence, chunk)
                        send_sequence += 1
                        remaining -= len(chunk)
                    break
                body = b""
                if size and cmd != 0x92:
                    body, sequence = recv_body(client, recv_key, recv_nonce, sequence, size)
                    print(f"[data] body ({len(body)} bytes): {body.hex()}", flush=True)
                if cmd == 0xA0:
                    for result in (0x23, 0x23, 0x23, 0x23, 0x23, 0x23, 0):
                        response = struct.pack("<BBHIQ", cmd, sub, version, result, 0)
                        send_record(client, send_key, send_nonce, send_sequence, response)
                        send_sequence += 1
                elif cmd == 0xB2:
                    file_type = struct.unpack_from("<I", body, 0)[0]
                    if file_type == 1:
                        metadata = Path(METADATA_PATH).read_bytes()
                        response = struct.pack("<BBHIQ", cmd, sub, version, 0, len(metadata))
                        send_record(client, send_key, send_nonce, send_sequence, response)
                        send_sequence += 1
                        for offset in range(0, len(metadata), 0x200000):
                            send_record(client, send_key, send_nonce, send_sequence, metadata[offset:offset + 0x200000])
                            send_sequence += 1
                    else:
                        response = struct.pack("<BBHIQ", cmd, sub, version, 0, 0)
                        send_record(client, send_key, send_nonce, send_sequence, response)
                        send_sequence += 1
                        file_id = struct.unpack_from("<I", body, 4)[0]
                        send_sequence = send_save_chunks(
                            client, send_key, send_nonce, send_sequence, file_type, file_id
                        )
                elif cmd == 0x92:
                    response = struct.pack("<BBHIQ", cmd, sub, version, 0, len(body))
                    send_record(client, send_key, send_nonce, send_sequence, response)
                    send_sequence += 1
                    for offset in range(0, len(body), 0x200000):
                        send_record(client, send_key, send_nonce, send_sequence, body[offset:offset + 0x200000])
                        send_sequence += 1
                    break
                elif cmd == 0xB4:
                    key = struct.unpack_from("<I", body)[0]
                    value_size = struct.unpack_from("<Q", body, 8)[0]
                    value = struct.pack("<I", 1) if key == 0x02020000 else bytes(value_size)
                    print(f"[data] GRP5 key={key:#x} size={value_size}", flush=True)
                    response = struct.pack("<BBHIQ", cmd, sub, version, 0, 0)
                    send_record(client, send_key, send_nonce, send_sequence, response)
                    send_sequence += 1
                    descriptor = struct.pack("<I", 0x35505247) + body[:4] + struct.pack("<Q", body[4]) + body[8:16]
                    send_record(client, send_key, send_nonce, send_sequence, descriptor)
                    send_sequence += 1
                    send_record(client, send_key, send_nonce, send_sequence, value)
                    send_sequence += 1
                elif cmd == 0xB5:
                    value = user_registry_value(body)
                    response = struct.pack("<BBHIQ", cmd, sub, version, 0, 0)
                    send_record(client, send_key, send_nonce, send_sequence, response)
                    send_sequence += 1
                    descriptor = struct.pack("<I", 0x35525547) + body[:12] + body[16:]
                    send_record(client, send_key, send_nonce, send_sequence, descriptor)
                    send_sequence += 1
                    send_record(client, send_key, send_nonce, send_sequence, value)
                    send_sequence += 1
                elif cmd in (0xB1, 0xB3):
                    response = struct.pack("<BBHIQ", cmd, sub, version, 0, 0)
                    send_record(client, send_key, send_nonce, send_sequence, response)
                    send_sequence += 1
                    if cmd == 0xB3:
                        break
                else:
                    break


def main():
    global DATA_ADDRESS, ADDRESS_PINNED, COMMAND_PORT, DATA_PORT, SYSTEM_VERSION
    parser = argparse.ArgumentParser(description="Local PS5 Data Transfer source emulator")
    # The UI spawns this module with no argv, so every setting also has an
    # environment form; without that the oracle below defaults to the developer's
    # own LAN and upload fails as soon as the console connects.
    parser.add_argument("--oracle-host", default=os.environ.get("DT_ORACLE_HOST", "192.168.0.125"))
    parser.add_argument("--oracle-port", type=int,
                        default=int(os.environ.get("DT_ORACLE_PORT", "9022")))
    parser.add_argument("--discovery-port", type=int, default=int(os.environ.get("DT_DISCOVERY_PORT", "1006")))
    parser.add_argument("--port", type=int, default=COMMAND_PORT)
    parser.add_argument("--data-port", type=int, default=DATA_PORT)
    parser.add_argument("--ipv6", action="store_true", help="use local PSK derivation instead of the oracle")
    parser.add_argument("--advertise-address", default=os.environ.get("DT_ADVERTISE"),
                        help="PC address to advertise for data connections (IPv4 or IPv6). "
                             "Default: detect the interface the console reaches us on")
    parser.add_argument("--system-version", default=os.environ.get("DT_SYSTEM_VERSION"),
                        help="system-version to advertise over DDP (default 14200007)")
    args = parser.parse_args()
    COMMAND_PORT = args.port
    DATA_PORT = args.data_port
    if args.system_version:
        SYSTEM_VERSION = str(args.system_version).encode()
    if args.ipv6 and not args.advertise_address:
        parser.error("--ipv6 requires --advertise-address")
    if args.advertise_address:
        try:
            DATA_ADDRESS = packed_address(args.advertise_address)
        except ValueError as exc:
            parser.error(f"--advertise-address is not a valid IP address: {exc}")
        ADDRESS_PINNED = True
        print(f"[ddp] advertising the pinned data address {args.advertise_address}", flush=True)
    print(f"[oracle] deriving keys via {args.oracle_host}:{args.oracle_port}"
          f"{' (not used for --ipv6)' if args.ipv6 else ''}", flush=True)
    family = socket.AF_INET6 if args.ipv6 else socket.AF_INET
    bind_address = "::" if args.ipv6 else "0.0.0.0"
    sys.stdout = Tee(os.environ.get("DT_LOG", "dt.txt"))
    sys.stderr = sys.stdout
    threading.Thread(target=interactive_console, daemon=True).start()

    data_listener = socket.socket(family, socket.SOCK_STREAM)
    data_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    data_listener.bind((bind_address, DATA_PORT))
    data_listener.listen(1)
    threading.Thread(target=data_server, args=(data_listener, args.oracle_host, args.oracle_port), daemon=True).start()
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((bind_address, args.port))
    listener.listen(1)
    threading.Thread(target=discovery_server, args=(args.discovery_port, args.ipv6), daemon=True).start()
    print(f"[tcp] source listening on {args.port}; select {HOST_NAME.decode()} on the PS5", flush=True)
    while True:
        client, address = listener.accept()
        print(f"[tcp] connection from {address}", flush=True)
        with client:
            client.settimeout(310)
            try:
                serve_command(client, args.oracle_host, args.oracle_port)
            except (EOFError, OSError, ValueError) as exc:
                print(f"[cmd] session ended: {exc}", flush=True)
        print(f"[tcp] waiting for the next command connection on {args.port}", flush=True)


if __name__ == "__main__":
    main()
