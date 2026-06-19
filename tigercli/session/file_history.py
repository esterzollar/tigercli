from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

FileHistoryCheckpointResult = dict[str, object]

FILE_HISTORY_AUTHOR_NAME = "TigerLiteCode Checkpoint"
FILE_HISTORY_AUTHOR_EMAIL = "tigercli-checkpoint@localhost"
MANIFEST_PATH = ".tigercli-file-history.json"

SIXTY_FOUR_HEX = re.compile(r"^files-[0-9a-f]{64}$")
FORTY_HEX = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def is_commit_hash(value: str) -> bool:
    return bool(FORTY_HEX.match(value))


def is_valid_stored_path(value: str) -> bool:
    return bool(SIXTY_FOUR_HEX.match(value))


def empty_manifest() -> dict[str, Any]:
    return {"version": 2, "files": {}}


def normalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for key, entry in sorted(manifest.get("files", {}).items()):
        if (
            not is_valid_stored_path(str(key))
            or not isinstance(entry, dict)
            or entry.get("mode") != "100644"
            or (entry.get("blob") is not None and not is_commit_hash(str(entry["blob"])))
        ):
            raise ValueError("Invalid file history manifest.")
        entry_path = entry.get("path", "")
        files[key] = {
            "path": str(Path(entry_path).resolve()),
            "blob": entry.get("blob"),
            "mode": "100644",
        }
    return {"version": 2, "files": files}


def is_same_file_history_entry(left: dict[str, Any], right: Optional[dict[str, Any]]) -> bool:
    if right is None:
        return False
    return left.get("path") == right.get("path") and left.get("blob") == right.get("blob") and left.get("mode") == right.get("mode")


