import ftplib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from poc.dt_ftp_server import Catalog, FtpServer, normalize


class FakeBackend:
    def __init__(self, output):
        self.output = Path(output)
        self.downloads = []

    def download(self, entry):
        self.downloads.append(entry.id)
        directory = self.output / str(entry.id)
        directory.mkdir(parents=True, exist_ok=True)
        name = "app.pkg" if entry.category == "apps" else "sdimg_test"
        payload = b"pkg payload" if entry.category == "apps" else b"save payload"
        (directory / name).write_bytes(payload)
        (directory / "manifest.json").write_text(json.dumps({"entry_id": entry.id, "files": []}))


class FtpServerTests(unittest.TestCase):
    def test_ftp_client_browses_and_downloads_cached_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            metadata = root / "metadata.db"
            db = sqlite3.connect(metadata)
            db.execute("CREATE TABLE dir_entry_info_tbl (id, path, actual_size, target_type)")
            db.executemany(
                "INSERT INTO dir_entry_info_tbl VALUES (?, ?, ?, ?)",
                [
                    (296, "/user/app/PPSA05688/", 42078652, 1),
                    (349, "/user/app/PPSA98513/", 43763, 1),
                    (122, "/user/home/18c60ea2/savedata_prospero/PPSA21564/", 20971520, 3),
                ],
            )
            db.commit()
            db.close()
            backend = FakeBackend(root)
            catalog = Catalog(metadata, root, backend)
            server = FtpServer("127.0.0.1", 0, catalog)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = ftplib.FTP()
            try:
                client.connect(server.host, server.port, timeout=3)
                client.login()
                self.assertIn("apps", client.nlst("/"))
                self.assertEqual(client.nlst("/apps"), ["296_PPSA05688", "349_PPSA98513"])
                listing = []
                client.retrlines("LIST /apps", listing.append)
                self.assertEqual(backend.downloads, [])
                client.cwd("/apps/296_PPSA05688")
                self.assertIn("app.pkg", client.nlst())
                self.assertEqual(backend.downloads, [296])
                chunks = []
                client.retrbinary("RETR app.pkg", chunks.append)
                self.assertEqual(b"".join(chunks), b"pkg payload")
                self.assertEqual(client.size("app.pkg"), len(b"pkg payload")
                )
                lines = []
                client.retrlines("LIST", lines.append)
                self.assertTrue(any(line.startswith("-r--r--r--") and line.endswith("app.pkg") for line in lines))
                self.assertIn("MLSD", client.sendcmd("FEAT"))
                facts = dict(client.mlsd())
                self.assertEqual(facts["app.pkg"]["size"], str(len(b"pkg payload")))
                self.assertTrue(client.sendcmd("MDTM app.pkg").startswith("213 "))
                client.quit()
            finally:
                client.close()
                server.close()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_normalize_rejects_parent_paths(self):
        with self.assertRaises(ValueError):
            normalize("/apps/../metadata.db")


if __name__ == "__main__":
    unittest.main()
