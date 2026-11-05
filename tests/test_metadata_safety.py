import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from poc.dt_metadata import create_filtered_metadata


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

    def test_resized_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            (assets / str(ENTRY_ID) / "save.bin").write_bytes(b"123456789")
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                self.generate(root, assets, source)
            self.assertFalse((root / "generated.db").exists())

    def test_header_size_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, assets, source = self.make_case(folder)
            manifest_path = assets / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            header = bytearray.fromhex(manifest["files"][0]["header"])
            struct.pack_into("<Q", header, 240, 9)
            manifest["files"][0]["header"] = header.hex()
            manifest[str(ENTRY_ID)]["files"][0]["header"] = header.hex()
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                self.generate(root, assets, source)

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

    def test_send_time_resize_is_rejected_before_any_record(self):
        import os
        from unittest.mock import patch
        from poc import dt_source

        with tempfile.TemporaryDirectory() as folder:
            root, assets, _ = self.make_case(folder)
            (assets / str(ENTRY_ID) / "save.bin").write_bytes(b"123456789")
            with patch.dict(os.environ, {"DT_ASSETS": str(assets), "DT_MANIFEST": str(assets / "manifest.json")}), \
                 patch.object(dt_source, "send_record") as send_record:
                with self.assertRaisesRegex(ValueError, "size mismatch"):
                    dt_source.send_save_chunks(None, None, None, 0, 2, ENTRY_ID)
                send_record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
