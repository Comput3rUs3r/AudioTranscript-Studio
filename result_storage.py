"""Identity-safe result revisions and WAV-cache storage for Transcript Studio."""

from __future__ import annotations

import ctypes
import datetime as _datetime
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import secrets
import shutil
import tempfile
from typing import Any, Callable, Mapping, Optional

from result_catalog import (
    ManifestValidationError,
    build_source_identity,
    is_valid_source_identity,
    source_identities_match,
    validate_project_manifest,
    validate_result_manifest,
)


PROJECT_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 1
SUPPORTED_ENGINES = frozenset({"whisperx", "crisperwhisper"})
_IDENTITY_FIELDS = ("path", "size", "mtime_ns", "st_dev", "st_ino")


def utc_now_text(now: Optional[_datetime.datetime] = None) -> str:
    value = now or _datetime.datetime.now(_datetime.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_datetime.timezone.utc)
    value = value.astimezone(_datetime.timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_source_identity(identity: Mapping[str, Any]) -> str:
    if not is_valid_source_identity(identity):
        raise ValueError("Source identity is missing or malformed.")
    canonical = {field: identity[field] for field in _IDENTITY_FIELDS}
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def source_identity_hash(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_source_identity(identity).encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def project_id_for_identity(identity: Mapping[str, Any]) -> str:
    return f"sha256:{source_identity_hash(identity)}"


def _safe_title(value: Any) -> str:
    text = str(value or "")
    sanitized = "".join(
        character
        if character.isalnum() or character in (" ", "_", "-")
        else "_"
        for character in text
    ).strip("_ ")
    sanitized = sanitized[:120].rstrip("_ ")
    return sanitized or "untitled"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not read {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} must contain a top-level object.")
    return data


def _atomic_write_json(
    path: Path,
    data: Mapping[str, Any],
    *,
    validator: Optional[Callable[[Path], Any]] = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(data, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if validator is not None:
            validator(temporary_path)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _set_hidden_on_windows(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        hidden_attribute = 0x2
        current = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if current != -1:
            ctypes.windll.kernel32.SetFileAttributesW(
                str(path), current | hidden_attribute
            )
    except Exception:
        pass


def _process_is_running(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                process_id,
            )
            if not handle:
                return kernel32.GetLastError() == 5  # Access denied implies alive.
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Failing closed avoids deleting another run's staging directory.
            return True
    try:
        os.kill(process_id, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _staging_process_id(path: Path) -> Optional[int]:
    prefix = ".staging-p"
    if not path.name.startswith(prefix):
        return None
    process_text = path.name[len(prefix) :].split("-", 1)[0]
    return int(process_text) if process_text.isdigit() else None


def cleanup_process_staging(
    output_root: Path | str,
    process_id: int,
    *,
    require_stopped: bool = True,
) -> tuple[Path, ...]:
    if require_stopped and _process_is_running(process_id):
        return ()
    root = Path(output_root).resolve()
    if not root.is_dir():
        return ()
    removed = []
    for project_root in root.iterdir():
        if not project_root.is_dir():
            continue
        for staging_root in project_root.glob(f".staging-p{process_id}-*"):
            if not staging_root.is_dir() or _staging_process_id(staging_root) != process_id:
                continue
            resolved = staging_root.resolve()
            try:
                resolved.relative_to(project_root.resolve())
            except ValueError:
                continue
            shutil.rmtree(resolved)
            removed.append(resolved)
        try:
            project_root.rmdir()
        except OSError:
            pass
    return tuple(removed)


def cleanup_abandoned_staging(output_root: Path | str) -> tuple[Path, ...]:
    root = Path(output_root).resolve()
    if not root.is_dir():
        return ()
    process_ids = set()
    for project_root in root.iterdir():
        if not project_root.is_dir():
            continue
        for staging_root in project_root.glob(".staging-p*-*"):
            process_id = _staging_process_id(staging_root)
            if process_id is not None and not _process_is_running(process_id):
                process_ids.add(process_id)
    removed = []
    for process_id in sorted(process_ids):
        removed.extend(
            cleanup_process_staging(
                root,
                process_id,
                require_stopped=False,
            )
        )
    return tuple(removed)


def _identity_from_project_sidecars(project_root: Path) -> Optional[Mapping[str, Any]]:
    identities = []
    for engine in sorted(SUPPORTED_ENGINES):
        engine_root = project_root / engine
        if not engine_root.is_dir():
            continue
        for result_path in engine_root.glob("*/result.json"):
            try:
                manifest = validate_result_manifest(
                    result_path,
                    project_root=project_root,
                )
            except Exception:
                continue
            identities.append(manifest.source_identity)
    if not identities:
        return None
    first = identities[0]
    if all(source_identities_match(first, identity) for identity in identities[1:]):
        return first
    return None


def _project_matches_identity(project_root: Path, identity: Mapping[str, Any]) -> bool:
    project_path = project_root / "project.json"
    if project_path.is_file():
        try:
            manifest = validate_project_manifest(project_path)
        except Exception:
            return False
        return (
            manifest.project_id == project_id_for_identity(identity)
            and source_identities_match(manifest.source_identity, identity)
        )
    sidecar_identity = _identity_from_project_sidecars(project_root)
    return (
        sidecar_identity is not None
        and source_identities_match(sidecar_identity, identity)
    )


def choose_project_root(
    output_root: Path | str,
    display_title: str,
    identity: Mapping[str, Any],
) -> Path:
    output_root = Path(output_root).resolve()
    identity_digest = source_identity_hash(identity)
    safe_title = _safe_title(display_title)
    for length in range(12, 65, 4):
        candidate = output_root / f"{safe_title}--{identity_digest[:length]}"
        if not candidate.exists() or _project_matches_identity(candidate, identity):
            return candidate
    counter = 2
    while True:
        candidate = output_root / f"{safe_title}--{identity_digest}-{counter}"
        if not candidate.exists() or _project_matches_identity(candidate, identity):
            return candidate
        counter += 1


@dataclass(frozen=True)
class ProjectLayout:
    output_root: Path
    project_root: Path
    display_title: str
    source_path: Path
    source_identity: Mapping[str, Any]
    source_hash: str
    project_id: str


def project_layout_for_source(
    output_root: Path | str,
    display_title: str,
    source_path: Path | str,
) -> ProjectLayout:
    resolved_source = Path(source_path).expanduser().resolve(strict=True)
    identity = build_source_identity(resolved_source)
    if identity is None:
        raise ValueError("The original source identity could not be established.")
    output = Path(output_root).resolve()
    source_hash = source_identity_hash(identity)
    return ProjectLayout(
        output_root=output,
        project_root=choose_project_root(output, display_title, identity),
        display_title=display_title,
        source_path=resolved_source,
        source_identity=dict(identity),
        source_hash=source_hash,
        project_id=f"sha256:{source_hash}",
    )


def generate_result_id(
    *,
    now: Optional[_datetime.datetime] = None,
    random_suffix: Optional[str] = None,
) -> str:
    value = now or _datetime.datetime.now(_datetime.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_datetime.timezone.utc)
    timestamp = value.astimezone(_datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = random_suffix or secrets.token_hex(4)
    if not suffix or any(character not in "0123456789abcdef" for character in suffix):
        raise ValueError("Result ID random suffix must be lowercase hexadecimal.")
    return f"{timestamp}-{suffix}"


def _result_record_from_manifest(manifest, project_root: Path) -> dict[str, Any]:
    relative_root = manifest.result_root.relative_to(project_root).as_posix()
    return {
        "result_id": manifest.result_id,
        "engine": manifest.engine,
        "model": manifest.model,
        "mode": manifest.mode,
        "execution_backend": manifest.execution_backend,
        "status": manifest.status,
        "created_at": (
            utc_now_text(manifest.created_at) if manifest.created_at is not None else None
        ),
        "updated_at": (
            utc_now_text(manifest.modified_at) if manifest.modified_at is not None else None
        ),
        "paths": {
            "root": relative_root,
            "metadata": f"{relative_root}/result.json",
            "speakers": f"{relative_root}/speakers.json",
            "segments": f"{relative_root}/segments.json",
        },
        "fingerprint": {
            "algorithm": "sha256",
            "speakers": manifest.speakers_sha256,
            "segments": manifest.segments_sha256,
        },
        "comparison_job_id": manifest.comparison_job_id,
    }


def _discover_result_manifests(project_root: Path):
    manifests = []
    for engine in sorted(SUPPORTED_ENGINES):
        engine_root = project_root / engine
        if not engine_root.is_dir():
            continue
        for result_path in sorted(engine_root.glob("*/result.json")):
            try:
                manifests.append(
                    validate_result_manifest(result_path, project_root=project_root)
                )
            except Exception:
                continue
    return manifests


def _new_project_manifest_data(layout: ProjectLayout, now_text: str) -> dict[str, Any]:
    existing_manifests = _discover_result_manifests(layout.project_root)
    records = []
    active = {}
    for manifest in existing_manifests:
        if (
            manifest.project_id != layout.project_id
            or not source_identities_match(manifest.source_identity, layout.source_identity)
        ):
            continue
        records.append(_result_record_from_manifest(manifest, layout.project_root))
        previous = active.get(manifest.engine)
        if previous is None or manifest.result_id > previous:
            active[manifest.engine] = manifest.result_id
    created_values = [
        record.get("created_at") for record in records if record.get("created_at")
    ]
    return {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "project_id": layout.project_id,
        "display_title": layout.display_title,
        "source": {
            "identity": dict(layout.source_identity),
            "last_known_path": str(layout.source_path),
        },
        "created_at": min(created_values) if created_values else now_text,
        "updated_at": now_text,
        "active_results": active,
        "results": records,
    }


def _load_project_data_for_update(layout: ProjectLayout, now_text: str) -> dict[str, Any]:
    project_path = layout.project_root / "project.json"
    if not project_path.exists():
        return _new_project_manifest_data(layout, now_text)
    manifest = validate_project_manifest(project_path)
    if (
        manifest.project_id != layout.project_id
        or not source_identities_match(manifest.source_identity, layout.source_identity)
    ):
        raise ManifestValidationError(
            "Existing project.json belongs to a different source identity."
        )
    data = _read_json_object(project_path, "project.json")
    return data


def _write_project_update(layout: ProjectLayout, result_manifest) -> None:
    now_text = utc_now_text(result_manifest.modified_at)
    project_path = layout.project_root / "project.json"
    project_existed = project_path.exists()
    data = _load_project_data_for_update(layout, now_text)
    records = data.get("results")
    if not isinstance(records, list):
        raise ManifestValidationError("project.json results must be an array.")
    duplicate = any(
        isinstance(record, dict) and record.get("result_id") == result_manifest.result_id
        for record in records
    )
    if duplicate and project_existed:
        raise ManifestValidationError(
            f"project.json already contains result_id {result_manifest.result_id!r}."
        )
    if not duplicate:
        records.append(_result_record_from_manifest(result_manifest, layout.project_root))
    active_results = data.setdefault("active_results", {})
    if not isinstance(active_results, dict):
        raise ManifestValidationError("project.json active_results must be an object.")
    active_results[result_manifest.engine] = result_manifest.result_id
    data["updated_at"] = now_text
    data["display_title"] = layout.display_title
    data["source"] = {
        "identity": dict(layout.source_identity),
        "last_known_path": str(layout.source_path),
    }
    _atomic_write_json(
        project_path,
        data,
        validator=lambda path: validate_project_manifest(path),
    )


class ResultRevision:
    """A same-volume staging area that becomes one committed result revision."""

    def __init__(
        self,
        layout: ProjectLayout,
        engine: str,
        *,
        result_id: Optional[str] = None,
    ):
        normalized_engine = str(engine or "").strip().lower()
        if normalized_engine not in SUPPORTED_ENGINES:
            raise ValueError(f"Unsupported result engine: {engine!r}")
        self.layout = layout
        self.engine = normalized_engine
        self.engine_root = layout.project_root / normalized_engine
        self.result_id = result_id or generate_result_id()
        while result_id is None and (self.engine_root / self.result_id).exists():
            self.result_id = generate_result_id()
        self.final_root = self.engine_root / self.result_id
        self.staging_root = layout.project_root / (
            f".staging-p{os.getpid()}-{self.result_id}-{secrets.token_hex(4)}"
        )
        self.output_root = self.staging_root / normalized_engine / self.result_id
        self.created_at = utc_now_text()
        self.committed = False

    def __enter__(self):
        if self.final_root.exists():
            raise FileExistsError(f"Result revision already exists: {self.result_id}")
        self.output_root.mkdir(parents=True, exist_ok=False)
        _set_hidden_on_windows(self.staging_root)
        return self

    def _cleanup_staging(self) -> None:
        if self.staging_root.exists():
            shutil.rmtree(self.staging_root)

    def _cleanup_empty_parents(self) -> None:
        for path in (self.engine_root, self.layout.project_root):
            try:
                path.rmdir()
            except OSError:
                pass

    def _result_manifest_data(
        self,
        *,
        model: Optional[str],
        mode: Optional[str],
        execution_backend: Optional[str],
    ) -> dict[str, Any]:
        speakers_path = self.output_root / "speakers.json"
        segments_path = self.output_root / "segments.json"
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "project_id": self.layout.project_id,
            "result_id": self.result_id,
            "display_title": self.layout.display_title,
            "engine": self.engine,
            "model": model,
            "mode": mode,
            "execution_backend": execution_backend,
            "status": "complete",
            "source": {
                "identity": dict(self.layout.source_identity),
                "last_known_path": str(self.layout.source_path),
            },
            "created_at": self.created_at,
            "updated_at": self.created_at,
            "paths": {
                "speakers": "speakers.json",
                "segments": "segments.json",
            },
            "fingerprint": {
                "algorithm": "sha256",
                "speakers": _sha256_file(speakers_path),
                "segments": _sha256_file(segments_path),
            },
            "comparison_job_id": None,
        }

    def commit(
        self,
        *,
        model: Optional[str],
        mode: Optional[str],
        execution_backend: Optional[str],
        validate_outputs: Optional[Callable[[Path], None]] = None,
    ) -> Path:
        if self.committed:
            raise RuntimeError("Result revision has already been committed.")
        current_source_identity = build_source_identity(self.layout.source_path)
        if not source_identities_match(
            self.layout.source_identity,
            current_source_identity,
        ):
            raise RuntimeError(
                "The original source changed or became unavailable during processing."
            )
        if validate_outputs is not None:
            validate_outputs(self.output_root)
        manifest_data = self._result_manifest_data(
            model=model,
            mode=mode,
            execution_backend=execution_backend,
        )
        result_path = self.output_root / "result.json"
        _atomic_write_json(result_path, manifest_data)
        validate_result_manifest(result_path, project_root=self.staging_root)
        current_source_identity = build_source_identity(self.layout.source_path)
        if not source_identities_match(
            self.layout.source_identity,
            current_source_identity,
        ):
            raise RuntimeError(
                "The original source changed or became unavailable before commit."
            )

        self.engine_root.mkdir(parents=True, exist_ok=True)
        revision_moved = False
        try:
            os.replace(self.output_root, self.final_root)
            revision_moved = True
            final_manifest = validate_result_manifest(
                self.final_root / "result.json",
                project_root=self.layout.project_root,
            )
            _write_project_update(self.layout, final_manifest)
        except Exception as commit_error:
            rollback_error = None
            if revision_moved and self.final_root.exists():
                try:
                    shutil.rmtree(self.final_root)
                except Exception as exc:
                    rollback_error = exc
            self._cleanup_staging()
            self._cleanup_empty_parents()
            if rollback_error is not None:
                raise RuntimeError(
                    "Result commit failed and the new revision could not be removed "
                    f"during rollback: {rollback_error}"
                ) from commit_error
            raise

        self.committed = True
        self._cleanup_staging()
        return self.final_root

    def __exit__(self, _exc_type, _exc, _traceback):
        self._cleanup_staging()
        if not self.committed:
            self._cleanup_empty_parents()
        return False


def cache_paths(
    cache_root: Path | str,
    display_title: str,
    identity: Mapping[str, Any],
) -> tuple[Path, Path]:
    digest = source_identity_hash(identity)
    base = f"{_safe_title(display_title)}--{digest}"
    root = Path(cache_root).resolve()
    return root / f"{base}.wav", root / f"{base}.source.json"


def verify_cached_wav(
    source_path: Path | str,
    wav_path: Path | str,
    sidecar_path: Path | str,
) -> tuple[bool, str]:
    source = Path(source_path).expanduser()
    wav = Path(wav_path)
    sidecar = Path(sidecar_path)
    current_identity = build_source_identity(source)
    if current_identity is None:
        return False, "source identity is unavailable"
    if not wav.is_file():
        return False, "cached WAV is missing"
    if not sidecar.is_file():
        return False, "cache sidecar is missing"
    try:
        data = _read_json_object(sidecar, "cache sidecar")
    except ValueError as exc:
        return False, str(exc)
    if data.get("schema_version") != CACHE_SCHEMA_VERSION:
        return False, "cache sidecar schema is unsupported"
    saved_identity = data.get("source_identity")
    if not source_identities_match(saved_identity, current_identity):
        return False, "cache source identity differs"
    metadata = data.get("cached_wav")
    if not isinstance(metadata, dict):
        return False, "cached WAV metadata is malformed"
    if metadata.get("filename") != wav.name:
        return False, "cache sidecar names a different WAV"
    try:
        wav_stat = wav.stat()
        expected_size = int(metadata.get("size"))
        expected_mtime = int(metadata.get("mtime_ns"))
    except (OSError, TypeError, ValueError):
        return False, "cached WAV metadata is malformed"
    if wav_stat.st_size <= 0 or wav_stat.st_size != expected_size:
        return False, "cached WAV size differs"
    if wav_stat.st_mtime_ns != expected_mtime:
        return False, "cached WAV timestamp differs"
    expected_hash = metadata.get("sha256")
    if not isinstance(expected_hash, str) or _sha256_file(wav) != expected_hash:
        return False, "cached WAV fingerprint differs"
    return True, "verified"


def create_or_reuse_cached_wav(
    source_path: Path | str,
    cache_root: Path | str,
    display_title: str,
    converter: Callable[[Path, Path], None],
) -> tuple[Path, bool]:
    source = Path(source_path).expanduser().resolve(strict=True)
    identity = build_source_identity(source)
    if identity is None:
        raise ValueError("The original source identity could not be established.")
    wav_path, sidecar_path = cache_paths(cache_root, display_title, identity)
    valid, _reason = verify_cached_wav(source, wav_path, sidecar_path)
    if valid:
        return wav_path, True

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_wav = wav_path.parent / f".{wav_path.name}.{secrets.token_hex(6)}.tmp.wav"
    try:
        converter(source, temporary_wav)
        if not temporary_wav.is_file() or temporary_wav.stat().st_size <= 0:
            raise RuntimeError("WAV conversion did not create a non-empty file.")
        os.replace(temporary_wav, wav_path)
        wav_stat = wav_path.stat()
        sidecar_data = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "source_identity": dict(identity),
            "cached_wav": {
                "filename": wav_path.name,
                "size": int(wav_stat.st_size),
                "mtime_ns": int(wav_stat.st_mtime_ns),
                "sha256": _sha256_file(wav_path),
            },
            "updated_at": utc_now_text(),
        }
        _atomic_write_json(sidecar_path, sidecar_data)
        verified, reason = verify_cached_wav(source, wav_path, sidecar_path)
        if not verified:
            raise RuntimeError(f"New WAV cache could not be verified: {reason}")
    except BaseException:
        temporary_wav.unlink(missing_ok=True)
        try:
            sidecar_path.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return wav_path, False


def build_apply_manifest_updates(
    result_root: Path | str,
    staged_speakers_json: Path | str,
    staged_segments_json: Path | str,
) -> tuple[tuple[Path, dict[str, Any]], ...]:
    """Build fingerprint updates for a transactional Review Apply."""

    result_root = Path(result_root).resolve()
    result_path = result_root / "result.json"
    if not result_path.is_file():
        return ()
    current = validate_result_manifest(result_path)
    result_data = _read_json_object(result_path, "result.json")
    timestamp = utc_now_text()
    fingerprints = {
        "algorithm": "sha256",
        "speakers": _sha256_file(Path(staged_speakers_json)),
        "segments": _sha256_file(Path(staged_segments_json)),
    }
    result_data["fingerprint"] = fingerprints
    result_data["updated_at"] = timestamp
    updates = [(result_path, result_data)]

    project_path = current.project_root / "project.json"
    if project_path.is_file():
        project_manifest = validate_project_manifest(project_path)
        project_data = _read_json_object(project_path, "project.json")
        matched = False
        for record in project_data.get("results", []):
            if isinstance(record, dict) and record.get("result_id") == current.result_id:
                record["fingerprint"] = dict(fingerprints)
                record["updated_at"] = timestamp
                matched = True
                break
        if not matched:
            raise ManifestValidationError(
                "project.json does not contain the selected result revision."
            )
        project_data["updated_at"] = timestamp
        if project_manifest.project_id != current.project_id:
            raise ManifestValidationError("project.json and result.json project IDs differ.")
        updates.append((project_path, project_data))
    return tuple(updates)


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "PROJECT_SCHEMA_VERSION",
    "ProjectLayout",
    "RESULT_SCHEMA_VERSION",
    "ResultRevision",
    "build_apply_manifest_updates",
    "cache_paths",
    "canonical_source_identity",
    "choose_project_root",
    "cleanup_abandoned_staging",
    "cleanup_process_staging",
    "create_or_reuse_cached_wav",
    "generate_result_id",
    "project_id_for_identity",
    "project_layout_for_source",
    "source_identity_hash",
    "utc_now_text",
    "verify_cached_wav",
]
