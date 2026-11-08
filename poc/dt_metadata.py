#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import struct
import time
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = PROJECT_ROOT / "poc" / "templates" / "single-save.db"


def validate_user(user_id, user_name):
    if not re.fullmatch(r"[0-9a-fA-F]{8}", user_id):
        raise ValueError("user ID must be exactly 8 hexadecimal digits")
    if not user_name or "\0" in user_name:
        raise ValueError("user name is required")
    return user_id.lower()


def validate_save_owner(files, user_id):
    for item in files:
        parts = item.get("path", "").strip("/").split("/")
        if len(parts) < 3 or parts[:2] != ["user", "home"]:
            raise ValueError("save manifest has an invalid owner path")
        if parts[2].lower() != user_id:
            raise ValueError(
                f"cross-user upload is blocked to prevent save corruption: save belongs to {parts[2]}, not {user_id}"
            )


def validate_payload(path, entry_id, item):
    try:
        header = bytearray.fromhex(item["header"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"save file {item.get('file', '')} has an invalid GFP5 header") from exc
    if len(header) != 1304 or struct.unpack_from("<I", header)[0] != 0x35504647:
        raise ValueError(f"save file {item['file']} has an invalid GFP5 header")
    if struct.unpack_from("<I", header, 4)[0] != entry_id:
        raise ValueError(f"save file {item['file']} has a mismatched GFP5 entry ID")
    if header[276:1300].split(b"\0", 1)[0].decode(errors="strict") != item.get("path"):
        raise ValueError(f"save file {item['file']} has mismatched manifest and GFP5 paths")
    actual_size = path.stat().st_size
    if actual_size <= 0:
        raise ValueError(f"save file {item['file']} is empty")
    manifest_size = int(item.get("size", 0))
    header_size = struct.unpack_from("<Q", header, 240)[0]
    if actual_size != manifest_size or actual_size != header_size:
        print(
            f"[metadata] {item['file']} using disk size {actual_size} "
            f"(manifest={manifest_size}, GFP5={header_size})",
            flush=True,
        )
    struct.pack_into("<Q", header, 240, actual_size)
    item["header"] = header.hex()
    item["size"] = actual_size
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(0x200000):
            digest.update(chunk)
    item["sha256"] = digest.hexdigest()
    return actual_size


def load_manifest(assets, entry_id=None, expected_user_id=None):
    assets = Path(assets).expanduser()
    manifest_path = assets / "manifest.json"
    if not manifest_path.is_file() and entry_id is not None:
        manifest_path = assets / str(entry_id) / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"save manifest not found under {assets}")
    manifest = json.loads(manifest_path.read_text())
    if not manifest:
        raise ValueError("no downloaded saves were found; download save data before uploading")
    if entry_id is None:
        entry_id = manifest.get("entry_id")
        if entry_id is None:
            numeric_keys = [key for key in manifest if key.isdigit()]
            if len(numeric_keys) != 1:
                raise ValueError("select the save entries to upload; the manifest contains multiple saves")
            entry_id = int(numeric_keys[0])
    entry_id = int(entry_id)
    entry = manifest.get(str(entry_id), {})
    files = entry.get("files", manifest.get("files", []))
    final_header = entry.get("final_header")
    if not files:
        raise ValueError(f"save manifest for entry {entry_id} contains no files")
    if expected_user_id is not None:
        validate_save_owner(files, expected_user_id)
    if not final_header:
        raise ValueError(f"entry {entry_id} lacks a GFP5 final header; redownload it with this version")
    for item in files:
        name = Path(item.get("file", "")).name
        if not name or name != item.get("file"):
            raise ValueError(f"entry {entry_id} has an invalid save filename")
        candidates = (assets / str(entry_id) / name, assets / name)
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise ValueError(f"save file for entry {entry_id} is missing: {name}")
        if not item.get("header"):
            raise ValueError(f"save file {name} lacks its GFP5 header; redownload the save")
        validate_payload(path, entry_id, item)
    final_bytes = bytes.fromhex(final_header)
    if len(final_bytes) != 1304 or struct.unpack_from("<I", final_bytes)[0] != 0x35504647:
        raise ValueError(f"entry {entry_id} has an invalid GFP5 final header")
    if struct.unpack_from("<I", final_bytes, 4)[0] != entry_id or not final_bytes[274]:
        raise ValueError(f"entry {entry_id} has a mismatched GFP5 final header")
    return entry_id, {"files": files, "final_header": final_header}


def write_upload_manifest(output, entries):
    output = Path(output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({str(entry_id): entry for entry_id, entry in entries.items()}, indent=2) + "\n")


