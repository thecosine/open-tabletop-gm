#!/usr/bin/env python3
"""Confirmation-gated apply, validation, and rollback for one approved migration package."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import prepare_mythlon_bard_to_warlock as prep


APPLY_PREFIX = "APPLY-MYTHLON-BARD-TO-WARLOCK"
ROLLBACK_PREFIX = "ROLLBACK-MYTHLON-BARD-TO-WARLOCK"
BACKUP_ROOT = Path.home() / ".local/share/open-tabletop-gm/migration-backups" / prep.MIGRATION_ID
TRANSACTION_SCHEMA_VERSION = 1
LIVE_RUNTIME_PATH = Path(__file__).resolve().with_name("mythlon_progression_live_runtime.py")
AT_FDCWD = -100
RENAME_EXCHANGE = 2


class ApplyFailure(prep.MigrationBlocked):
    pass


@contextmanager
def _exclusive_lock(path: Path, *, nonblocking: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(handle, operation)
        except BlockingIOError as exc:
            raise ApplyFailure(f"transaction lock is already held: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _json(path: Path, label: str) -> dict[str, Any]:
    value = prep.read_regular_once(path, label).parsed
    if not isinstance(value, dict):
        raise ApplyFailure(f"{label} is not a JSON object")
    return value


def _package_digest(package: Path) -> str:
    digest = _json(package / "package_manifest.json", "package manifest").get("package_content_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ApplyFailure("package content digest is invalid")
    return digest


def _deployment_digest() -> str:
    records = {
        "apply": prep.sha256_bytes(Path(__file__).read_bytes()),
        "runtime": prep.sha256_bytes(LIVE_RUNTIME_PATH.read_bytes()),
    }
    return prep.sha256_bytes(prep.canonical_json_bytes(records))


def apply_confirmation(package: Path, decision_id: str) -> str:
    return f"{APPLY_PREFIX}:{_package_digest(package)}:{_deployment_digest()}:{decision_id}"


def rollback_confirmation(transaction: Path) -> str:
    record = _json(transaction / "transaction.json", "transaction journal")
    return f'{ROLLBACK_PREFIX}:{record.get("transaction_id", "")}'


def _validate_decision_id(decision_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", decision_id):
        raise ApplyFailure("decision ID must be 8-128 safe identifier characters")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_durable(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        _fsync_directory(directory.parent)
        _fsync_directory(directory)


def _exchange_paths(first: Path, second: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ApplyFailure("atomic pathname exchange is unavailable; refusing unsafe migration")
    result = renameat2(
        AT_FDCWD, ctypes.c_char_p(os.fsencode(first)),
        AT_FDCWD, ctypes.c_char_p(os.fsencode(second)),
        RENAME_EXCHANGE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(second))


def _stage_exchange_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.migration-swap-", dir=path.parent)
    temporary_path = Path(temporary)
    completed = False
    try:
        os.fchmod(descriptor, mode)
        try:
            os.fchown(descriptor, uid, gid)
        except PermissionError as exc:
            current = os.fstat(descriptor)
            if (current.st_uid, current.st_gid) != (uid, gid):
                raise ApplyFailure(f"cannot preserve ownership for {path}") from exc
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
        completed = True
        return temporary_path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not completed:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _atomic_create(path: Path, data: bytes, *, mode: int, uid: int, gid: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.migration-create-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        try:
            os.fchown(descriptor, uid, gid)
        except PermissionError as exc:
            current = os.fstat(descriptor)
            if (current.st_uid, current.st_gid) != (uid, gid):
                raise ApplyFailure(f"cannot preserve ownership for {path}") from exc
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, path, follow_symlinks=False)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_bytes(path: Path, data: bytes, *, mode: int, uid: int, gid: int) -> None:
    prep.assert_no_symlink_components(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.migration-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        try:
            os.fchown(descriptor, uid, gid)
        except PermissionError as exc:
            current = os.fstat(descriptor)
            if (current.st_uid, current.st_gid) != (uid, gid):
                raise ApplyFailure(f"cannot preserve ownership for {path}") from exc
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    info = path.stat()
    _atomic_bytes(
        path,
        prep.canonical_json_bytes(value),
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
    )


def _finish_exchange_temp(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _transactional_exchange(
    transaction: Path,
    record: dict[str, Any],
    entry: dict[str, Any],
    data: bytes,
    *,
    operation: str,
) -> None:
    destination = Path(entry["destination"])
    expected = entry["source_sha256"] if operation == "install" else entry["installed_sha256"]
    new_hash = entry["installed_sha256"] if operation == "install" else entry["source_sha256"]
    staged = _stage_exchange_bytes(
        destination, data, mode=entry["mode"], uid=entry["uid"], gid=entry["gid"],
    )
    journal = transaction / "transaction.json"
    exchange = {
        "artifact": entry["artifact"],
        "destination": str(destination),
        "temporary": str(staged),
        "operation": operation,
        "expected_sha256": expected,
        "new_sha256": new_hash,
        "phase": "prepared",
    }
    record["exchange"] = exchange
    _atomic_json(journal, record)
    _exchange_paths(staged, destination)
    _fsync_directory(destination.parent)
    displaced = prep.sha256_bytes(staged.read_bytes())
    if displaced != expected:
        _exchange_paths(staged, destination)
        _fsync_directory(destination.parent)
        exchange["phase"] = "mismatch_restored"
        exchange["concurrent_sha256"] = displaced
        _atomic_json(journal, record)
        _finish_exchange_temp(staged)
        record["exchange"] = None
        record["committing"] = None
        _atomic_json(journal, record)
        raise ApplyFailure(f"destination changed at atomic commit: {destination}")
    exchange["phase"] = "committed"
    if operation == "install" and entry["artifact"] not in record["replaced"]:
        record["replaced"].append(entry["artifact"])
    if operation == "restore" and entry["artifact"] not in record.setdefault("restored", []):
        record["restored"].append(entry["artifact"])
    _atomic_json(journal, record)
    _finish_exchange_temp(staged)
    record["exchange"] = None
    record["committing"] = None
    _atomic_json(journal, record)


def _recover_exchange(transaction: Path, record: dict[str, Any]) -> None:
    exchange = record.get("exchange")
    if not exchange:
        return
    artifact = exchange.get("artifact")
    entry = next((item for item in record["contract"]["entries"] if item["artifact"] == artifact), None)
    if entry is None or exchange.get("destination") != entry["destination"]:
        raise ApplyFailure("journaled exchange identity is invalid")
    destination = Path(entry["destination"])
    temporary = Path(str(exchange.get("temporary", "")))
    if temporary.parent != destination.parent or not temporary.name.startswith(f".{destination.name}.migration-swap-"):
        raise ApplyFailure("journaled exchange temporary path is invalid")
    operation = exchange.get("operation")
    if operation not in {"install", "restore"}:
        raise ApplyFailure("journaled exchange operation is invalid")
    destination_hash = prep.sha256_bytes(destination.read_bytes())
    temporary_exists = temporary.is_file() and not temporary.is_symlink()
    temporary_hash = prep.sha256_bytes(temporary.read_bytes()) if temporary_exists else None
    source_hash = entry["source_sha256"]
    installed_hash = entry["installed_sha256"]
    if operation == "install":
        if destination_hash == installed_hash and temporary_hash is None and artifact in record.get("replaced", []):
            pass
        elif destination_hash == installed_hash and temporary_hash == source_hash:
            if artifact not in record["replaced"]:
                record["replaced"].append(artifact)
        elif destination_hash == installed_hash and temporary_hash not in {None, source_hash}:
            _exchange_paths(temporary, destination)
            _fsync_directory(destination.parent)
            destination_hash, temporary_hash = temporary_hash, installed_hash
        elif destination_hash not in {source_hash, installed_hash} and temporary_hash == installed_hash:
            record.setdefault("preserved_concurrent_changes", []).append({
                "artifact": artifact, "sha256": destination_hash,
            })
        elif (
            exchange.get("phase") == "mismatch_restored"
            and destination_hash == exchange.get("concurrent_sha256")
            and temporary_hash is None
        ):
            record.setdefault("preserved_concurrent_changes", []).append({
                "artifact": artifact, "sha256": destination_hash,
            })
        elif not (destination_hash == source_hash and temporary_hash in {None, installed_hash}):
            raise ApplyFailure("cannot recover interrupted install exchange")
    else:
        if destination_hash == source_hash and temporary_hash is None and artifact in record.get("restored", []):
            pass
        elif destination_hash == source_hash and temporary_hash == installed_hash:
            if artifact not in record.setdefault("restored", []):
                record["restored"].append(artifact)
        elif destination_hash == source_hash and temporary_hash not in {None, installed_hash}:
            _exchange_paths(temporary, destination)
            _fsync_directory(destination.parent)
            raise ApplyFailure("rollback exchange raced with a migrated destination change")
        elif not (destination_hash == installed_hash and temporary_hash in {None, source_hash}):
            raise ApplyFailure("cannot recover interrupted restore exchange")
    if temporary_exists and temporary.exists():
        _finish_exchange_temp(temporary)
    record["exchange"] = None
    record["committing"] = None
    _atomic_json(transaction / "transaction.json", record)


def _live_wrapper_bytes() -> bytes:
    values = {
        "PACKAGE_DIR": prep.ENGINE_DIR,
        "STATE_PATH": prep.ENGINE_DIR / "character_state.json",
        "INITIAL_STATE_PATH": prep.ENGINE_SOURCE_DIR / "initial_character_state.json",
        "TEMPLATE_PATH": prep.ENGINE_SOURCE_DIR / "initial_character_state.json",
        "PROGRESSION_PATH": prep.ENGINE_SOURCE_DIR / "progression.json",
        "TRUE_STATUS": prep.ENGINE_DIR / "True_Status.md",
        "MASKED_STATUS": prep.ENGINE_DIR / "Masked_Status.md",
        "LOCK_PATH": prep.ENGINE_DIR / "character_state.lock",
        "BACKUP_DIR": prep.ENGINE_DIR / "backups",
    }
    assignments = ",\n    ".join(f'"{name}": Path({str(path)!r})' for name, path in values.items())
    return (
        "#!/usr/bin/env python3\n"
        '"""Live-path adapter for the hash-verified approved migration engine."""\n'
        "from pathlib import Path\n\n"
        "from approved_mythlon_progression_runtime import main\n\n"
        "implementation = Path(__file__).resolve().with_name(\"approved_mythlon_progression.py\")\n"
        "paths = {\n    " + assignments + "\n}\n"
        "raise SystemExit(main(implementation_path=implementation, paths=paths))\n"
    ).encode("utf-8")


def _link_record(path: Path) -> dict[str, Any]:
    info = os.lstat(path)
    if not stat.S_ISLNK(info.st_mode):
        raise ApplyFailure(f"expected symlink: {path}")
    return {
        "path": str(path),
        "raw_target": os.readlink(path),
        "resolved_target": str(Path(os.path.realpath(path))),
        "identity": [info.st_dev, info.st_ino, info.st_mtime_ns],
    }


def _verify_bridge_links(expected: list[dict[str, Any]], *, require_identity: bool) -> None:
    for wanted in expected:
        current = _link_record(Path(wanted["path"]))
        fields = {"path", "raw_target", "resolved_target"}
        if require_identity:
            fields.add("identity")
        if any(current[field] != wanted[field] for field in fields):
            raise ApplyFailure(f'bridge topology changed: {wanted.get("name", wanted["path"])}')


def _verify_supplementals(expected: list[dict[str, Any]], *, present: bool) -> None:
    for item in expected:
        path = Path(item["path"])
        exists = os.path.lexists(path)
        if exists != present:
            raise ApplyFailure(f"supplemental engine presence is invalid: {path}")
        if present and (path.is_symlink() or prep.sha256_bytes(path.read_bytes()) != item["sha256"]):
            raise ApplyFailure(f"supplemental engine hash is invalid: {path}")


def _capture_contract(package: Path) -> dict[str, Any]:
    preservation = _json(package / "preservation_manifest.json", "preservation manifest")
    rollback = _json(package / "rollback_manifest.json", "rollback manifest")
    plan = _json(package / "migration_plan.json", "migration plan")
    register = _json(package / "unresolved_authority_register.json", "authority register")
    if plan.get("mode") != "coordinated-dry-run-only" or plan.get("writes_live_state") is not False:
        raise ApplyFailure("approved package must remain an inert dry-run package")
    unresolved_required = {
        item.get("id") for item in register.get("items", [])
        if item.get("classification") == "required_before_live_apply" and item.get("status") != "resolved"
    }
    if unresolved_required != {"final_migration_approval"}:
        raise ApplyFailure("technical authority is not complete or final approval is not the sole live gate")
    by_name = {item["artifact"]: item for item in preservation["artifacts"]}
    entries = []
    for item in rollback["entries"]:
        artifact = item["artifact"]
        candidate = by_name[artifact]["candidate_path"]
        candidate_hash = by_name[artifact]["candidate_sha256"]
        entries.append({
            **item,
            "candidate_relative_path": candidate,
            "candidate_sha256": candidate_hash,
            "installed_sha256": (
                prep.sha256_bytes(_live_wrapper_bytes())
                if artifact == "progression_script"
                else candidate_hash
            ),
        })
    approved_engine = next(entry for entry in entries if entry["artifact"] == "progression_script")
    supplementals = [{
        "path": str(prep.ENGINE_SOURCE_DIR / "approved_mythlon_progression.py"),
        "source_path": str(package / approved_engine["candidate_relative_path"]),
        "candidate_relative_path": approved_engine["candidate_relative_path"],
        "sha256": approved_engine["candidate_sha256"],
        "mode": approved_engine["mode"],
        "uid": approved_engine["uid"],
        "gid": approved_engine["gid"],
    }, {
        "path": str(prep.ENGINE_SOURCE_DIR / "approved_mythlon_progression_runtime.py"),
        "source_path": str(LIVE_RUNTIME_PATH),
        "candidate_relative_path": None,
        "sha256": prep.sha256_bytes(LIVE_RUNTIME_PATH.read_bytes()),
        "mode": approved_engine["mode"],
        "uid": approved_engine["uid"],
        "gid": approved_engine["gid"],
    }]
    for item in supplementals:
        if os.path.lexists(item["path"]):
            raise ApplyFailure(f'migration supplemental path already exists: {item["path"]}')
    return {
        "entries": entries,
        "supplementals": supplementals,
        "bridge_links": preservation["bridge_links"],
        "artifacts": preservation["artifacts"],
    }


def _verify_sources(contract: dict[str, Any], *, installed: bool) -> None:
    changed = {entry["artifact"]: entry for entry in contract["entries"]}
    for item in contract["artifacts"]:
        name = item["artifact"]
        path = Path(item["source_path"])
        expected = (
            (changed[name]["installed_sha256"] if name in changed else item["candidate_sha256"])
            if installed and (name in changed or name == "bridge_character_state")
            else item["sha256"]
        )
        if prep.sha256_bytes(path.read_bytes()) != expected:
            raise ApplyFailure(f"artifact hash mismatch: {name}")
    _verify_bridge_links(contract["bridge_links"], require_identity=True)


def _verify_migrated_semantics(contract: dict[str, Any]) -> None:
    state = json.loads(prep.ENGINE_DIR.joinpath("character_state.json").read_text(encoding="utf-8"))
    classes = state.get("character", {}).get("classes", {})
    if set(classes) != {"rogue", "warlock", "wizard"}:
        raise ApplyFailure("installed state class set is not Rogue/Warlock/Wizard")
    serialized = json.dumps({
        "classes": classes,
        "features": state.get("character", {}).get("features", {}),
        "spellcasting": state.get("character", {}).get("spellcasting", {}),
    }).casefold()
    if '"bard"' in serialized:
        raise ApplyFailure("installed active mechanics still contain Bard")
    progression = json.loads(prep.ENGINE_SOURCE_DIR.joinpath("progression.json").read_text(encoding="utf-8"))
    initial = json.loads(prep.ENGINE_SOURCE_DIR.joinpath("initial_character_state.json").read_text(encoding="utf-8"))
    if set(progression) != {"rogue", "warlock", "wizard"}:
        raise ApplyFailure("installed progression tracks can recreate a superseded class")
    if set(initial.get("character", {}).get("classes", {})) != {"rogue", "warlock", "wizard"}:
        raise ApplyFailure("installed reset template can recreate a superseded class")
    script = prep.ENGINE_SOURCE_DIR.joinpath("mythlon_progression.py").read_bytes()
    compile(script, str(prep.ENGINE_SOURCE_DIR / "mythlon_progression.py"), "exec")
    _verify_supplementals(contract["supplementals"], present=True)


def validate_live(transaction: Path) -> dict[str, Any]:
    transaction = transaction.resolve()
    record = _json(transaction / "transaction.json", "transaction journal")
    if record.get("migration_id") != prep.MIGRATION_ID or record.get("status") != "applied":
        raise ApplyFailure("transaction is not in applied state")
    contract = record["contract"]
    _verify_sources(contract, installed=True)
    _verify_supplementals(contract["supplementals"], present=True)
    _verify_migrated_semantics(contract)
    return {"valid": True, "status": "applied", "transaction": str(transaction)}


def _new_backup_path(root: Path, package_digest: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return root / f"{stamp}-{package_digest[:16]}"


def _create_backup(root: Path, package: Path, decision_id: str, contract: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    _mkdir_durable(root)
    transaction = _new_backup_path(root, _package_digest(package))
    files: dict[str, bytes] = {}
    for entry in contract["entries"]:
        data = Path(entry["destination"]).read_bytes()
        if prep.sha256_bytes(data) != entry["source_sha256"]:
            raise ApplyFailure(f'source became stale before backup: {entry["artifact"]}')
        files[f'originals/{entry["artifact"]}'] = data
    transaction_id = transaction.name
    record = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "migration_id": prep.MIGRATION_ID,
        "transaction_id": transaction_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "decision_id": decision_id,
        "package": str(package),
        "package_digest": _package_digest(package),
        "deployment_digest": _deployment_digest(),
        "status": "backed_up",
        "replaced": [],
        "exchange": None,
        "committing": None,
        "created_supplementals": [],
        "contract": contract,
    }
    files["transaction.json"] = prep.canonical_json_bytes(record)
    prep._write_tree_exclusive(transaction, files)
    _fsync_directory(root)
    for entry in contract["entries"]:
        backup = transaction / "originals" / entry["artifact"]
        if prep.sha256_bytes(backup.read_bytes()) != entry["source_sha256"]:
            raise ApplyFailure(f'backup verification failed: {entry["artifact"]}')
    return transaction, record


def _remove_supplementals(contract: dict[str, Any], created: set[str]) -> None:
    for item in reversed(contract["supplementals"]):
        path = Path(item["path"])
        if str(path) not in created:
            continue
        if not os.path.lexists(path):
            continue
        if path.is_symlink() or prep.sha256_bytes(path.read_bytes()) != item["sha256"]:
            raise ApplyFailure(f"refusing to remove changed supplemental engine: {path}")
        path.unlink()
        _fsync_directory(path.parent)


def _validate_transaction(transaction: Path, record: dict[str, Any]) -> None:
    if (
        record.get("schema_version") != TRANSACTION_SCHEMA_VERSION
        or record.get("migration_id") != prep.MIGRATION_ID
        or record.get("transaction_id") != transaction.name
        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("package_digest")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("deployment_digest")))
    ):
        raise ApplyFailure("transaction journal identity is invalid")
    contract = record.get("contract")
    if not isinstance(contract, dict) or set(contract) != {
        "entries", "supplementals", "bridge_links", "artifacts",
    }:
        raise ApplyFailure("transaction contract is malformed")
    artifacts = {item.get("artifact"): item for item in contract["artifacts"] if isinstance(item, dict)}
    expected_paths = prep.coordinated_source_paths()
    if set(artifacts) != set(expected_paths) or any(
        Path(artifacts[name].get("source_path", "")) != path for name, path in expected_paths.items()
    ):
        raise ApplyFailure("transaction artifact paths are invalid")
    category_a = {name for name, item in artifacts.items() if item.get("category") == "A"}
    entries = contract["entries"]
    if not isinstance(entries, list) or {item.get("artifact") for item in entries if isinstance(item, dict)} != category_a:
        raise ApplyFailure("transaction replacement set is invalid")
    for entry in entries:
        artifact = entry["artifact"]
        if entry.get("destination") != artifacts[artifact].get("source_path"):
            raise ApplyFailure(f"transaction destination is invalid: {artifact}")
        for field in ("source_sha256", "candidate_sha256", "installed_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get(field))):
                raise ApplyFailure(f"transaction hash is invalid: {artifact}/{field}")
        backup = transaction / "originals" / artifact
        if not backup.is_file() or backup.is_symlink() or prep.sha256_bytes(backup.read_bytes()) != entry["source_sha256"]:
            raise ApplyFailure(f"transaction backup is invalid: {artifact}")
    expected_supplementals = {
        prep.ENGINE_SOURCE_DIR / "approved_mythlon_progression.py",
        prep.ENGINE_SOURCE_DIR / "approved_mythlon_progression_runtime.py",
    }
    actual_supplementals = {
        Path(item.get("path", "")) for item in contract["supplementals"] if isinstance(item, dict)
    }
    if actual_supplementals != expected_supplementals:
        raise ApplyFailure("transaction supplemental set is invalid")
    links = {item.get("name"): item for item in contract["bridge_links"] if isinstance(item, dict)}
    if set(links) != set(prep.BRIDGE_LINKS):
        raise ApplyFailure("transaction bridge set is invalid")
    for name, (expected_path, expected_target) in prep.BRIDGE_LINKS.items():
        item = links[name]
        if Path(item.get("path", "")) != expected_path or Path(item.get("resolved_target", "")) != expected_target:
            raise ApplyFailure(f"transaction bridge path is invalid: {name}")
        raw = Path(item.get("raw_target", ""))
        resolved = prep.lexical_absolute(raw if raw.is_absolute() else expected_path.parent / raw)
        if resolved != expected_target:
            raise ApplyFailure(f"transaction bridge target is invalid: {name}")


def _preflight_restore(transaction: Path, record: dict[str, Any]) -> None:
    _validate_transaction(transaction, record)
    contract = record["contract"]
    _verify_bridge_links(contract["bridge_links"], require_identity=True)
    replaced = set(record.get("replaced", []))
    concurrent = []
    for entry in contract["entries"]:
        current = prep.sha256_bytes(Path(entry["destination"]).read_bytes())
        if current not in {entry["source_sha256"], entry["installed_sha256"]}:
            if entry["artifact"] in replaced or record.get("committing") == entry["artifact"]:
                raise ApplyFailure(f'refusing rollback after migrated destination change: {entry["artifact"]}')
            concurrent.append({"artifact": entry["artifact"], "sha256": current})
    record["preserved_concurrent_changes"] = concurrent
    for item in contract["supplementals"]:
        path = Path(item["path"])
        if os.path.lexists(path) and (path.is_symlink() or prep.sha256_bytes(path.read_bytes()) != item["sha256"]):
            raise ApplyFailure(f"refusing rollback after supplemental change: {path}")


def _verify_restored(contract: dict[str, Any], concurrent: list[dict[str, str]]) -> None:
    concurrent_hashes = {item["artifact"]: item["sha256"] for item in concurrent}
    for entry in contract["entries"]:
        expected = concurrent_hashes.get(entry["artifact"], entry["source_sha256"])
        if prep.sha256_bytes(Path(entry["destination"]).read_bytes()) != expected:
            raise ApplyFailure(f'exact restoration failed: {entry["artifact"]}')
    _verify_bridge_links(contract["bridge_links"], require_identity=True)
    _verify_supplementals(contract["supplementals"], present=False)


def _restore(transaction: Path, record: dict[str, Any], *, automatic: bool) -> dict[str, Any]:
    _validate_transaction(transaction, record)
    _recover_exchange(transaction, record)
    _preflight_restore(transaction, record)
    contract = record["contract"]
    replaced = set(record.get("replaced", []))
    created = set(record.get("created_supplementals", []))
    for entry in contract["entries"]:
        if prep.sha256_bytes(Path(entry["destination"]).read_bytes()) == entry["installed_sha256"]:
            replaced.add(entry["artifact"])
    for item in contract["supplementals"]:
        path = Path(item["path"])
        if path.is_file() and not path.is_symlink() and prep.sha256_bytes(path.read_bytes()) == item["sha256"]:
            created.add(str(path))
    record["status"] = "rolling_back"
    _atomic_json(transaction / "transaction.json", record)
    _remove_supplementals(contract, created)
    for entry in reversed(contract["entries"]):
        if entry["artifact"] not in replaced:
            continue
        backup = transaction / "originals" / entry["artifact"]
        data = prep.read_regular_once(backup, f'backup {entry["artifact"]}').data
        if prep.sha256_bytes(data) != entry["source_sha256"]:
            raise ApplyFailure(f'backup is corrupt: {entry["artifact"]}')
        destination = Path(entry["destination"])
        current_hash = prep.sha256_bytes(destination.read_bytes())
        if current_hash not in {entry["source_sha256"], entry["installed_sha256"]}:
            raise ApplyFailure(f'refusing to overwrite post-transaction change: {entry["artifact"]}')
        if current_hash != entry["source_sha256"]:
            record["committing"] = entry["artifact"]
            _atomic_json(transaction / "transaction.json", record)
            _transactional_exchange(transaction, record, entry, data, operation="restore")
    concurrent = record.get("preserved_concurrent_changes", [])
    _verify_restored(contract, concurrent)
    base_status = "automatically_restored" if automatic else "rolled_back"
    record["status"] = f"{base_status}_with_concurrent_changes" if concurrent else base_status
    record["restored_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(transaction / "transaction.json", record)
    return {"valid": True, "status": record["status"], "transaction": str(transaction)}


def rollback(transaction: Path, confirmation: str) -> dict[str, Any]:
    transaction = transaction.resolve()
    record = _json(transaction / "transaction.json", "transaction journal")
    _validate_transaction(transaction, record)
    expected = f'{ROLLBACK_PREFIX}:{record.get("transaction_id", "")}'
    if confirmation != expected:
        raise ApplyFailure(f"rollback requires exact confirmation: {expected}")
    if record.get("status") in {
        "rolled_back", "automatically_restored",
        "rolled_back_with_concurrent_changes", "automatically_restored_with_concurrent_changes",
    }:
        return {"valid": True, "status": record["status"], "transaction": str(transaction)}
    if record.get("status") not in {"applied", "applying", "backed_up", "rollback_failed", "rolling_back"}:
        raise ApplyFailure("transaction status cannot be rolled back")
    state_path = prep.ENGINE_DIR / "character_state.json"
    with (
        _exclusive_lock(transaction.parent / ".apply.lock", nonblocking=True),
        _exclusive_lock(prep.ENGINE_DIR / "character_state.lock", nonblocking=False),
        _exclusive_lock(state_path.with_suffix(state_path.suffix + ".lock"), nonblocking=False),
        _exclusive_lock(state_path, nonblocking=False),
    ):
        record = _json(transaction / "transaction.json", "transaction journal")
        return _restore(transaction, record, automatic=False)


def apply(
    package: Path,
    decision_id: str,
    confirmation: str,
    *,
    expected_source_paths: dict[str, Path] | None = None,
    expected_bridge_links: dict[str, prep.BridgeSnapshot] | None = None,
    backup_root: Path | None = None,
    fail_after: int | None = None,
) -> dict[str, Any]:
    package = package.resolve()
    _validate_decision_id(decision_id)
    prep.validate_coordinated_package(
        package,
        expected_source_paths=expected_source_paths,
        expected_bridge_links=expected_bridge_links,
    )
    validated_package_digest = _package_digest(package)
    validated_deployment_digest = _deployment_digest()
    expected_confirmation = (
        f"{APPLY_PREFIX}:{validated_package_digest}:{validated_deployment_digest}:{decision_id}"
    )
    if confirmation != expected_confirmation:
        raise ApplyFailure(f"apply requires exact confirmation: {expected_confirmation}")
    contract = _capture_contract(package)
    prep.validate_coordinated_package(
        package,
        expected_source_paths=expected_source_paths,
        expected_bridge_links=expected_bridge_links,
    )
    if _package_digest(package) != validated_package_digest:
        raise ApplyFailure("package changed after confirmation validation")
    if _deployment_digest() != validated_deployment_digest:
        raise ApplyFailure("deployment mechanism changed after confirmation validation")
    _verify_sources(contract, installed=False)
    root = (backup_root or BACKUP_ROOT).resolve()
    _mkdir_durable(root)
    lock_path = root / ".apply.lock"
    state_path = prep.ENGINE_DIR / "character_state.json"
    with (
        _exclusive_lock(lock_path, nonblocking=True),
        _exclusive_lock(prep.ENGINE_DIR / "character_state.lock", nonblocking=False),
        _exclusive_lock(state_path.with_suffix(state_path.suffix + ".lock"), nonblocking=False),
        _exclusive_lock(state_path, nonblocking=False),
    ):
        _verify_sources(contract, installed=False)
        transaction, record = _create_backup(root, package, decision_id, contract)
        journal = transaction / "transaction.json"
        try:
            record["status"] = "applying"
            _atomic_json(journal, record)
            writes = 0
            for entry in contract["entries"]:
                candidate = prep.read_regular_once(package / entry["candidate_relative_path"], f'candidate {entry["artifact"]}').data
                if prep.sha256_bytes(candidate) != entry["candidate_sha256"]:
                    raise ApplyFailure(f'candidate changed after validation: {entry["artifact"]}')
                installed = _live_wrapper_bytes() if entry["artifact"] == "progression_script" else candidate
                if prep.sha256_bytes(installed) != entry["installed_sha256"]:
                    raise ApplyFailure(f'installed projection is invalid: {entry["artifact"]}')
                destination = Path(entry["destination"])
                if prep.sha256_bytes(destination.read_bytes()) != entry["source_sha256"]:
                    raise ApplyFailure(f'destination changed after backup: {entry["artifact"]}')
                record["committing"] = entry["artifact"]
                _atomic_json(journal, record)
                _transactional_exchange(transaction, record, entry, installed, operation="install")
                writes += 1
                if fail_after == writes:
                    raise ApplyFailure("injected apply failure")
            for item in contract["supplementals"]:
                path = Path(item["path"])
                if os.path.lexists(path):
                    raise ApplyFailure(f"supplemental engine path became occupied: {path}")
                source_path = (
                    package / item["candidate_relative_path"]
                    if item["candidate_relative_path"] is not None
                    else Path(item["source_path"])
                )
                source = prep.read_regular_once(source_path, "approved engine supplemental").data
                if prep.sha256_bytes(source) != item["sha256"]:
                    raise ApplyFailure("approved engine supplemental changed after validation")
                _atomic_create(path, source, mode=item["mode"], uid=item["uid"], gid=item["gid"])
                record["created_supplementals"].append(str(path))
                _atomic_json(journal, record)
                writes += 1
                if fail_after == writes:
                    raise ApplyFailure("injected apply failure")
            _verify_sources(contract, installed=True)
            _verify_migrated_semantics(contract)
            _verify_sources(contract, installed=True)
            record["status"] = "applied"
            record["applied_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_json(journal, record)
            return {
                "valid": True,
                "status": "applied",
                "transaction": str(transaction),
                "rollback_confirmation": f'{ROLLBACK_PREFIX}:{record["transaction_id"]}',
            }
        except BaseException as apply_error:
            try:
                _restore(transaction, record, automatic=True)
            except BaseException as restore_error:
                record["status"] = "rollback_failed"
                record["failure"] = f"apply={apply_error!r}; rollback={restore_error!r}"
                _atomic_json(journal, record)
                raise ApplyFailure(f"apply failed and automatic restoration failed: {restore_error}") from apply_error
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("confirmation", "apply", "validate-live", "rollback"))
    parser.add_argument("--package", type=Path)
    parser.add_argument("--transaction", type=Path)
    parser.add_argument("--decision-id")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    try:
        if args.command == "confirmation":
            if args.package is None or args.decision_id is None:
                raise ApplyFailure("confirmation requires --package and --decision-id")
            _validate_decision_id(args.decision_id)
            prep.validate_coordinated_package(args.package)
            print(apply_confirmation(args.package, args.decision_id))
            return 0
        if args.command == "apply":
            if args.package is None or args.decision_id is None:
                raise ApplyFailure("apply requires --package and --decision-id")
            result = apply(args.package, args.decision_id, args.confirm or "")
        elif args.command == "validate-live":
            if args.transaction is None:
                raise ApplyFailure("validate-live requires --transaction")
            result = validate_live(args.transaction)
        else:
            if args.transaction is None:
                raise ApplyFailure("rollback requires --transaction")
            result = rollback(args.transaction, args.confirm or "")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (prep.MigrationBlocked, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
