"""Identity-safe result revisions and WAV-cache storage for Transcript Studio."""

from __future__ import annotations

import ctypes
import datetime as _datetime
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
import secrets
import shutil
import tempfile
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from result_catalog import (
    ManifestValidationError,
    build_source_identity,
    descriptor_from_json_pair,
    is_valid_source_identity,
    preflight_result_pair,
    source_identities_match,
    validate_project_manifest,
    validate_result_manifest,
)
from result_fusion import FusionPlan, FusionPreview
from result_comparison import ResultComparison


PROJECT_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 1
SUPPORTED_ENGINES = frozenset({"whisperx", "crisperwhisper", "combined"})
COMBINED_SCHEMA_VERSION = 1
FUSION_AUDIT_SCHEMA_VERSION = 1
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
    record = {
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
    if manifest.engine == "combined" and manifest.fusion_json is not None:
        record["paths"]["fusion"] = (
            manifest.fusion_json.relative_to(project_root).as_posix()
        )
        record["fingerprint"]["fusion"] = manifest.fusion_sha256
    return record


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
        status: str,
        coverage: Optional[Mapping[str, Any]],
        comparison_job_id: Optional[str],
        manifest_extras: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        speakers_path = self.output_root / "speakers.json"
        segments_path = self.output_root / "segments.json"
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"complete", "incomplete"}:
            raise ValueError("Result revision status must be complete or incomplete.")
        if normalized_status == "incomplete" and self.engine != "crisperwhisper":
            raise ValueError("Only CrisperWhisper revisions may be incomplete.")
        if self.engine == "combined" and normalized_status != "complete":
            raise ValueError("Combined revisions must have Complete status.")
        manifest = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "project_id": self.layout.project_id,
            "result_id": self.result_id,
            "display_title": self.layout.display_title,
            "engine": self.engine,
            "model": model,
            "mode": mode,
            "execution_backend": execution_backend,
            "status": normalized_status,
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
            "comparison_job_id": comparison_job_id,
        }
        if coverage is not None:
            manifest["coverage"] = dict(coverage)
        if manifest_extras:
            extras = dict(manifest_extras)
            protected = set(manifest) - {"paths", "fingerprint"}
            conflicts = protected.intersection(extras)
            if conflicts:
                raise ValueError(
                    "Combined manifest extras cannot replace required fields: "
                    + ", ".join(sorted(conflicts))
                )
            extra_paths = extras.pop("paths", {})
            extra_fingerprints = extras.pop("fingerprint", {})
            if not isinstance(extra_paths, Mapping) or not isinstance(
                extra_fingerprints, Mapping
            ):
                raise ValueError("Manifest path and fingerprint extras must be mappings.")
            if {"speakers", "segments"}.intersection(extra_paths):
                raise ValueError("Manifest extras cannot replace required JSON paths.")
            if {"algorithm", "speakers", "segments"}.intersection(extra_fingerprints):
                raise ValueError("Manifest extras cannot replace required fingerprints.")
            manifest["paths"].update(dict(extra_paths))
            manifest["fingerprint"].update(dict(extra_fingerprints))
            manifest.update(extras)
        return manifest

    def commit(
        self,
        *,
        model: Optional[str],
        mode: Optional[str],
        execution_backend: Optional[str],
        status: str = "complete",
        coverage: Optional[Mapping[str, Any]] = None,
        comparison_job_id: Optional[str] = None,
        validate_outputs: Optional[Callable[[Path], None]] = None,
        manifest_extras: Optional[Mapping[str, Any]] = None,
        pre_commit_check: Optional[Callable[[], None]] = None,
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
            status=status,
            coverage=coverage,
            comparison_job_id=comparison_job_id,
            manifest_extras=manifest_extras,
        )
        result_path = self.output_root / "result.json"
        _atomic_write_json(result_path, manifest_data)
        validate_result_manifest(result_path, project_root=self.staging_root)
        if pre_commit_check is not None:
            pre_commit_check()
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


@dataclass(frozen=True)
class CombinedRevisionCommit:
    result_root: Path
    result_id: str
    speakers_json: Path
    segments_json: Path
    fusion_json: Path


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, _datetime.datetime):
        return utc_now_text(value)
    return value


def _revision_manifest_sha256(revision) -> Optional[str]:
    path = Path(revision.speakers_json).parent / "result.json"
    return _sha256_file(path) if path.is_file() else None