def backup_size(files):
    return sum(
        int(item["size"])
        for item in files
        if item.get("file", "").startswith("sdimg_sce_bu_")
        or (item.get("file", "").startswith("sce_bu_") and item.get("file", "").endswith(".bin"))
    )


def sync_upload_metadata(metadata_path, entry_id, entries):
    entry = entries[str(entry_id)]
    title_id = infer_title_id("", entry["files"])
    total_size = sum(int(item["size"]) for item in entry["files"])
    bu_size = backup_size(
        item
        for candidate in entries.values()
        for item in candidate["files"]
        if infer_title_id("", [item]) == title_id
    )
    db = sqlite3.connect(metadata_path)
    try:
        cursor = db.execute(
            "UPDATE dir_entry_info_tbl SET actual_size = ?, transfer_size = ?, required_size = ? WHERE id = ?",
            (total_size, total_size, total_size, entry_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"metadata entry {entry_id} is missing for upload")
        db.execute(
            "UPDATE users_savedata_tbl SET "
            "size = (SELECT COALESCE(SUM(actual_size), 0) FROM dir_entry_info_tbl "
            "WHERE target_type = 3 AND path LIKE ?), bu_size = ? WHERE title_id = ?",
            (f"%/savedata_prospero/{title_id}/", bu_size, title_id),
        )
        db.commit()
    finally:
        db.close()


def sync_other_metadata(metadata_path, entry_id, entries):
    entry = entries[str(entry_id)]
    total_size = sum(int(item["size"]) for item in entry["files"])
    db = sqlite3.connect(metadata_path)
    try:
        row = db.execute(
            "SELECT path, target_type FROM dir_entry_info_tbl WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"metadata entry {entry_id} is missing for upload")
        source_path, target_type = row
        if target_type == 3:
            raise ValueError("savedata entries require savedata metadata generation")
        prefix = source_path.rstrip("/") + "/"
        for item in entry["files"]:
            if not item.get("path", "").startswith(prefix):
                raise ValueError(f"entry {entry_id} has a payload path outside {source_path}")
        db.execute(
            "UPDATE dir_entry_info_tbl SET actual_size = ?, transfer_size = ?, required_size = ? WHERE id = ?",
            (total_size, total_size, total_size, entry_id),
        )
        db.commit()
    finally:
        db.close()


def infer_title_id(title_id, files):
    if title_id:
        return title_id.upper()
    for item in files:
        parts = item.get("path", "").strip("/").split("/")
        for marker in ("savedata_prospero", "savedata"):
            if marker in parts:
                index = parts.index(marker)
                if index + 1 < len(parts) and re.fullmatch(r"[A-Za-z0-9]{9}", parts[index + 1]):
                    return parts[index + 1].upper()
    raise ValueError("title ID must be exactly 9 letters or digits")


def create_metadata(output, user_id, title_id, entry_id, assets, user_name="user", manifest_output=None):
    user_id = validate_user(user_id, user_name)
    entry_id, upload_entry = load_manifest(assets, entry_id, expected_user_id=user_id)
    files = upload_entry["files"]
    title_id = infer_title_id(title_id, files)
    if not re.fullmatch(r"[A-Z0-9]{9}", title_id):
        raise ValueError("title ID must be exactly 9 letters or digits")
    total_size = sum(int(item["size"]) for item in files)
    bu_size = backup_size(files)
    if total_size <= 0:
        raise ValueError("save files must not be empty")

    output = Path(output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE, output)
    db = sqlite3.connect(output)
    try:
        db.execute("DELETE FROM user_info_tbl")
        db.execute("DELETE FROM users_savedata_tbl")
        db.execute("DELETE FROM dir_entry_info_tbl")
        db.execute(
            "INSERT INTO user_info_tbl VALUES (?, ?, 0, 0, ?, ?, '', 'np', 1, 1, 0, ?, 1, 1)",
            (int(user_id, 16), user_name, user_name, user_name, uuid.uuid4().hex[:16]),
        )
        db.execute(
            "INSERT INTO users_savedata_tbl VALUES (?, ?, ?, ?, ?, 1, 1, 0, ?)",
            (int(user_id, 16), title_id, title_id, total_size, bu_size, int(time.time())),
        )
        save_path = f"/user/home/{user_id}/savedata_prospero/{title_id}/"
        db.execute(
            "INSERT INTO dir_entry_info_tbl VALUES (?, ?, 1, 3, 0, '', '', ?, ?, ?, 0)",
            (entry_id, save_path, total_size, total_size, total_size),
        )
        db.commit()
    finally:
        db.close()
    if manifest_output:
        write_upload_manifest(manifest_output, {entry_id: upload_entry})
    return output


def create_external_save(output, user_id, title_id, image_path, assets, user_name="user", manifest_output=None):
    user_id = validate_user(user_id, user_name)
    title_id = title_id.upper()
    if not re.fullmatch(r"[A-Z0-9]{9}", title_id):
        raise ValueError("title ID must be exactly 9 letters or digits")
    image_path = Path(image_path).expanduser()
    if not image_path.is_file() or image_path.stat().st_size <= 0:
        raise ValueError("external save container is missing or empty")
    entry_id = 0x10000
    assets = Path(assets).expanduser()
    entry_directory = assets / str(entry_id)
    entry_directory.mkdir(parents=True, exist_ok=True)
    destination = entry_directory / image_path.name
    if image_path.resolve() != destination.resolve():
        shutil.copyfile(image_path, destination)
    source_path = f"/user/home/{user_id}/savedata_prospero/{title_id}/{image_path.name}"
    header = bytearray(1304)
    struct.pack_into("<I", header, 0, 0x35504647)
    struct.pack_into("<I", header, 4, entry_id)
    struct.pack_into("<Q", header, 240, destination.stat().st_size)
    header[276:276 + len(source_path)] = source_path.encode()
    final_header = bytearray(1304)
    struct.pack_into("<I", final_header, 0, 0x35504647)
    struct.pack_into("<I", final_header, 4, entry_id)
    final_header[274] = 1
    item = {
        "path": source_path,
        "file": image_path.name,
        "size": destination.stat().st_size,
        "sha256": "",
        "header": header.hex(),
    }
    manifest = {
        "entry_id": entry_id,
        "files": [item],
        str(entry_id): {"files": [item], "final_header": final_header.hex()},
    }
    (entry_directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return create_metadata(output, user_id, title_id, entry_id, assets, user_name, manifest_output)


def target_path(source_path, user_id):
    parts = source_path.strip("/").split("/")
    if len(parts) < 4 or parts[:2] != ["user", "home"]:
        raise ValueError(f"unsupported save path in metadata: {source_path}")
    parts[2] = user_id
    result = "/" + "/".join(parts)
    return result + "/" if source_path.endswith("/") else result


def resolve_upload_layout(row, upload_entry, user_id, candidate_rows):
    files = upload_entry["files"]
    title_id = infer_title_id("", files)
    total_size = sum(int(item["size"]) for item in files)
    if total_size <= 0:
        raise ValueError("save files must not be empty")
    row_title = infer_title_id("", [{"path": row[1]}])
    if row_title != title_id:
        matches = []
        for candidate in candidate_rows:
            parts = candidate[1].strip("/").split("/")
            if len(parts) < 3 or parts[2].lower() != user_id:
                continue
            if infer_title_id("", [{"path": candidate[1]}]) != title_id:
                continue
            matches.append(candidate)
        if len(matches) != 1:
            raise ValueError(
                f"entry {row[0]} metadata is for {row_title}, but its save payload is for {title_id}; "
                "no unique matching directory entry exists"
            )
        row = (row[0], *matches[0][1:])
    layout = list(row)
    layout[1] = target_path(layout[1], user_id)
    layout[7] = layout[8] = layout[9] = total_size
    return title_id, layout


def create_filtered_metadata(
    output, source_metadata, user_id, user_name, entry_ids, assets, manifest_output=None
):
    user_id = validate_user(user_id, user_name)
    if not entry_ids:
        entry_ids = [None]
    upload_entries = {}
    for entry_id in entry_ids:
        actual_id, entry = load_manifest(assets, entry_id, expected_user_id=user_id)
        upload_entries[actual_id] = entry
    entry_ids = sorted(upload_entries)

    output = Path(output).expanduser()
    source_metadata = Path(source_metadata).expanduser()
    if output.resolve() == source_metadata.resolve():
        raise ValueError("filtered metadata output must differ from the source database")
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_metadata, output)
    placeholders = ",".join("?" for _ in entry_ids)
    db = sqlite3.connect(output)
    try:
        rows = db.execute(
            f"SELECT * FROM dir_entry_info_tbl WHERE target_type = 3 AND id IN ({placeholders}) ORDER BY id",
            entry_ids,
        ).fetchall()
        if {row[0] for row in rows} != set(entry_ids):
            raise ValueError("one or more selected save entries are absent from the source metadata")
        target_user = db.execute(
            "SELECT * FROM user_info_tbl WHERE user_id = ?", (int(user_id, 16),)
        ).fetchone()
        savedata_rows = db.execute("SELECT * FROM users_savedata_tbl").fetchall()
        candidate_rows = db.execute("SELECT * FROM dir_entry_info_tbl WHERE target_type = 3").fetchall()

        db.execute("DELETE FROM dir_entry_info_tbl")
        db.execute("DELETE FROM app_info_tbl")
        db.execute("DELETE FROM app_service_ac_tbl")
        db.execute("DELETE FROM app_service_fgc_tbl")
        db.execute("DELETE FROM capture_info_tbl")
        db.execute("DELETE FROM users_savedata_tbl")
        db.execute("DELETE FROM user_info_tbl")
        if target_user:
            transfer_user = list(target_user)
            transfer_user[7] = "np"
            transfer_user[8] = 1
            transfer_user[9] = 1
            transfer_user[10] = 0
            transfer_user[12] = 1
            transfer_user[13] = 1
            db.execute("INSERT INTO user_info_tbl VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", transfer_user)
        else:
            db.execute(
                "INSERT INTO user_info_tbl VALUES (?, ?, 0, 0, ?, ?, '', 'np', 1, 1, 0, ?, 1, 1)",
                (int(user_id, 16), user_name, user_name, user_name, uuid.uuid4().hex[:16]),
            )

        entries_by_title = {}
        for row in rows:
            title_id, layout = resolve_upload_layout(row, upload_entries[row[0]], user_id, candidate_rows)
            db.execute("INSERT INTO dir_entry_info_tbl VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", layout)
            entries_by_title.setdefault(title_id, []).append(layout)

        for title_id, title_rows in entries_by_title.items():
            original = next((row for row in savedata_rows if row[1].upper() == title_id), None)
            total_size = sum(int(row[7]) for row in title_rows)
            bu_size = backup_size(
                item
                for row in title_rows
                for item in upload_entries[row[0]]["files"]
            )
            db.execute(
                "INSERT INTO users_savedata_tbl VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(user_id, 16), title_id, title_id, total_size, bu_size,
                    original[5] if original else 1,
                    original[6] if original else 1,
                    original[7] if original else 0,
                    original[8] if original else int(time.time()),
                ),
            )
        db.commit()
    finally:
        db.close()
    write_upload_manifest(manifest_output or output.with_suffix(".manifest.json"), upload_entries)
    return output


def create_other_metadata(output, source_metadata, entry_ids, assets, manifest_output=None):
    if not entry_ids:
        raise ValueError("select at least one non-save entry to upload")
    output = Path(output).expanduser()
    source_metadata = Path(source_metadata).expanduser()
    if output.resolve() == source_metadata.resolve():
        raise ValueError("upload metadata output must differ from the source database")
    upload_entries = {}
    for entry_id in entry_ids:
        actual_id, entry = load_manifest(assets, entry_id)
        upload_entries[actual_id] = entry
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_metadata, output)
    for entry_id, entry in upload_entries.items():
        sync_other_metadata(output, entry_id, {str(entry_id): entry})
    write_upload_manifest(manifest_output or output.with_suffix(".manifest.json"), upload_entries)
    return output


