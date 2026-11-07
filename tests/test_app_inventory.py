import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from poc.dt_backend import list_inventory


class AppInventoryTests(unittest.TestCase):
    def test_all_entry_types_are_categorized(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metadata.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE dir_entry_info_tbl (id, path, actual_size, target_type)")
            db.executemany(
                "INSERT INTO dir_entry_info_tbl VALUES (?, ?, ?, ?)",
                [
                    (1, "/system_data/priv/cache/profile/0x18C60EA2/", 475760, 0),
                    (349, "/user/app/PPSA98513/", 43763, 1),
                    (348, "/user/app/PPSA98513/sce_sys/", 16633876, 1),
                    (122, "/user/home/18c60ea2/savedata_prospero/PPSA21564/", 20971520, 3),
                    (87, "/user/av_contents/", 1056720102, 2),
                    (5, "/system_data/priv/appmeta/NPXS40074/", 160, 1),
                    (205, "/user/playgo/app/IV0005-PPSA01385_00-DIAGQAINSPECTION/00/", 2156, 1),
                ],
            )
            db.execute("CREATE TABLE user_info_tbl (user_id, user_name, login_id, online_id)")
            db.execute("INSERT INTO user_info_tbl VALUES (415633058, 'tester', 'tester@example.invalid', 'tester')")
            db.commit()
            db.close()

            output = io.StringIO()
            with redirect_stdout(output):
                list_inventory(path)
            event = json.loads(output.getvalue())
            self.assertEqual(event["type"], "inventory")
            self.assertEqual([entry["id"] for entry in event["entries"]], [1, 5, 87, 122, 205, 348, 349])
            entries = {entry["id"]: entry for entry in event["entries"]}
            self.assertEqual(entries[1]["kind"], "profile")
            self.assertEqual(entries[5]["kind"], "other")
            self.assertEqual(entries[87]["kind"], "media")
            self.assertEqual(entries[122]["kind"], "save")
            self.assertEqual(entries[122]["owner"], "18c60ea2")
            self.assertEqual(entries[205]["kind"], "other")
            self.assertEqual(entries[348]["kind"], "app")
            self.assertEqual(entries[349]["kind"], "app")
            self.assertEqual(entries[349]["owner"], "console")
            self.assertEqual(entries[349]["size"], 43763)


if __name__ == "__main__":
    unittest.main()
