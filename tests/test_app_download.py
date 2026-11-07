import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from poc.dt_download import download_entry


class FakeChannel:
    def __init__(self):
        self.sock = object()
        self.recv_key = None
        self.recv_nonce = None
        self.recv_sequence = 0

    def request(self, command, body):
        return 7

    def response(self, command, sub):
        return 0, b""


class AppDownloadTests(unittest.TestCase):
    def test_final_chunk_padding_is_not_written_or_hashed(self):
        payload = b"pkg-data"
        header = bytearray(1304)
        struct.pack_into("<I", header, 0, 0x35504647)
        struct.pack_into("<I", header, 4, 296)
        struct.pack_into("<Q", header, 240, len(payload))
        source = b"/user/app/PPSA05688/app.pkg"
        header[276:276 + len(source)] = source
        companion = bytearray(1304)
        struct.pack_into("<I", companion, 0, 0x35504647)
        struct.pack_into("<I", companion, 4, 296)
        struct.pack_into("<Q", companion, 240, 4)
        companion_source = b"/user/app/PPSA05688/app.json"
        companion[276:276 + len(companion_source)] = companion_source
        final = bytearray(1304)
        struct.pack_into("<I", final, 0, 0x35504647)
        struct.pack_into("<I", final, 4, 296)
        final[274] = 1
        records = [bytes(header), payload + bytes(5), bytes(companion), b"json", bytes(final)]

        with tempfile.TemporaryDirectory() as folder, \
             patch("poc.dt_download.recv_record", side_effect=records):
            output = Path(folder)
            download_entry(FakeChannel(), 296, output, package_only=True)
            package = output / "296" / "app.pkg"
            self.assertEqual(package.read_bytes(), payload)
            self.assertFalse((output / "296" / "app.json").exists())
            manifest = json.loads((output / "296" / "manifest.json").read_text())
            self.assertEqual(manifest["296"]["files"][0]["size"], len(payload))
            self.assertEqual(len(manifest["296"]["files"]), 1)


if __name__ == "__main__":
    unittest.main()
