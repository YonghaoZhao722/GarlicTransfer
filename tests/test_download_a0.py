import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from poc.dt_download import download_metadata


class FakeChannel:
    def __init__(self, busy_results):
        self.busy_results = list(busy_results)
        self.commands = []

    def request(self, cmd, _body):
        self.commands.append(cmd)
        return 1

    def response(self, cmd, _sub):
        if cmd == 0xA0:
            return self.busy_results.pop(0), b""
        if cmd == 0xB2:
            return 0, b"metadata"
        raise AssertionError(f"unexpected command {cmd:#x}")


class DownloadMetadataTests(unittest.TestCase):
    def test_busy_a0_is_retried_with_backoff_beyond_sixteen_results(self):
        channel = FakeChannel([0x23] * 20 + [0])
        with tempfile.TemporaryDirectory() as directory, patch("poc.dt_download.time.sleep") as sleep:
            download_metadata(channel, Path(directory))
            self.assertEqual((Path(directory) / "metadata.db").read_bytes(), b"metadata")
        self.assertEqual(channel.commands, [0xA0, 0xB2])
        self.assertEqual(sleep.call_count, 20)
        sleep.assert_called_with(0.25)

    def test_busy_a0_has_a_finite_timeout(self):
        channel = FakeChannel([0x23])
        with tempfile.TemporaryDirectory() as directory, patch(
            "poc.dt_download.time.monotonic", side_effect=[0, 120]
        ), patch("poc.dt_download.time.sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "A0 did not complete"):
                download_metadata(channel, Path(directory))
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
