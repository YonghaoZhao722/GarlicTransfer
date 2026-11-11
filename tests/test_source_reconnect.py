import socket
import struct
import threading
import unittest
from unittest.mock import Mock, patch

from poc import dt_source
from poc.dt_psk_client import recv_exact, recv_record, send_record


class StopListener(Exception):
    pass


class DataReconnectTests(unittest.TestCase):
    def check_reconnect(self, after_handshake):
        first, abandoned = socket.socketpair()
        second, peer = socket.socketpair()
        listener = Mock()
        listener.accept.side_effect = [
            (first, ('127.0.0.1', 1)),
            (second, ('127.0.0.1', 2)),
            StopListener(),
        ]
        key, nonce = bytes(16), bytes(8)
        failures = []

        def handshake(client):
            client.sendall(bytes(64))
            recv_exact(client, 64)
            send_record(client, key, nonce, 0, b'')

        def clients():
            try:
                with abandoned:
                    abandoned.settimeout(2)
                    if after_handshake:
                        handshake(abandoned)
                with peer:
                    peer.settimeout(2)
                    handshake(peer)
                    header = struct.pack('<BBHIQ', 0x92, 1, 0x140, 0, 4)
                    send_record(peer, key, nonce, 1, header)
                    send_record(peer, key, nonce, 2, b'echo')
                    self.assertEqual(recv_record(peer, key, nonce, 0), header)
                    self.assertEqual(recv_record(peer, key, nonce, 1), b'echo')
            except Exception as exc:
                failures.append(exc)

        with patch.object(dt_source, 'derive_session', return_value=(key, key, nonce, nonce)):
            thread = threading.Thread(target=clients)
            thread.start()
            try:
                with self.assertRaises(StopListener):
                    dt_source.data_server(listener, '127.0.0.1', 9022)
            finally:
                first.close()
                second.close()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(listener.accept.call_count, 3)

    def test_reconnect_after_eof_during_handshake(self):
        self.check_reconnect(after_handshake=False)

    def test_reconnect_after_eof_waiting_for_command(self):
        self.check_reconnect(after_handshake=True)


if __name__ == '__main__':
    unittest.main()
