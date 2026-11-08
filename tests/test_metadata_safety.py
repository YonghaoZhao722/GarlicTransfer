import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from poc.dt_metadata import create_metadata, create_filtered_metadata


USER_ID = "138c0dbf"
ENTRY_ID = 137
TITLE_ID = "PPSA01289"


class MetadataSafetyTests(unittest.TestCase):
    def make_case(self, folder):
        root = Path(folder)
        assets = root / "assets"
        (assets / str(ENTRY_ID)).mkdir(parents=True)
        source_path = f"/user/home/{USER_ID}/savedata_prospero/{TITLE_ID}/save.bin"
        header = bytearray(1304)
        struct.pack_into("<I", header, 0, 0x35504647)
        struct.pack_into("<I", header, 4, ENTRY_ID)
        struct.pack_into("<Q", header, 240, 8)
        header[276:276 + len(source_path)] = source_path.encode()
        final_header = bytearray(1304)
        struct.pack_into("<I", final_header, 0, 0x35504647)
        struct.pack_into("<I", final_header, 4, ENTRY_ID)
        final_header[274] = 1
        item = {
            "path": source_path,
            "file": "save.bin",
            "size": 8,
            "sha256": "stale-is-allowed-for-same-size-edits",
            "header": header.hex(),
        }
        manifest = {
            "entry_id": ENTRY_ID,
            "files": [item],
            str(ENTRY_ID): {"files": [item], "final_header": final_header.hex()},
        }
        (assets / "manifest.json").write_text(json.dumps(manifest))
        (assets / str(ENTRY_ID) / "manifest.json").write_text(json.dumps(manifest))
        (assets / str(ENTRY_ID) / "save.bin").write_bytes(b"12345678")
        source = root / "source.db"
        template = Path(__file__).resolve().parent.parent / "poc" / "templates" / "single-save.db"
        shutil.copyfile(template, source)
        return root, assets, source

    def generate(self, root, assets, source, user_id=USER_ID):
        return create_filtered_metadata(
            root / "generated.db", source, user_id, "test-user", [ENTRY_ID],
            assets, root / "generated-manifest.json",
        )

    def test_same_user_same_size_edit_is_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            (assets / str(ENTRY_ID) / "save.bin").write_bytes(b"abcdefgh")
            output = self.generate(root, assets, source)
            self.assertTrue(output.is_file())
            self.assertTrue((root / "generated-manifest.json").is_file())

    def test_cross_user_upload_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            with self.assertRaisesRegex(ValueError, "cross-user upload is blocked"):
                self.generate(root, assets, source, user_id="18c60eaa")
            self.assertFalse((root / "generated.db").exists())

    def test_resized_file_regenerates_manifest_and_metadata(self):
        import hashlib
        import sqlite3
        from contextlib import closing

        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            payload = b"123456789"
            (assets / str(ENTRY_ID) / "save.bin").write_bytes(payload)
            output = self.generate(root, assets, source)

            generated = json.loads((root / "generated-manifest.json").read_text())
            item = generated[str(ENTRY_ID)]["files"][0]
            self.assertEqual(item["size"], len(payload))
            self.assertEqual(item["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(struct.unpack_from("<Q", bytes.fromhex(item["header"]), 240)[0], len(payload))
            with closing(sqlite3.connect(output)) as db:
                sizes = db.execute(
                    "SELECT actual_size, transfer_size, required_size FROM dir_entry_info_tbl"
                ).fetchone()
                savedata_size = db.execute("SELECT size FROM users_savedata_tbl").fetchone()[0]
            self.assertEqual(sizes, (len(payload), len(payload), len(payload)))
            self.assertEqual(savedata_size, len(payload))

    def test_stale_header_size_is_regenerated(self):
        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            manifest_path = assets / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            header = bytearray.fromhex(manifest["files"][0]["header"])
            struct.pack_into("<Q", header, 240, 9)
            manifest["files"][0]["header"] = header.hex()
            manifest[str(ENTRY_ID)]["files"][0]["header"] = header.hex()
            manifest_path.write_text(json.dumps(manifest))
            self.generate(root, assets, source)
            generated = json.loads((root / "generated-manifest.json").read_text())
            item = generated[str(ENTRY_ID)]["files"][0]
            self.assertEqual(item["size"], 8)
            self.assertEqual(struct.unpack_from("<Q", bytes.fromhex(item["header"]), 240)[0], 8)

    def test_header_path_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            manifest_path = assets / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            header = bytearray.fromhex(manifest["files"][0]["header"])
            header[276:1300] = bytes(1024)
            manifest["files"][0]["header"] = header.hex()
            manifest[str(ENTRY_ID)]["files"][0]["header"] = header.hex()
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "mismatched manifest and GFP5 paths"):
                self.generate(root, assets, source)

    def test_regional_mismatch_uses_the_payload_title_and_size(self):
        import sqlite3
        from contextlib import closing

        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            wrong_path = f"/user/home/{USER_ID}/savedata_prospero/PPSA00000/"
            payload_path = f"/user/home/{USER_ID}/savedata_prospero/{TITLE_ID}/"
            with closing(sqlite3.connect(source)) as db:
                db.execute(
                    "UPDATE dir_entry_info_tbl SET path = ?, actual_size = 16, transfer_size = 16, required_size = 16 WHERE id = ?",
                    (wrong_path, ENTRY_ID),
                )
                db.execute(
                    "INSERT INTO dir_entry_info_tbl VALUES (122, ?, 1, 3, 0, '', '', 8, 8, 8, 0)",
                    (payload_path,),
                )
                db.commit()

            (assets / str(ENTRY_ID) / "save.bin").write_bytes(b"123456789")
            output = self.generate(root, assets, source)
            with closing(sqlite3.connect(output)) as db:
                row = db.execute(
                    "SELECT id, path, actual_size, transfer_size, required_size FROM dir_entry_info_tbl"
                ).fetchone()
                titles = db.execute("SELECT title_id, size FROM users_savedata_tbl").fetchall()
            self.assertEqual(row, (ENTRY_ID, payload_path, 9, 9, 9))
            self.assertEqual(titles, [(TITLE_ID, 9)])

    def test_no_backup_files_have_zero_bu_size(self):
        import sqlite3
        from contextlib import closing

        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            output = create_metadata(
                root / "single.db", USER_ID, TITLE_ID, ENTRY_ID, assets, "test-user"
            )
            with closing(sqlite3.connect(output)) as db:
                self.assertEqual(db.execute("SELECT bu_size FROM users_savedata_tbl").fetchone()[0], 0)

    def test_backup_size_counts_only_backup_files(self):
        import sqlite3
        from contextlib import closing

        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            for name, payload in (("sdimg_sce_bu_save", b"0123456789"), ("sce_bu_save.bin", b"ab")):
                source_path = f"/user/home/{USER_ID}/savedata_prospero/{TITLE_ID}/{name}"
                header = bytearray(1304)
                struct.pack_into("<I", header, 0, 0x35504647)
                struct.pack_into("<I", header, 4, ENTRY_ID)
                struct.pack_into("<Q", header, 240, len(payload))
                header[276:276 + len(source_path)] = source_path.encode()
                item = {
                    "path": source_path,
                    "file": name,
                    "size": len(payload),
                    "sha256": "",
                    "header": header.hex(),
                }
                for manifest_path in (assets / "manifest.json", assets / str(ENTRY_ID) / "manifest.json"):
                    manifest = json.loads(manifest_path.read_text())
                    manifest["files"].append(item)
                    manifest[str(ENTRY_ID)]["files"].append(item)
                    manifest_path.write_text(json.dumps(manifest))
                (assets / str(ENTRY_ID) / name).write_bytes(payload)

            outputs = [
                create_metadata(root / "single.db", USER_ID, TITLE_ID, ENTRY_ID, assets, "test-user"),
                self.generate(root, assets, source),
            ]
            for output in outputs:
                with closing(sqlite3.connect(output)) as db:
                    self.assertEqual(
                        db.execute("SELECT size, bu_size FROM users_savedata_tbl").fetchone(),
                        (20, 12),
                    )

    def test_send_time_resize_refreshes_header_manifest_and_metadata(self):
        import os
        import sqlite3
        from contextlib import closing
        from unittest.mock import patch
        from poc import dt_source

        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            output = self.generate(root, assets, source)
            (assets / str(ENTRY_ID) / "save.bin").write_bytes(b"123456789")
            environment = {
                "DT_ASSETS": str(assets),
                "DT_MANIFEST": str(root / "generated-manifest.json"),
                "DT_METADATA": str(output),
            }
            with patch.dict(os.environ, environment), \
                 patch.object(dt_source, "send_record") as send_record:
                dt_source.send_save_chunks(None, None, None, 0, 2, ENTRY_ID)

            self.assertEqual(send_record.call_count, 3)
            header = send_record.call_args_list[0].args[4]
            self.assertEqual(struct.unpack_from("<Q", header, 240)[0], 9)
            generated = json.loads((root / "generated-manifest.json").read_text())
            self.assertEqual(generated[str(ENTRY_ID)]["files"][0]["size"], 9)
            with closing(sqlite3.connect(output)) as db:
                sizes = db.execute(
                    "SELECT actual_size, transfer_size, required_size FROM dir_entry_info_tbl"
                ).fetchone()
            self.assertEqual(sizes, (9, 9, 9))


if __name__ == "__main__":
    unittest.main()