def _source_revision_record(revision, descriptor) -> dict[str, Any]:
    coverage = revision.coverage
    coverage_data = None
    if coverage is not None:
        coverage_data = {
            "coverage_complete": coverage.coverage_complete,
            "remaining_speech_active_gap_count": (
                coverage.remaining_speech_active_gap_count
            ),
            "remaining_speech_active_gap_duration": (
                coverage.remaining_speech_active_gap_duration
            ),
            "remaining_speech_active_gap_ranges": _json_safe(
                coverage.remaining_speech_active_gap_ranges
            ),
            "selected_strategy": coverage.selected_strategy,
        }
    return {
        "revision_id": revision.result_id,
        "project_id": revision.project_id,
        "engine": revision.engine,
        "model": revision.model,
        "mode": revision.mode,
        "execution_backend": revision.execution_backend,
        "status": revision.status,
        "created_at": (
            utc_now_text(revision.created_at)
            if revision.created_at is not None
            else None
        ),
        "updated_at": (
            utc_now_text(revision.modified_at)
            if revision.modified_at is not None
            else None
        ),
        "comparison_job_id": revision.comparison_job_id,
        "fingerprints": {
            "algorithm": "sha256",
            "speakers": revision.speakers_sha256,
            "segments": revision.segments_sha256,
            "result_manifest": _revision_manifest_sha256(revision),
        },
        "source_state_at_save": descriptor.source_state,
        "coverage": coverage_data,
    }


def _revalidate_combined_sources(
    comparison: ResultComparison,
    plan: FusionPlan,
) -> tuple[Any, Any]:
    if not isinstance(comparison, ResultComparison) or not isinstance(plan, FusionPlan):
        raise ValueError("A Combined save requires the exact comparison and fusion plan.")
    if comparison.pair_id != plan.pair_identity.comparison_pair_id:
        raise RuntimeError("The Combined Draft no longer matches its comparison pair.")

    validated = []
    expected_pairs = (
        (comparison.whisperx.revision, plan.pair_identity.whisperx_revision),
        (
            comparison.crisperwhisper.revision,
            plan.pair_identity.crisperwhisper_revision,
        ),
    )
    for revision, expected in expected_pairs:
        try:
            descriptor = descriptor_from_json_pair(
                revision.speakers_json,
                revision.segments_json,
            )
        except Exception as exc:
            raise RuntimeError(
                f"The {revision.engine} source revision is unavailable or invalid: {exc}"
            ) from exc
        current = (
            descriptor.engine,
            descriptor.result_id,
            descriptor.project_id,
            descriptor.speakers_sha256,
            descriptor.segments_sha256,
            descriptor.comparison_job_id,
            descriptor.status,
        )
        retained = (
            expected.engine,
            expected.result_id,
            expected.project_id,
            expected.speakers_sha256,
            expected.segments_sha256,
            expected.comparison_job_id,
            expected.status,
        )
        if current != retained:
            raise RuntimeError(
                f"The {revision.engine} source revision changed after the draft was built."
            )
        if tuple(sorted(dict(descriptor.source_identity or {}).items())) != (
            plan.pair_identity.source_identity
        ):
            raise RuntimeError("A source revision no longer matches the exact media identity.")
        validated.append(descriptor)

    whisper, crisper = validated
    if whisper.engine != "whisperx" or crisper.engine != "crisperwhisper":
        raise RuntimeError("Combined storage requires one WhisperX and one CrisperWhisper revision.")
    if not source_identities_match(whisper.source_identity, crisper.source_identity):
        raise RuntimeError("The source revisions no longer share a strict media identity.")
    if (
        whisper.project_id is not None
        and crisper.project_id is not None
        and whisper.project_id != crisper.project_id
    ):
        raise RuntimeError("The source revisions no longer belong to the same project.")
    source_path = whisper.source_path or crisper.source_path
    current_identity = build_source_identity(source_path)
    if not source_identities_match(whisper.source_identity, current_identity):
        raise RuntimeError(
            "The original media changed or became unavailable after the draft was built."
        )
    return whisper, crisper


_CLOSING_PUNCTUATION = frozenset(",.!?;:%)]}\u2019'\"")


def _join_combined_words(words: Iterable[Any]) -> str:
    text = ""
    for word in words:
        value = str(word.text)
        if not value:
            continue
        if not text:
            text = value.strip()
        elif value[:1].isspace() or value[:1] in _CLOSING_PUNCTUATION:
            text += value
        else:
            text += " " + value
    return text.strip()