def main():
    parser = argparse.ArgumentParser(description="Create filtered single-user Data Transfer metadata")
    parser.add_argument("--output", required=True)
    parser.add_argument("--source", help="downloaded metadata.db to filter")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--title-id", default="")
    parser.add_argument("--entry-id", type=int, action="append", default=[])
    parser.add_argument("--assets", required=True)
    parser.add_argument("--user-name", default="user")
    parser.add_argument("--manifest-output")
    parser.add_argument("--other", action="store_true", help="preserve non-savedata transfer entries")
    parser.add_argument("--external-image", type=Path, help="encrypted save container to import")
    args = parser.parse_args()
    if args.external_image:
        path = create_external_save(
            args.output, args.user_id, args.title_id, args.external_image,
            args.assets, args.user_name, args.manifest_output,
        )
    elif args.other:
        if not args.source:
            parser.error("--other requires --source")
        path = create_other_metadata(
            args.output, args.source, args.entry_id, args.assets, args.manifest_output,
        )
    elif args.source:
        path = create_filtered_metadata(
            args.output, args.source, args.user_id, args.user_name,
            args.entry_id, args.assets, args.manifest_output,
        )
    else:
        if len(args.entry_id) > 1:
            parser.error("single-save metadata accepts only one --entry-id without --source")
        path = create_metadata(
            args.output, args.user_id, args.title_id,
            args.entry_id[0] if args.entry_id else None, args.assets,
            args.user_name, args.manifest_output,
        )
    print(path)


if __name__ == "__main__":
    main()
