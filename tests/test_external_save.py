import json
import os
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from poc import dt_source
from poc.dt_metadata import create_external_save


class ExternalSaveTests(unittest.TestCase):
    def test_new_container_creates_uploadable_library_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "sdimg_external"
            image.write_bytes(b"encrypted-save")
            assets = root / "cache"
            output = root / "metadata.db"
            manifest_output = root / "manifest.json"
            create_external_save(
                output, "18c60ea2", "PPSA12345", image, assets,
                user_name="tester", manifest_output=manifest_output,
            )
            entry_id = 0x10000
            with sqlite3.connect(output) as db:
                directory = db.execute(
                    "SELECT path, actual_size, transfer_size, required_size FROM dir_entry_info_tbl"
                ).fetchone()
                title = db.execute("SELECT title_id, size FROM users_savedata_tbl").fetchone()
            expected_path = "/user/home/18c60ea2/savedata_prospero/PPSA12345/"
            self.assertEqual(directory, (expected_path, len(b"encrypted-save"), len(b"encrypted-save"), len(b"encrypted-save")))
            self.assertEqual(title, ("PPSA12345", len(b"encrypted-save")))
            manifest = json.loads(manifest_output.read_text())
            item = manifest[str(entry_id)]["files"][0]
            self.assertEqual(item["path"], expected_path + "sdimg_external")
            self.assertEqual(struct.unpack_from("<I", bytes.fromhex(item["header"]), 4)[0], entry_id)
            self.assertEqual((assets / str(entry_id) / "sdimg_external").read_bytes(), b"encrypted-save")

            with patch.dict(os.environ, {
                "DT_ASSETS": str(assets),
                "DT_METADATA": str(output),
                "DT_MANIFEST": str(manifest_output),
                "DT_UPLOAD_KIND": "save",
            }), patch.object(dt_source, "send_record") as send_record:
                dt_source.send_save_chunks(None, None, None, 0, 2, entry_id)
            self.assertEqual(send_record.call_count, 3)
            self.assertEqual(send_record.call_args_list[0].args[4], bytes.fromhex(item["header"]))


if __name__ == "__main__":
    unittest.main()
