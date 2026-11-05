#!/usr/bin/env python3
"""Pair once with a PS5 Data Transfer request, without starting a transfer.

The console sends its own 0x91 request when the user selects this advertised
source in Data Transfer. The console ID is checked and only the first 32 bytes
are retained. This listener never answers the encrypted request or opens the
data port.
"""

import ipaddress
import json
import os
import re
import select
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path

from poc.dt_psk_client import recv_exact, recv_record


HOST_ID = b"020000000001"
HOST_NAME = b"GarlicTransfer Pairing"
PREFIX_SIZE = 32
POWER_BODY_SIZE = 88


class DeadlineReader:
    """Keep a single absolute deadline across partial TCP reads."""

    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def recv(self, size):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("pairing timed out while reading a connection")
        self.sock.settimeout(min(30, remaining))
        return self.sock.recv(size)


def emit(kind, **values):
    print(json.dumps({"type": kind, **values}), flush=True)


def local_ipv4_for(ps5_ip):
    """Ask the routing table which local address reaches the selected console."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((ps5_ip, 1006))
        address = route.getsockname()[0]
    if address == "0.0.0.0":
        raise OSError("no local IPv4 route to the selected PS5")
    return address


def discovery_reply(command, bind_ip, system_version, command_port):
    common = (
        b"HTTP/1.1 200 Ok\n"
        + b"host-id:" + HOST_ID + b"\n"
        + b"host-request-port:" + str(command_port).encode("ascii") + b"\n"
        + b"device-discovery-protocol-version:00030010\n"
    )
    if command == b"SRCH":
        return (
            common + b"host-type:PS5\n"
            + b"host-name:" + HOST_NAME + b"\n"
            + b"system-version:" + system_version.encode("ascii") + b"\n"
        )
    if command == b"LAUNCH4":
        address = ipaddress.IPv6Address(f"::ffff:{bind_ip}").packed.hex().encode("ascii")
        return common + b"host-network:0\n" + b"host-address:" + address + b"\n"
    return None


def derive_receive_key(oracle_host, oracle_port, client_value, server_value, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("pairing timed out before key derivation")
    with socket.create_connection((oracle_host, oracle_port), timeout=min(5, remaining)) as oracle:
        oracle.sendall(client_value + server_value)
        derived = recv_exact(DeadlineReader(oracle, deadline), 40)
    return derived[:16], derived[32:36]


def receive_prefix(client, oracle_host, oracle_port, deadline):
    """Read and validate one encrypted 0x91; send no encrypted response."""
    reader = DeadlineReader(client, deadline)
    server_value = os.urandom(16)
    client.sendall(server_value + bytes(48))
    client_value = recv_exact(reader, 64)[:16]
    recv_key, recv_nonce = derive_receive_key(
        oracle_host, oracle_port, client_value, server_value, deadline
    )
    if recv_record(reader, recv_key, recv_nonce, 0):
        raise ValueError("unexpected handshake completion")
    header = recv_record(reader, recv_key, recv_nonce, 1)
    if len(header) != 16:
        raise ValueError("unexpected command header length")
    command, _sub, _version, _flags, size = struct.unpack("<BBHIQ", header)
    if command != 0x91 or size != POWER_BODY_SIZE:
        raise ValueError("expected an 88-byte Data Transfer power request")
    body = bytearray()
    sequence = 2
    while len(body) < POWER_BODY_SIZE:
        if time.monotonic() >= deadline:
            raise TimeoutError("pairing timed out while reading the request")
        chunk = recv_record(reader, recv_key, recv_nonce, sequence)
        if not chunk or len(body) + len(chunk) > POWER_BODY_SIZE:
            raise ValueError("invalid Data Transfer request length")
        body.extend(chunk)
        sequence += 1
    return bytes(body[:PREFIX_SIZE]), bytes(body[32:38])


def save_prefix(prefix, destination):
    if len(prefix) != PREFIX_SIZE:
        raise ValueError("pairing prefix must contain 32 bytes")
    path = Path(destination).expanduser()
    missing = []
    ancestor = path.parent
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".pairing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.chmod(temporary, 0o600)
            output.write(prefix)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_options(options):
    if not isinstance(options, dict):
        raise ValueError("pairing options must be a JSON object")
    ps5_ip = str(ipaddress.IPv4Address(options["ps5Ip"]))
    host_id = options["hostId"]
    if not isinstance(host_id, str) or not re.fullmatch(r"[0-9a-fA-F]{12}", host_id):
        raise ValueError("hostId must be 12 hexadecimal digits from discovery")
    system_version = str(options["systemVersion"])
    if not re.fullmatch(r"[0-9a-fA-F]{8}", system_version):
        raise ValueError("systemVersion must be eight hexadecimal digits from discovery")
    oracle_host = options["oracleHost"]
    oracle_port = int(options["oraclePort"])
    if not isinstance(oracle_host, str) or not oracle_host.strip() or not 1 <= oracle_port <= 65535:
        raise ValueError("invalid oracle address or port")
    destination = options["prefixPath"]
    if not isinstance(destination, str) or not Path(destination).is_absolute():
        raise ValueError("prefixPath must be an absolute private file path")
    duration = int(options.get("duration", 180))
    if not 1 <= duration <= 600:
        raise ValueError("duration must be between 1 and 600 seconds")
    return ps5_ip, host_id.lower(), system_version.upper(), oracle_host, oracle_port, destination, duration


def drop_privileges_from_environment():
    if os.name != "posix" or os.geteuid() != 0:
        return
    uid = os.environ.get("DT_PAIRING_UID")
    gid = os.environ.get("DT_PAIRING_GID")
    if uid is None or gid is None:
        return
    os.setgroups([])
    os.setgid(int(gid))
    os.setuid(int(uid))


def pair(options, discovery_port=1006, command_port=1004, bind_ip=None):
    ps5_ip, host_id, system_version, oracle_host, oracle_port, destination, duration = validate_options(options)
    bind_ip = bind_ip or local_ipv4_for(ps5_ip)
    deadline = time.monotonic() + duration
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as discovery, \
         socket.socket(socket.AF_INET, socket.SOCK_STREAM) as command:
        discovery.bind(("0.0.0.0", discovery_port))
        command.bind((bind_ip, command_port))
        command.listen(1)
        drop_privileges_from_environment()
        emit("status", message="Pairing listener ready. Select GarlicTransfer Pairing on the PS5.")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select(
                [discovery, command], [], [], min(0.5, remaining)
            )
            for ready in readable:
                if ready is discovery:
                    packet, address = discovery.recvfrom(4096)
                    if address[0] != ps5_ip:
                        continue
                    reply = discovery_reply(packet.split(b" ", 1)[0], bind_ip, system_version, command_port)
                    if reply is not None:
                        discovery.sendto(reply, address)
                else:
                    client, address = command.accept()
                    with client:
                        if address[0] != ps5_ip:
                            continue
                        emit("status", message="PS5 connected; reading its Data Transfer request.")
                        prefix, observed_host_id = receive_prefix(client, oracle_host, oracle_port, deadline)
                    if observed_host_id != bytes.fromhex(host_id):
                        raise ValueError("Data Transfer request did not match the selected console")
                    save_prefix(prefix, destination)
                    emit("paired")
                    return
    raise TimeoutError("pairing timed out before the PS5 request arrived")


def main():
    if len(sys.argv) != 2 or sys.argv[1] != "-":
        raise SystemExit("usage: python -m poc.dt_pair - (JSON options on standard input)")
    try:
        pair(json.loads(sys.stdin.readline()))
    except (EOFError, KeyError, OSError, TimeoutError, TypeError, ValueError) as exc:
        # Do not print the console request, pairing prefix, or account identifier.
        emit("error", message=f"Pairing stopped ({type(exc).__name__}). Check the PS5, network, and oracle.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
