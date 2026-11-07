import socket
import threading
import unittest

from poc.dt_discovery import SRCH, discover_routes


class DiscoveryRouteTests(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def add_server(self, address, port=0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((address, port))
        sock.settimeout(0.1)
        self.servers.append(sock)

        def serve():
            while True:
                try:
                    request, peer = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not request.startswith(b"SRCH"):
                    continue
                response = (
                    b"HTTP/1.1 200 Ok\n"
                    + b"host-id:5C843CC6D727\n"
                    + b"host-type:PS5\n"
                    + b"host-name:" + address.encode() + b"\n"
                    + b"host-request-port:1004\n"
                    + b"device-discovery-protocol-version:00030010\n"
                    + b"system-version:13600007\n"
                )
                sock.sendto(response, peer)

        threading.Thread(target=serve, daemon=True).start()
        return sock.getsockname()[1]

    def tearDown(self):
        for sock in self.servers:
            sock.close()

    def test_multiple_bound_routes_collect_every_response(self):
        port = self.add_server("127.0.0.1")
        self.assertEqual(self.add_server("127.0.0.2", port), port)
        consoles = discover_routes([
            ("127.0.0.1", "127.0.0.1"),
            ("127.0.0.1", "127.0.0.2"),
        ], 0.5, port)
        self.assertEqual({item["host"] for item in consoles}, {"127.0.0.1", "127.0.0.2"})

    def test_unbound_directed_route_uses_unicast(self):
        port = self.add_server("127.0.0.1")
        consoles = discover_routes([("", "127.0.0.1")], 0.5, port)
        self.assertEqual(len(consoles), 1)
        self.assertEqual(consoles[0]["host"], "127.0.0.1")
        self.assertEqual(consoles[0]["firmware"], "13600007")


if __name__ == "__main__":
    unittest.main()