def _combined_segments(
    preview: FusionPreview,
    *,
    authoritative_duration: Optional[float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    segments = []
    audit_words = []
    previous_end = None
    combined_ordinal = 0
    for candidate in preview.candidates:
        if candidate.text and not candidate.words:
            raise RuntimeError(
                f"Fusion region {candidate.region_id} contains text without word evidence."
            )
        if _join_combined_words(candidate.words) != candidate.text:
            raise RuntimeError(
                f"Fusion region {candidate.region_id} text differs from its selected words."
            )
        serialized_words = []
        for word in candidate.words:
            if (
                isinstance(word.start, bool)
                or not isinstance(word.start, (int, float))
                or isinstance(word.end, bool)
                or not isinstance(word.end, (int, float))
                or not math.isfinite(float(word.start))
                or not math.isfinite(float(word.end))
            ):
                raise RuntimeError("Combined words must have finite start and end timestamps.")
            start = float(word.start)
            end = float(word.end)
            if start < 0.0 or end <= start:
                raise RuntimeError("Combined words must have positive timestamps within the media timeline.")
            if previous_end is not None and start < previous_end - 1e-9:
                raise RuntimeError("Combined words must be chronological and nonoverlapping.")
            if authoritative_duration is not None and end > authoritative_duration + 0.001:
                raise RuntimeError("Combined words exceed the authoritative media duration.")
            previous_end = end
            speaker_id = str(word.speaker_id or "").strip() or None
            serialized_word = {
                "word": word.text,
                "start": start,
                "end": end,
            }
            if speaker_id:
                serialized_word["speaker"] = speaker_id
            serialized_words.append(serialized_word)
            audit_words.append(
                {
                    "combined_word_ordinal": combined_ordinal,
                    "text": word.text,
                    "start": start,
                    "end": end,
                    "speaker_id": speaker_id,
                    "speaker_name": word.speaker_name,
                    "text_source": word.text_source_engine,
                    "timing_source": word.timing_source_engine,
                    "speaker_source": word.speaker_source_engine,
                    "speaker_decision_source": word.speaker_decision_source,
                    "timing": {
                        "kind": "derived" if word.timing_is_derived else "native",
                        "transformation": word.timing_transformation,
                    },
                    "source_word_references": [
                        {
                            "engine": item.source_engine,
                            "revision_id": item.revision_id,
                            "speakers_sha256": item.speakers_sha256,
                            "segments_sha256": item.segments_sha256,
                            "segment_index": item.segment_index,
                            "word_index": item.word_index,
                            "ordinal": item.ordinal,
                            "text": item.original_text,
                            "start": item.original_start,
                            "end": item.original_end,
                            "speaker_id": item.original_speaker_id,
                            "speaker_name": item.original_speaker_name,
                            "classification": item.comparison_classification,
                            "decision_source": item.decision_source,
                            "source_roles": list(item.source_roles),
                        }
                        for item in word.provenance
                    ],
                }
            )
            combined_ordinal += 1
        if not serialized_words:
            continue
        segment_start = serialized_words[0]["start"]
        segment_end = serialized_words[-1]["end"]
        segment_speaker = serialized_words[0].get("speaker")
        segment = {
            "start": segment_start,
            "end": segment_end,
            "text": candidate.text,
            "words": serialized_words,
            "fusion_region_id": candidate.region_id,
            "fusion_candidate_id": candidate.candidate_id,
        }
        if segment_speaker:
            segment["speaker"] = segment_speaker
        segments.append(segment)
    if not segments or not audit_words:
        raise RuntimeError("A Combined result cannot be saved without timed transcript words.")
    if "\n".join(segment["text"] for segment in segments) != preview.text:
        raise RuntimeError("Serialized Combined segments differ from the validated clean preview.")
    return segments, audit_words


def _decision_data(decision: Any) -> Optional[dict[str, Any]]:
    if decision is None:
        return None
    return {
        "region_id": decision.region_id,
        "action": decision.action,
        "selected_candidate_ids": list(decision.selected_candidate_ids),
        "decision_source": decision.decision_source,
        "sequence": decision.sequence,
        "text_source": decision.text_source_engine,
        "timing_source": decision.timing_source_engine,
        "speaker_source": decision.speaker_source_engine,
        "text_decision_source": decision.text_decision_source,
        "timing_decision_source": decision.timing_decision_source,
    }


def _fusion_summary(plan: FusionPlan, preview: FusionPreview) -> dict[str, Any]:
    decisions = [
        region.decision
        for region in plan.regions
        if region.decision is not None
    ]
    omitted = sum(
        decision.action in {"omit_engine_only_passage", "omit_both"}
        for decision in decisions
    )
    included = sum(
        decision.action == "include_engine_only_passage"
        for decision in decisions
    )
    derived = sum(word.timing_is_derived for word in preview.words)
    return {
        "automatic_decision_count": plan.summary.automatically_resolved_region_count,
        "explicit_decision_count": plan.summary.explicitly_resolved_region_count,
        "omitted_region_count": omitted,
        "included_engine_only_count": included,
        "derived_timing_word_count": derived,
        "warning_count": len(plan.warnings),
        "unresolved_count": plan.summary.unresolved_region_count,
    }


def _fusion_audit_data(
    *,
    result_id: str,
    created_at: str,
    comparison: ResultComparison,
    plan: FusionPlan,
    preview: FusionPreview,
    source_revisions: Mapping[str, Any],
    mapping_audit: Mapping[str, Any],
    audit_words: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    regions = []
    for region in plan.regions:
        regions.append(
            {
                "region_id": region.region_id,
                "classification": region.classification,
                "automatically_resolved": region.automatically_resolved,
                "warning": region.warning,
                "final_decision": _decision_data(region.decision),
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "role": candidate.role,
                        "source_engine": candidate.source_engine,
                        "supporting_engines": list(candidate.supporting_engines),
                        "text": candidate.text,
                        "start": candidate.start,
                        "end": candidate.end,
                        "text_source": candidate.text_source_engine,
                        "timing_source": candidate.timing_source_engine,
                        "speaker_source": candidate.speaker_source_engine,
                    }
                    for candidate in region.candidates
                ],
            }
        )
    summary = _fusion_summary(plan, preview)
    return {
        "schema_version": FUSION_AUDIT_SCHEMA_VERSION,
        "result_id": result_id,
        "engine": "combined",
        "comparison_pair_id": plan.pair_identity.comparison_pair_id,
        "comparison_job_id": plan.pair_identity.comparison_job_id,
        "created_at": created_at,
        "source_revisions": _json_safe(source_revisions),
        "regions": regions,
        "decision_history": [
            _decision_data(decision) for decision in plan.decision_history
        ],
        "speaker_mappings": _json_safe(mapping_audit),
        "per_word_provenance": _json_safe(audit_words),
        "omitted_region_ids": [
            region.region_id
            for region in plan.regions
            if region.decision is not None
            and region.decision.action in {"omit_engine_only_passage", "omit_both"}
        ],
        "included_engine_only_region_ids": [
            region.region_id
            for region in plan.regions
            if region.decision is not None
            and region.decision.action == "include_engine_only_passage"
        ],
        "source_coverage_warnings": list(plan.warnings),
        "authoritative_duration": {
            "seconds": plan.authoritative_media_duration,
            "authoritative": plan.authoritative_media_duration is not None,
            "source": plan.authoritative_duration_source,
            "known_transcript_bound": plan.known_transcript_bound,
        },
        "validation": {
            "final_ready": preview.final_ready,
            "provisional": preview.provisional,
            "chronology_valid": True,
            "word_count": len(preview.words),
            **summary,
        },
    }


def _write_json_file(path: Path, data: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate_combined_staging(
    output_root: Path,
    *,
    title: str,
    expected_text: str,
    output_files: Mapping[str, Path],
) -> None:
    preflight = preflight_result_pair(
        output_root / "speakers.json",
        output_root / "segments.json",
    )
    serialized_text = "\n".join(
        str(segment.get("text") or "")
        for segment in preflight.segments_data["segments"]
    )
    if serialized_text != expected_text:
        raise RuntimeError("Staged Combined transcript differs from the validated draft.")
    fusion_path = output_root / "fusion.json"
    fusion_data = _read_json_object(fusion_path, "fusion.json")
    if (
        fusion_data.get("schema_version") != FUSION_AUDIT_SCHEMA_VERSION
        or fusion_data.get("engine") != "combined"
        or not fusion_data.get("validation", {}).get("final_ready")
    ):
        raise RuntimeError("Staged fusion.json did not pass validation.")
    for required in ("srt", "txt"):
        if required not in output_files:
            raise RuntimeError(f"Combined storage did not create the required {required.upper()} output.")
    for name, path in output_files.items():
        resolved = Path(path).resolve()
        if resolved.parent != output_root.resolve() or not resolved.is_file():
            raise RuntimeError(f"Staged Combined output {name!r} is missing or unsafe.")
        raw = resolved.read_bytes()
        if not raw:
            raise RuntimeError(f"Staged Combined output {name!r} is empty.")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Staged Combined output {name!r} is not UTF-8.") from exc


def save_combined_revision(
    *,
    output_root: Path | str,
    comparison: ResultComparison,
    plan: FusionPlan,
    preview: FusionPreview,
    canonical_speakers: Sequence[Mapping[str, Any]],
    mapping_audit: Mapping[str, Any],
    write_exports: Callable[
        [Path, str, Sequence[Mapping[str, Any]], Mapping[str, str]],
        Mapping[str, Path | str],
    ],
    result_id: Optional[str] = None,
) -> CombinedRevisionCommit:
    """Atomically persist one explicitly validated, exact-pair Combined result."""

    if not isinstance(preview, FusionPreview) or preview.provisional or not preview.final_ready:
        raise RuntimeError("Only a final-ready Combined Draft can be saved.")
    if not plan.ready_for_final_generation or plan.summary.unresolved_region_count:
        raise RuntimeError("The Combined Draft still has unresolved regions.")
    if preview.pair_identity != plan.pair_identity:
        raise RuntimeError("The validated preview no longer matches its fusion plan.")

    whisper_descriptor, crisper_descriptor = _revalidate_combined_sources(
        comparison,
        plan,
    )
    source_path = whisper_descriptor.source_path or crisper_descriptor.source_path
    title = comparison.whisperx.revision.display_title
    layout = project_layout_for_source(output_root, title, source_path)
    if plan.pair_identity.project_id and layout.project_id != plan.pair_identity.project_id:
        raise RuntimeError("The current project identity differs from the fusion pair.")

    source_revisions = {
        "whisperx": _source_revision_record(
            comparison.whisperx.revision,
            whisper_descriptor,
        ),
        "crisperwhisper": _source_revision_record(
            comparison.crisperwhisper.revision,
            crisper_descriptor,
        ),
    }
    segments, audit_words = _combined_segments(
        preview,
        authoritative_duration=plan.authoritative_media_duration,
    )
    actual_speaker_ids = []
    actual_names = {}
    for word in preview.words:
        speaker_id = str(word.speaker_id or "").strip()
        if speaker_id and speaker_id not in actual_speaker_ids:
            actual_speaker_ids.append(speaker_id)
        if speaker_id and word.speaker_name:
            actual_names.setdefault(speaker_id, str(word.speaker_name))
    for speaker in canonical_speakers:
        speaker_id = str(speaker.get("speaker_id") or "").strip()
        if not speaker_id:
            raise RuntimeError("Combined speaker records must have a speaker ID.")
        if speaker_id not in actual_speaker_ids:
            actual_speaker_ids.append(speaker_id)
        name = str(speaker.get("name") or "").strip()
        if name:
            actual_names[speaker_id] = name

    model_label = " + ".join(
        (
            comparison.whisperx.revision.model or "WhisperX model unknown",
            comparison.crisperwhisper.revision.model or "CrisperWhisper model unknown",
        )
    )
    with ResultRevision(
        layout,
        "combined",
        result_id=result_id,
    ) as revision:
        segments_data = {
            "title": title,
            "source_path": str(layout.source_path),
            "source_identity": dict(layout.source_identity),
            "segments": segments,
            "transcription": {
                "engine": "combined",
                "model": model_label,
                "mode": "User-reviewed fusion",
                "source_revision_ids": {
                    "whisperx": comparison.whisperx.revision.result_id,
                    "crisperwhisper": comparison.crisperwhisper.revision.result_id,
                },
                "comparison_pair_id": plan.pair_identity.comparison_pair_id,
                "fusion_manifest": "fusion.json",
            },
            "media_duration": plan.authoritative_media_duration,
            "media_duration_authoritative": plan.authoritative_media_duration is not None,
            "media_duration_source": plan.authoritative_duration_source,
        }
        speakers_data = {
            "title": title,
            "diarization": bool(actual_speaker_ids),
            "speakers": actual_speaker_ids,
            "names": actual_names,
            "engine": "combined",
            "fusion_manifest": "fusion.json",
        }
        fusion_data = _fusion_audit_data(
            result_id=revision.result_id,
            created_at=revision.created_at,
            comparison=comparison,
            plan=plan,
            preview=preview,
            source_revisions=source_revisions,
            mapping_audit=mapping_audit,
            audit_words=audit_words,
        )
        _write_json_file(revision.output_root / "segments.json", segments_data)
        _write_json_file(revision.output_root / "speakers.json", speakers_data)
        _write_json_file(revision.output_root / "fusion.json", fusion_data)

        output_values = write_exports(
            revision.output_root,
            title,
            segments,
            actual_names,
        )
        if not isinstance(output_values, Mapping):
            raise RuntimeError("Combined export writer did not return an output mapping.")
        output_files = {
            str(name): (
                Path(value)
                if Path(value).is_absolute()
                else revision.output_root / Path(value)
            )
            for name, value in output_values.items()
        }
        _validate_combined_staging(
            revision.output_root,
            title=title,
            expected_text=preview.text,
            output_files=output_files,
        )
        output_manifest = {
            name: {
                "path": path.name,
                "sha256": _sha256_file(path),
            }
            for name, path in sorted(output_files.items())
        }
        summary = _fusion_summary(plan, preview)
        manifest_extras = {
            "paths": {"fusion": "fusion.json"},
            "fingerprint": {
                "fusion": _sha256_file(revision.output_root / "fusion.json")
            },
            "outputs": output_manifest,
            "combined": {
                "schema_version": COMBINED_SCHEMA_VERSION,
                "comparison_pair_id": plan.pair_identity.comparison_pair_id,
                "comparison_job_id": plan.pair_identity.comparison_job_id,
                "source_revisions": source_revisions,
                "source_state_at_save": {
                    "whisperx": whisper_descriptor.source_state,
                    "crisperwhisper": crisper_descriptor.source_state,
                },
                "authoritative_duration": {
                    "seconds": plan.authoritative_media_duration,
                    "authoritative": plan.authoritative_media_duration is not None,
                    "source": plan.authoritative_duration_source,
                },
                "fusion_summary": summary,
                "source_coverage_warnings": list(plan.warnings),
            },
        }

        def exact_pair_check() -> None:
            _revalidate_combined_sources(comparison, plan)

        final_root = revision.commit(
            model=model_label,
            mode="User-reviewed fusion",
            execution_backend=None,
            status="complete",
            comparison_job_id=plan.pair_identity.comparison_job_id,
            manifest_extras=manifest_extras,
            validate_outputs=lambda root: _validate_combined_staging(
                root,
                title=title,
                expected_text=preview.text,
                output_files={
                    name: root / path.name for name, path in output_files.items()
                },
            ),
            pre_commit_check=exact_pair_check,
        )

    return CombinedRevisionCommit(
        result_root=final_root,
        result_id=revision.result_id,
        speakers_json=final_root / "speakers.json",
        segments_json=final_root / "segments.json",
        fusion_json=final_root / "fusion.json",
    )


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
    staged_output_files: Optional[Mapping[Path | str, Path | str]] = None,
) -> tuple[tuple[Path, dict[str, Any]], ...]:
    """Build fingerprint updates for a transactional Review Apply."""

    result_root = Path(result_root).resolve()
    result_path = result_root / "result.json"
    if not result_path.is_file():
        return ()
    current = validate_result_manifest(result_path)
    result_data = _read_json_object(result_path, "result.json")
    timestamp = utc_now_text()
    existing_fingerprints = result_data.get("fingerprint")
    fingerprints = (
        dict(existing_fingerprints)
        if isinstance(existing_fingerprints, dict)
        else {}
    )
    fingerprints.update(
        {
            "algorithm": "sha256",
            "speakers": _sha256_file(Path(staged_speakers_json)),
            "segments": _sha256_file(Path(staged_segments_json)),
        }
    )
    result_data["fingerprint"] = fingerprints
    if current.engine == "combined" and staged_output_files:
        outputs = result_data.get("outputs")
        if not isinstance(outputs, dict):
            raise ManifestValidationError(
                "Combined result.json outputs metadata is malformed."
            )
        staged_by_target = {
            Path(target).resolve(): Path(staged).resolve()
            for target, staged in staged_output_files.items()
        }
        for output_record in outputs.values():
            if not isinstance(output_record, dict):
                raise ManifestValidationError(
                    "Combined result.json output metadata is malformed."
                )
            relative = output_record.get("path")
            if not isinstance(relative, str):
                raise ManifestValidationError(
                    "Combined result.json output path is malformed."
                )
            target = (result_root / relative).resolve()
            staged = staged_by_target.get(target)
            if staged is not None:
                output_record["sha256"] = _sha256_file(staged)
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
    "COMBINED_SCHEMA_VERSION",
    "CombinedRevisionCommit",
    "FUSION_AUDIT_SCHEMA_VERSION",
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
    "save_combined_revision",
    "source_identity_hash",
    "utc_now_text",
    "verify_cached_wav",
]
