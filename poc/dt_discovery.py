#!/usr/bin/env python3
import argparse
import json
import select
import socket
import time


SRCH = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030000\n"


def parse_response(response, address):
    fields = {}
    for line in response.splitlines():
        if b":" in line:
            key, value = line.split(b":", 1)
            fields[key.decode()] = value.decode()
    return {
        "host": address[0],
        "name": fields.get("host-name", "PS5"),
        "type": fields.get("host-type", "PS5"),
        "hostId": fields.get("host-id", ""),
        "firmware": fields.get("system-version", "unknown"),
        "requestPort": fields.get("host-request-port", ""),
        "network": fields.get("host-network", ""),
        "address": fields.get("host-address", ""),
    }


def discover_routes(routes, timeout, port=1006):
    consoles = {}
    sockets = []
    try:
        for bind, target in routes:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if bind:
                    sock.bind((bind, 0))
                sock.setblocking(False)
                sock.sendto(SRCH, (target, port))
            except OSError:
                sock.close()
                continue
            sockets.append(sock)

        deadline = time.monotonic() + timeout
        while sockets and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select(sockets, [], [], min(0.2, remaining))
            for sock in readable:
                while True:
                    try:
                        response, address = sock.recvfrom(4096)
                    except BlockingIOError:
                        break
                    if response.startswith(b"HTTP/1.1"):
                        console = parse_response(response, address)
                        consoles[address[0]] = console
    finally:
        for sock in sockets:
            sock.close()
    return list(consoles.values())


def discover(broadcast, timeout):
    return discover_routes([("", broadcast)], timeout)


def main():
    parser = argparse.ArgumentParser(description="Discover PS5 consoles using SRCH")
    parser.add_argument("--broadcast", default="255.255.255.255")
    parser.add_argument("--routes", help="JSON list of {bind, target} discovery routes")
    parser.add_argument("--port", type=int, default=1006)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    if args.routes:
        routes = [(item.get("bind", ""), item["target"]) for item in json.loads(args.routes)]
    else:
        routes = [("", args.broadcast)]
    print(json.dumps({"consoles": discover_routes(routes, args.timeout, args.port)}), flush=True)


if __name__ == "__main__":
    main()
