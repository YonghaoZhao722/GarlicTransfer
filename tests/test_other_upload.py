import json
import os
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from poc import dt_source
from poc.dt_metadata import create_other_metadata


class OtherUploadTests(unittest.TestCase):
    def make_case(self, folder):
        root = Path(folder)
        assets = root / "assets"
        (assets / "296").mkdir(parents=True)
        source_path = "/user/app/PPSA05688/app.pkg"
        header = bytearray(1304)
        struct.pack_into("<I", header, 0, 0x35504647)
        struct.pack_into("<I", header, 4, 296)
        struct.pack_into("<Q", header, 240, 8)
        header[276:276 + len(source_path)] = source_path.encode()
        final = bytearray(1304)
        struct.pack_into("<I", final, 0, 0x35504647)
        struct.pack_into("<I", final, 4, 296)
        final[274] = 1
        item = {
            "path": source_path,
            "file": "app.pkg",
            "size": 8,
            "sha256": "stale",
            "header": header.hex(),
        }
        manifest = {
            "entry_id": 296,
            "files": [item],
            "296": {"files": [item], "final_header": final.hex()},
        }
        (assets / "296" / "manifest.json").write_text(json.dumps(manifest))
        (assets / "296" / "app.pkg").write_bytes(b"pkg-data")
        source = root / "source.db"
        db = sqlite3.connect(source)
        db.execute(
            "CREATE TABLE dir_entry_info_tbl (id, path, partition, target_type, target_subtype, "
            "title_id, content_id, actual_size, transfer_size, required_size, required_zone_size)"
        )
        db.execute("INSERT INTO dir_entry_info_tbl VALUES (296, ?, 1, 1, 0, '', '', 8, 8, 8, 0)", ("/user/app/PPSA05688/",))
        db.commit()
        db.close()
        return root, assets, source

    def test_other_metadata_and_send_time_resize(self):
        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            output = root / "generated.db"
            manifest_output = root / "generated.json"
            create_other_metadata(output, source, [296], assets, manifest_output)
            (assets / "296" / "app.pkg").write_bytes(b"pkg-data!")
            environment = {
                "DT_ASSETS": str(assets),
                "DT_METADATA": str(output),
                "DT_MANIFEST": str(manifest_output),
                "DT_UPLOAD_KIND": "other",
            }
            with patch.dict(os.environ, environment), \
                 patch.object(dt_source, "send_record") as send_record:
                dt_source.send_save_chunks(None, None, None, 0, 2, 296)
            self.assertEqual(send_record.call_count, 3)
            with sqlite3.connect(output) as db:
                sizes = db.execute(
                    "SELECT actual_size, transfer_size, required_size FROM dir_entry_info_tbl"
                ).fetchone()
            self.assertEqual(sizes, (9, 9, 9))
            generated = json.loads(manifest_output.read_text())
            self.assertEqual(generated["296"]["files"][0]["size"], 9)


if __name__ == "__main__":
    unittest.main()
