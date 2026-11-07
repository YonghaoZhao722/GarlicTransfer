import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from poc.dt_pair import discovery_reply, pair, save_prefix, validate_options
from poc.dt_psk_client import command_header, recv_exact, send_record


def unused_port(kind):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PairTests(unittest.TestCase):
    def test_discovery_advertises_selected_firmware_and_route(self):
        search = discovery_reply(b"SRCH", "192.0.2.5", "14220000", 1004)
        launch = discovery_reply(b"LAUNCH4", "192.0.2.5", "14220000", 1004)
        self.assertIn(b"host-name:GarlicTransfer Pairing\n", search)
        self.assertIn(b"system-version:14220000\n", search)
        self.assertIn(b"host-request-port:1004\n", search)
        self.assertIn(b"host-address:00000000000000000000ffffc0000205\n", launch)
        self.assertIsNone(discovery_reply(b"OTHER", "192.0.2.5", "14220000", 1004))

    def test_validation_rejects_bad_target_or_version(self):
        options = {
            "ps5Ip": "127.0.0.1", "hostId": "202122232425", "systemVersion": "14220000",
            "oracleHost": "127.0.0.1", "oraclePort": 9022,
            "prefixPath": str(Path(tempfile.gettempdir()) / "pairing-prefix.bin"), "duration": 180,
        }
        self.assertEqual(validate_options(options)[0:3], ("127.0.0.1", "202122232425", "14220000"))
        for change in ({"ps5Ip": "::1"}, {"hostId": "not-a-host"}, {"systemVersion": "14.22"},
                       {"duration": 0}, {"oraclePort": 0}, {"prefixPath": ""},
                       {"prefixPath": "relative-prefix.bin"}):
            with self.subTest(change=change), self.assertRaises((ValueError, KeyError)):
                validate_options({**options, **change})

    def test_prefix_file_contains_only_32_bytes_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "private" / "pairing-prefix.bin"
            save_prefix(bytes(range(32)), destination)
            self.assertEqual(destination.read_bytes(), bytes(range(32)))
            save_prefix(bytes(reversed(range(32))), destination)
            self.assertEqual(destination.read_bytes(), bytes(reversed(range(32))))
            self.assertEqual([p.name for p in destination.parent.iterdir()], [destination.name])
            with self.assertRaises(ValueError):
                save_prefix(b"short", destination)
            self.assertEqual(destination.read_bytes(), bytes(reversed(range(32))))

    def test_privileged_prefix_is_returned_to_the_app_user(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "pairing-prefix.bin"
            environment = {"DT_PAIRING_UID": "1000", "DT_PAIRING_GID": "1001"}
            with patch.dict(os.environ, environment), \
                 patch("poc.dt_pair.os.geteuid", return_value=0), \
                 patch("poc.dt_pair.os.chown") as chown:
                save_prefix(bytes(range(32)), destination)
            chown.assert_called_once_with(destination, 1000, 1001)
            self.assertEqual(destination.read_bytes(), bytes(range(32)))

    def test_privileged_pairing_drops_back_to_the_app_user_after_binding(self):
        from poc.dt_pair import drop_privileges_from_environment

        environment = {"DT_PAIRING_UID": "1000", "DT_PAIRING_GID": "1001"}
        with patch.dict(os.environ, environment), \
             patch("poc.dt_pair.os.geteuid", return_value=0), \
             patch("poc.dt_pair.os.setgroups") as setgroups, \
             patch("poc.dt_pair.os.setgid") as setgid, \
             patch("poc.dt_pair.os.setuid") as setuid:
            drop_privileges_from_environment()
        setgroups.assert_called_once_with([])
        setgid.assert_called_once_with(1001)
        setuid.assert_called_once_with(1000)

    def test_one_encrypted_request_pairs_without_answer_or_data_port(self):
        prefix = bytes(range(32))
        body = prefix + bytes(range(32, 88))
        receive_key = b"K" * 16
        receive_nonce = b"N" * 4
        oracle_port = unused_port(socket.SOCK_STREAM)
        discovery_port = unused_port(socket.SOCK_DGRAM)
        command_port = unused_port(socket.SOCK_STREAM)
        oracle_ready = threading.Event()
        pairing_ready = threading.Event()
        result = {}
        events = []

        def oracle():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", oracle_port))
                listener.listen(1)
                oracle_ready.set()
                with listener.accept()[0] as client:
                    self.assertEqual(len(recv_exact(client, 32)), 32)
                    client.sendall(receive_key + b"S" * 16 + receive_nonce + b"Q" * 4)

        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "pairing-prefix.bin"
            options = {
                "ps5Ip": "127.0.0.1", "hostId": "202122232425", "systemVersion": "14220000",
                "oracleHost": "127.0.0.1", "oraclePort": oracle_port,
                "prefixPath": str(destination), "duration": 5,
            }

            def emit(kind, **values):
                events.append((kind, values))
                if kind == "status" and "ready" in values.get("message", ""):
                    pairing_ready.set()

            def run_pair():
                try:
                    pair(options, discovery_port, command_port, bind_ip="127.0.0.1")
                except Exception as exc:
                    result["error"] = exc

            with patch("poc.dt_pair.emit", emit):
                oracle_thread = threading.Thread(target=oracle, daemon=True)
                pair_thread = threading.Thread(target=run_pair, daemon=True)
                oracle_thread.start()
                self.assertTrue(oracle_ready.wait(2))
                pair_thread.start()
                self.assertTrue(pairing_ready.wait(2))

                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as discovery:
                    discovery.settimeout(2)
                    discovery.sendto(b"SRCH * HTTP/1.1\n", ("127.0.0.1", discovery_port))
                    response, _ = discovery.recvfrom(4096)
                    self.assertIn(b"GarlicTransfer Pairing", response)
                    discovery.sendto(b"LAUNCH4 * HTTP/1.1\n", ("127.0.0.1", discovery_port))
                    response, _ = discovery.recvfrom(4096)
                    self.assertIn(b"host-address:", response)

                with socket.create_connection(("127.0.0.1", command_port), timeout=2) as client:
                    client.settimeout(2)
                    self.assertEqual(len(recv_exact(client, 64)), 64)
                    client.sendall(os.urandom(16) + bytes(48))
                    send_record(client, receive_key, receive_nonce, 0, b"")
                    send_record(client, receive_key, receive_nonce, 1,
                                command_header(0x91, len(body), version=0x140))
                    send_record(client, receive_key, receive_nonce, 2, body)
                    self.assertEqual(client.recv(1), b"")  # No command response is sent.

                pair_thread.join(2)
                oracle_thread.join(2)
            self.assertFalse(pair_thread.is_alive())
            self.assertFalse(oracle_thread.is_alive())
            self.assertNotIn("error", result)
            self.assertEqual(destination.read_bytes(), prefix)
            self.assertEqual(events[-1][0], "paired")

    def test_mismatched_console_request_does_not_save_pairing(self):
        discovery_port = unused_port(socket.SOCK_DGRAM)
        command_port = unused_port(socket.SOCK_STREAM)
        ready = threading.Event()
        result = {}
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "pairing-prefix.bin"
            options = {
                "ps5Ip": "127.0.0.1", "hostId": "202122232425",
                "systemVersion": "14220000", "oracleHost": "127.0.0.1",
                "oraclePort": 9022, "prefixPath": str(destination), "duration": 5,
            }

            def emit(kind, **values):
                if kind == "status" and "ready" in values.get("message", ""):
                    ready.set()

            def run_pair():
                try:
                    pair(options, discovery_port, command_port, bind_ip="127.0.0.1")
                except Exception as exc:
                    result["error"] = exc

            with patch("poc.dt_pair.emit", emit), \
                 patch("poc.dt_pair.receive_prefix", return_value=(bytes(32), bytes.fromhex("000000000001"))):
                worker = threading.Thread(target=run_pair, daemon=True)
                worker.start()
                self.assertTrue(ready.wait(2))
                with socket.create_connection(("127.0.0.1", command_port), timeout=2):
                    pass
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(result.get("error"), ValueError)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