def unique_absolute_paths(file_paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for fp in file_paths:
        resolved = str(Path(fp).resolve())
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def remove_tracked_file(file_path: str) -> None:
    p = Path(file_path)
    if not p.exists():
        return
    if p.is_dir():
        return
    p.unlink()


def get_file_history_git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", FILE_HISTORY_AUTHOR_NAME)
    env.setdefault("GIT_AUTHOR_EMAIL", FILE_HISTORY_AUTHOR_EMAIL)
    env.setdefault("GIT_COMMITTER_NAME", FILE_HISTORY_AUTHOR_NAME)
    env.setdefault("GIT_COMMITTER_EMAIL", FILE_HISTORY_AUTHOR_EMAIL)
    return env


class GitFileHistory:
    def __init__(self, project_root: str, git_dir: str) -> None:
        self._project_root = project_root
        self._git_dir = git_dir

    def _get_session_branch_ref(self, session_id: str) -> Optional[str]:
        if not re.match(r"^[A-Za-z0-9._-]+$", session_id):
            return None
        return f"refs/heads/{session_id}"

    def _spawn_git(
        self,
        args: list[str],
        input_data: Optional[bytes] = None,
        env: Optional[dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        git_args = [
            "-c", "core.autocrlf=false",
            "-c", "core.eol=lf",
            f"--git-dir={self._git_dir}",
            *args,
        ]
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        result = subprocess.run(
            ["git", *git_args],
            input=input_data,
            capture_output=True,
            env=merged_env,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            detail = (stderr or stdout or "").strip()
            raise RuntimeError(detail or f"git {' '.join(args)} failed")
        return result

    def _run_git(
        self,
        args: list[str],
        input_data: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> str:
        inp = input_data.encode("utf-8") if input_data else None
        result = self._spawn_git(args, input_data=inp, env=env)
        return result.stdout.decode("utf-8", errors="replace")

    def _run_git_buffer(
        self,
        args: list[str],
        input_data: Optional[bytes] = None,
        env: Optional[dict[str, str]] = None,
    ) -> bytes:
        result = self._spawn_git(args, input_data=input_data, env=env)
        return result.stdout

    def _create_commit(self, tree_hash: str, parent_hash: Optional[str], message: str) -> str:
        args = ["commit-tree", tree_hash]
        if parent_hash:
            args.extend(["-p", parent_hash])
        args.extend(["-m", message])
        return self._run_git(args, env=get_file_history_git_env()).strip()

    def _create_tree(self, manifest: dict[str, Any]) -> str:
        normalized = normalize_manifest(manifest)
        manifest_blob = self._hash_content(json.dumps(normalized, indent=2) + "\n")
        entries: list[str] = [f"100644 blob {manifest_blob}\t{MANIFEST_PATH}\0"]
        for key, entry in normalized.get("files", {}).items():
            blob = entry.get("blob")
            if not blob:
                continue
            mode = entry.get("mode", "100644")
            entries.append(f"{mode} blob {blob}\t{key}\0")
        return self._run_git(["mktree", "-z"], input_data="".join(entries)).strip()

    def _read_manifest(self, commit_hash: str) -> dict[str, Any]:
        buf = self._run_git_buffer(["cat-file", "blob", f"{commit_hash}:{MANIFEST_PATH}"])
        parsed = json.loads(buf.decode("utf-8"))
        if (
            not isinstance(parsed, dict)
            or parsed.get("version") not in (1, 2)
            or not isinstance(parsed.get("files"), dict)
        ):
            raise ValueError("Invalid file history manifest.")
        return normalize_manifest(parsed)

    def _read_blob(self, blob_hash: str) -> bytes:
        if not is_commit_hash(blob_hash):
            raise ValueError("Invalid file history blob hash.")
        return self._run_git_buffer(["cat-file", "blob", blob_hash])

    def _hash_file(self, file_path: str) -> str:
        blob_hash = self._run_git(["hash-object", "-w", "--", file_path]).strip()
        if not is_commit_hash(blob_hash):
            raise ValueError("Invalid file history blob hash.")
        return blob_hash

    def _hash_content(self, content: str) -> str:
        blob_hash = self._run_git(["hash-object", "-w", "--stdin"], input_data=content).strip()
        if not is_commit_hash(blob_hash):
            raise ValueError("Invalid file history blob hash.")
        return blob_hash

    def _get_file_key(self, file_path: str) -> str:
        h = hashlib.sha256(file_path.encode("utf-8")).hexdigest()
        return f"files-{h}"

    def ensure_session(self, session_id: str) -> Optional[str]:
        branch_ref = self._get_session_branch_ref(session_id)
        if branch_ref is None:
            return None
        try:
            git_dir_path = Path(self._git_dir)
            if not git_dir_path.exists():
                git_dir_path.parent.mkdir(parents=True, exist_ok=True)
                self._run_git(["init"])
            current = self.get_current_checkpoint_hash(session_id)
            if current:
                return current
            tree_hash = self._create_tree(empty_manifest())
            commit_hash = self._create_commit(tree_hash, None, "Initial checkpoint")
            self._run_git(["update-ref", branch_ref, commit_hash])
            return commit_hash
        except Exception:
            return None

    def get_current_checkpoint_hash(self, session_id: str) -> Optional[str]:
        branch_ref = self._get_session_branch_ref(session_id)
        if branch_ref is None or not Path(self._git_dir).exists():
            return None
        try:
            raw = self._run_git(["rev-parse", "--verify", f"{branch_ref}^{{commit}}"]).strip()
            return raw if is_commit_hash(raw) else None
        except Exception:
            return None

    def record_checkpoint(self, session_id: str, file_paths: list[str], message: str) -> Optional[str]:
        branch_ref = self._get_session_branch_ref(session_id)
        if branch_ref is None:
            return None
        absolute_paths = unique_absolute_paths(file_paths)
        if not absolute_paths:
            return self.get_current_checkpoint_hash(session_id)
        try:
            parent_hash = self.ensure_session(session_id)
            if parent_hash is None:
                return None
            manifest = self._read_manifest(parent_hash)
            for file_path in absolute_paths:
                key = self._get_file_key(file_path)
                p = Path(file_path)
                if not p.exists() or not p.is_file():
                    manifest["files"][key] = {
                        "path": file_path,
                        "blob": None,
                        "mode": "100644",
                    }
                else:
                    manifest["files"][key] = {
                        "path": file_path,
                        "blob": self._hash_file(file_path),
                        "mode": "100644",
                    }
            tree_hash = self._create_tree(manifest)
            parent_tree = self._run_git(["rev-parse", f"{parent_hash}^{{tree}}"]).strip()
            if tree_hash == parent_tree:
                return parent_hash
            commit_hash = self._create_commit(tree_hash, parent_hash, message)
            self._run_git(["update-ref", branch_ref, commit_hash, parent_hash])
            return commit_hash
        except Exception:
            return None

    def record_tracked_files_checkpoint(self, session_id: str, message: str) -> dict[str, Any]:
        current_hash = self.ensure_session(session_id)
        if current_hash is None:
            return {"checkpointHash": None, "changedFilePaths": []}
        try:
            manifest = self._read_manifest(current_hash)
            tracked_paths = sorted(
                entry["path"]
                for entry in manifest.get("files", {}).values()
                if isinstance(entry, dict) and entry.get("path")
            )
            if not tracked_paths:
                return {"checkpointHash": current_hash, "changedFilePaths": []}
            next_hash = self.record_checkpoint(session_id, tracked_paths, message)
            if next_hash is None:
                return {"checkpointHash": None, "changedFilePaths": []}
            next_manifest = self._read_manifest(next_hash)
            changed: list[str] = []
            prev_files = manifest.get("files", {})
            next_files = next_manifest.get("files", {})
            for key, entry in prev_files.items():
                if not is_same_file_history_entry(entry, next_files.get(key)):
                    next_entry = next_files.get(key)
                    changed.append(next_entry["path"] if next_entry else entry["path"])
            changed.sort()
            return {"checkpointHash": next_hash, "changedFilePaths": changed}
        except Exception:
            return {"checkpointHash": None, "changedFilePaths": []}

    def can_restore(self, session_id: str, checkpoint_hash: str) -> bool:
        if not is_commit_hash(checkpoint_hash):
            return False
        if self._get_session_branch_ref(session_id) is None:
            return False
        if not Path(self._git_dir).exists():
            return False
        try:
            self._run_git(["cat-file", "-e", f"{checkpoint_hash}^{{commit}}"])
            self._read_manifest(checkpoint_hash)
            return True
        except Exception:
            return False

    def restore(self, session_id: str, checkpoint_hash: str) -> None:
        if not is_commit_hash(checkpoint_hash):
            raise ValueError("Invalid checkpoint hash.")
        branch_ref = self._get_session_branch_ref(session_id)
        if branch_ref is None or not Path(self._git_dir).exists():
            raise RuntimeError("File history Git repository was not found for this project.")
        self._run_git(["cat-file", "-e", f"{checkpoint_hash}^{{commit}}"])
        current_hash = self.get_current_checkpoint_hash(session_id)
        current_manifest = self._read_manifest(current_hash) if current_hash else empty_manifest()
        target_manifest = self._read_manifest(checkpoint_hash)
        for key, entry in current_manifest.get("files", {}).items():
            if key not in target_manifest.get("files", {}):
                self._restore_first_known_entry(current_hash, key, entry["path"])
        for entry in target_manifest.get("files", {}).values():
            if entry.get("blob") is None:
                remove_tracked_file(entry["path"])
                continue
            Path(entry["path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(entry["path"]).write_bytes(self._read_blob(entry["blob"]))
        self._run_git(["update-ref", branch_ref, checkpoint_hash])

    def _restore_first_known_entry(self, current_hash: Optional[str], key: str, fallback_path: str) -> None:
        first_entry: Optional[dict[str, Any]] = None
        if current_hash:
            first_entry = self._find_first_known_entry(current_hash, key)
        if first_entry is None:
            first_entry = {"path": fallback_path, "blob": None, "mode": "100644"}
        if first_entry.get("blob") is None:
            remove_tracked_file(first_entry["path"])
            return
        Path(first_entry["path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(first_entry["path"]).write_bytes(self._read_blob(first_entry["blob"]))

    def _find_first_known_entry(self, current_hash: str, key: str) -> Optional[dict[str, Any]]:
        raw_hashes = self._run_git(["rev-list", "--reverse", current_hash]).strip()
        if not raw_hashes:
            return None
        for line in raw_hashes.split("\n"):
            h = line.strip()
            if not is_commit_hash(h):
                continue
            try:
                manifest = self._read_manifest(h)
            except Exception:
                continue
            entry = manifest.get("files", {}).get(key)
            if entry:
                return entry
        return None
