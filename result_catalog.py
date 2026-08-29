"""Validated discovery contract for Transcript Studio review results.

This module intentionally depends only on the Python standard library.  It
understands both the current direct (legacy) output layout and the proposed
project/engine/revision layout, but it never writes or migrates either one.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional


PROJECT_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
SOURCE_IDENTITY_FIELDS = ("path", "size", "mtime_ns", "st_dev", "st_ino")
SUPPORTED_ENGINES = frozenset({"whisperx", "crisperwhisper", "combined"})
SUPPORTED_LAYOUTS = frozenset({"project", "legacy"})
SUPPORTED_SOURCE_STATES = frozenset({"available", "missing", "changed", "unverified"})
SUPPORTED_RESULT_STATUSES = frozenset({"complete", "failed", "incomplete", "processing"})


class ResultCatalogError(ValueError):
    """Base error for invalid result data or catalog metadata."""


class ResultPreflightError(ResultCatalogError):
    """Raised when a speakers/segments JSON pair is unavailable or incompatible."""


class ManifestValidationError(ResultCatalogError):
    """Raised when project.json or result.json violates the catalog contract."""


@dataclass(frozen=True)
class ResultFileIdentity:
    speakers_json: Path
    segments_json: Path
    speakers_sha256: str
    segments_sha256: str

    @property
    def paths(self) -> tuple[Path, Path]:
        return self.speakers_json, self.segments_json


@dataclass(frozen=True)
class ResultPreflight:
    identity: ResultFileIdentity
    speakers_data: dict[str, Any]
    segments_data: dict[str, Any]


@dataclass(frozen=True)
class ResultCoverage:
    coverage_complete: bool
    remaining_speech_active_gap_count: int
    remaining_speech_active_gap_duration: float
    remaining_speech_active_gap_ranges: tuple[Mapping[str, Any], ...]
    selected_strategy: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "remaining_speech_active_gap_ranges",
            tuple(
                MappingProxyType(dict(item))
                for item in self.remaining_speech_active_gap_ranges
            ),
        )


@dataclass(frozen=True)
class ResultDescriptor:
    project_id: Optional[str]
    result_id: str
    layout: str
    display_title: str
    engine: str
    model: Optional[str]
    mode: Optional[str]
    execution_backend: Optional[str]
    status: str
    source_identity: Optional[Mapping[str, Any]]
    source_path: Optional[Path]
    source_state: str
    speakers_json: Path
    segments_json: Path
    speakers_sha256: str
    segments_sha256: str
    created_at: Optional[datetime]
    modified_at: Optional[datetime]
    comparison_job_id: Optional[str]
    coverage: Optional[ResultCoverage] = None
    pending: bool = False
    provenance_source_states: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.layout not in SUPPORTED_LAYOUTS:
            raise ValueError(f"Unsupported result layout: {self.layout!r}")
        if self.engine not in SUPPORTED_ENGINES:
            raise ValueError(f"Unsupported result engine: {self.engine!r}")
        if self.source_state not in SUPPORTED_SOURCE_STATES:
            raise ValueError(f"Unsupported source state: {self.source_state!r}")
        if self.status not in SUPPORTED_RESULT_STATUSES:
            raise ValueError(f"Unsupported result status: {self.status!r}")
        if not self.result_id:
            raise ValueError("result_id must not be empty")
        if not self.display_title:
            raise ValueError("display_title must not be empty")
        if not _is_sha256(self.speakers_sha256) or not _is_sha256(self.segments_sha256):
            raise ValueError("Result fingerprints must be lowercase SHA-256 hex strings")
        provenance_states = dict(self.provenance_source_states)
        if len(provenance_states) != len(self.provenance_source_states) or any(
            engine not in {"whisperx", "crisperwhisper"}
            or state not in {"available", "missing", "changed"}
            for engine, state in self.provenance_source_states
        ):
            raise ValueError("Combined provenance source states are malformed")
        if self.source_identity is not None:
            if not is_valid_source_identity(self.source_identity):
                raise ValueError("source_identity is malformed")
            object.__setattr__(
                self,
                "source_identity",
                MappingProxyType(dict(self.source_identity)),
            )
        object.__setattr__(self, "speakers_json", Path(self.speakers_json).resolve())
        object.__setattr__(self, "segments_json", Path(self.segments_json).resolve())
        if self.source_path is not None:
            object.__setattr__(self, "source_path", Path(self.source_path).resolve())

    @property
    def paths(self) -> tuple[Path, Path]:
        return self.speakers_json, self.segments_json

    @property
    def file_identity(self) -> ResultFileIdentity:
        return ResultFileIdentity(
            self.speakers_json,
            self.segments_json,
            self.speakers_sha256,
            self.segments_sha256,
        )


@dataclass(frozen=True)
class CatalogIssue:
    path: Path
    message: str


@dataclass(frozen=True)
class CatalogDiscovery:
    results: tuple[ResultDescriptor, ...]
    issues: tuple[CatalogIssue, ...]


@dataclass(frozen=True)
class ProjectResultRecord:
    result_id: str
    engine: str
    model: Optional[str]
    mode: Optional[str]
    execution_backend: Optional[str]
    status: str
    created_at: Optional[datetime]
    modified_at: Optional[datetime]
    root: Path
    metadata: Path
    speakers_json: Path
    segments_json: Path
    speakers_sha256: str
    segments_sha256: str
    comparison_job_id: Optional[str]
    fusion_json: Optional[Path] = None
    fusion_sha256: Optional[str] = None


@dataclass(frozen=True)
class ProjectManifest:
    path: Path
    project_root: Path
    project_id: str
    display_title: str
    source_identity: Mapping[str, Any]
    source_path: Path
    created_at: Optional[datetime]
    modified_at: Optional[datetime]
    results: tuple[ProjectResultRecord, ...]


@dataclass(frozen=True)
class ResultManifest:
    path: Path
    project_root: Path
    result_root: Path
    project_id: str
    result_id: str
    display_title: str
    engine: str
    model: Optional[str]
    mode: Optional[str]
    execution_backend: Optional[str]
    status: str
    coverage: Optional[ResultCoverage]
    source_identity: Mapping[str, Any]
    source_path: Path
    created_at: Optional[datetime]
    modified_at: Optional[datetime]
    speakers_json: Path
    segments_json: Path
    speakers_sha256: str
    segments_sha256: str
    comparison_job_id: Optional[str]
    fusion_json: Optional[Path] = None
    fusion_sha256: Optional[str] = None
    output_files: tuple[tuple[str, Path, str], ...] = ()
    provenance_source_states: tuple[tuple[str, str], ...] = ()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def build_source_identity(path: Path | str | None) -> Optional[dict[str, Any]]:
    if not path:
        return None
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        source_stat = resolved.stat()
        if not resolved.is_file():
            return None
        return {
            "path": os.path.normcase(os.path.normpath(str(resolved))),
            "size": int(source_stat.st_size),
            "mtime_ns": int(source_stat.st_mtime_ns),
            "st_dev": int(source_stat.st_dev),
            "st_ino": int(source_stat.st_ino),
        }
    except (OSError, TypeError, ValueError):
        return None


def is_valid_source_identity(identity: Any) -> bool:
    if not isinstance(identity, Mapping):
        return False
    if set(SOURCE_IDENTITY_FIELDS) - set(identity):
        return False
    identity_path = identity.get("path")
    if not isinstance(identity_path, str) or not identity_path:
        return False
    for field in SOURCE_IDENTITY_FIELDS[1:]:
        value = identity.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            return False
    return True


def source_identities_match(saved: Any, current: Any) -> bool:
    if not is_valid_source_identity(saved) or not is_valid_source_identity(current):
        return False
    if os.path.normcase(os.path.normpath(saved["path"])) != os.path.normcase(
        os.path.normpath(current["path"])
    ):
        return False
    return all(saved[field] == current[field] for field in SOURCE_IDENTITY_FIELDS[1:])


def determine_source_state(
    source_path: Path | str | None,
    source_identity: Optional[Mapping[str, Any]],
) -> str:
    """Return source availability without making transcript discovery depend on it."""

    if source_path is None or not str(source_path).strip():
        return "unverified"
    try:
        path = Path(source_path).expanduser()
        if not path.is_file():
            return "missing"
    except (OSError, TypeError, ValueError):
        return "missing"
    if not is_valid_source_identity(source_identity):
        return "unverified"
    current_identity = build_source_identity(path)
    if current_identity is None:
        return "missing"
    return "available" if source_identities_match(source_identity, current_identity) else "changed"


def _read_json_object(path: Path, label: str, error_type=ResultPreflightError):
    try:
        raw = path.read_bytes()
    except Exception as exc:
        raise error_type(f"Could not read {label}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise error_type(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise error_type(f"{label} must contain a top-level JSON object.")
    return data, hashlib.sha256(raw).hexdigest()


def preflight_result_pair(speakers_json: Path | str, segments_json: Path | str) -> ResultPreflight:
    speakers_path = Path(speakers_json).resolve()
    segments_path = Path(segments_json).resolve()
    if not speakers_path.is_file():
        raise FileNotFoundError(f"Missing speakers.json: {speakers_path}")
    if not segments_path.is_file():
        raise FileNotFoundError(f"Missing segments.json: {segments_path}")
    if speakers_path.parent != segments_path.parent:
        raise ResultPreflightError("speakers.json and segments.json must be in the same result folder.")

    speakers_data, speakers_sha256 = _read_json_object(speakers_path, "speakers.json")
    segments_data, segments_sha256 = _read_json_object(segments_path, "segments.json")

    speakers = speakers_data.get("speakers")
    if not isinstance(speakers, list):
        raise ResultPreflightError("speakers.json must contain a 'speakers' list.")
    if any(not isinstance(speaker, str) or not speaker.strip() for speaker in speakers):
        raise ResultPreflightError(
            "Every speakers.json speaker entry must be a non-empty string."
        )
    if len(set(speakers)) != len(speakers):
        raise ResultPreflightError("speakers.json contains duplicate speaker IDs.")
    for mapping_key in ("names", "name_map"):
        mapping = speakers_data.get(mapping_key)
        if mapping is not None and not isinstance(mapping, dict):
            raise ResultPreflightError(
                f"speakers.json '{mapping_key}' must be an object when present."
            )

    transcription = segments_data.get("transcription")
    if transcription is not None and not isinstance(transcription, dict):
        raise ResultPreflightError(
            "segments.json 'transcription' must be an object when present."
        )
    source_path = segments_data.get("source_path")
    if source_path is not None and not isinstance(source_path, str):
        raise ResultPreflightError(
            "segments.json 'source_path' must be a string when present."
        )

    segments = segments_data.get("segments")
    if not isinstance(segments, list):
        raise ResultPreflightError("segments.json must contain a 'segments' list.")
    for segment_index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ResultPreflightError(
                f"segments.json segment {segment_index + 1} must be a JSON object."
            )
        speaker = segment.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise ResultPreflightError(
                f"segments.json segment {segment_index + 1} has an invalid speaker value."
            )
        words = segment.get("words")
        if words is not None:
            if not isinstance(words, list):
                raise ResultPreflightError(
                    f"segments.json segment {segment_index + 1} has a non-list words value."
                )
            if any(not isinstance(word, dict) for word in words):
                raise ResultPreflightError(
                    f"segments.json segment {segment_index + 1} contains an invalid word entry."
                )

    identity = ResultFileIdentity(
        speakers_path,
        segments_path,
        speakers_sha256,
        segments_sha256,
    )
    return ResultPreflight(identity, speakers_data, segments_data)


def _optional_string(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{label} must be a non-empty string or null.")
    return value.strip()


def _required_string(value: Any, label: str) -> str:
    result = _optional_string(value, label)
    if result is None:
        raise ManifestValidationError(f"{label} is required.")
    return result


def _parse_timestamp(value: Any, label: str) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{label} must be an ISO-8601 string or null.")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ManifestValidationError(f"{label} is not a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ManifestValidationError(f"{label} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _validate_engine(value: Any, label: str) -> str:
    engine = _required_string(value, label).lower()
    if engine not in SUPPORTED_ENGINES:
        raise ManifestValidationError(
            f"{label} must be 'whisperx', 'crisperwhisper', or 'combined'."
        )
    return engine


def _validate_status(value: Any, label: str) -> str:
    status = _required_string(value, label).lower()
    if status not in SUPPORTED_RESULT_STATUSES:
        raise ManifestValidationError(
            f"{label} must be one of: {', '.join(sorted(SUPPORTED_RESULT_STATUSES))}."
        )
    return status


def _safe_relative_path(base: Path, value: Any, boundary: Path, label: str) -> Path:
    text = _required_string(value, label)
    relative = Path(text)
    if relative.is_absolute() or relative.drive or relative.anchor:
        raise ManifestValidationError(f"{label} must be a relative path.")
    if ".." in relative.parts:
        raise ManifestValidationError(f"{label} must not contain '..' traversal.")
    if relative == Path("."):
        raise ManifestValidationError(f"{label} must identify a file or directory.")
    base = base.resolve()
    boundary = boundary.resolve()
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(boundary)
    except ValueError as exc:
        raise ManifestValidationError(
            f"{label} resolves outside the project directory."
        ) from exc
    return resolved


def _manifest_source(data: Any, label: str) -> tuple[Mapping[str, Any], Path]:
    if not isinstance(data, dict):
        raise ManifestValidationError(f"{label} must be an object.")
    identity = data.get("identity")
    if not is_valid_source_identity(identity):
        raise ManifestValidationError(f"{label}.identity is malformed.")
    last_known_path = _required_string(data.get("last_known_path"), f"{label}.last_known_path")
    source_path = Path(last_known_path).expanduser()
    if not source_path.is_absolute():
        raise ManifestValidationError(f"{label}.last_known_path must be an absolute path.")
    return MappingProxyType(dict(identity)), source_path.resolve()


def _manifest_fingerprints(data: Any, label: str) -> tuple[str, str]:
    if not isinstance(data, dict):
        raise ManifestValidationError(f"{label} must be an object.")
    algorithm = data.get("algorithm")
    if algorithm != "sha256":
        raise ManifestValidationError(f"{label}.algorithm must be 'sha256'.")
    speakers_hash = data.get("speakers")
    segments_hash = data.get("segments")
    if not _is_sha256(speakers_hash) or not _is_sha256(segments_hash):
        raise ManifestValidationError(f"{label} contains an invalid SHA-256 fingerprint.")
    return speakers_hash, segments_hash


def _validate_combined_source_revisions(value: Any, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"whisperx", "crisperwhisper"}:
        raise ManifestValidationError(
            f"{label} must identify the exact WhisperX and CrisperWhisper revisions."
        )
    for expected_engine, record in value.items():
        record_label = f"{label}.{expected_engine}"
        if not isinstance(record, dict):
            raise ManifestValidationError(f"{record_label} must be an object.")
        if record.get("engine") != expected_engine:
            raise ManifestValidationError(f"{record_label}.engine is invalid.")
        _required_string(record.get("revision_id"), f"{record_label}.revision_id")
        _validate_status(record.get("status"), f"{record_label}.status")
        fingerprints = record.get("fingerprints")
        if not isinstance(fingerprints, dict) or fingerprints.get("algorithm") != "sha256":
            raise ManifestValidationError(f"{record_label}.fingerprints is invalid.")
        if not _is_sha256(fingerprints.get("speakers")) or not _is_sha256(
            fingerprints.get("segments")
        ):
            raise ManifestValidationError(
                f"{record_label}.fingerprints contains an invalid source fingerprint."
            )
        manifest_fingerprint = fingerprints.get("result_manifest")
        if manifest_fingerprint is not None and not _is_sha256(manifest_fingerprint):
            raise ManifestValidationError(
                f"{record_label}.fingerprints.result_manifest is invalid."
            )


def _validate_combined_payloads(
    *,
    result_id: str,
    pair_id: str,
    source_revisions: Mapping[str, Any],
    fusion_json: Path,
    speakers_json: Path,
    segments_json: Path,
) -> None:
    fusion_data, _fusion_digest = _read_json_object(
        fusion_json,
        "fusion.json",
        ManifestValidationError,
    )
    if (
        fusion_data.get("schema_version") != 1
        or fusion_data.get("result_id") != result_id
        or fusion_data.get("engine") != "combined"
        or fusion_data.get("comparison_pair_id") != pair_id
        or fusion_data.get("source_revisions") != source_revisions
    ):
        raise ManifestValidationError(
            "fusion.json does not identify the exact Combined result and source pair."
        )
    validation = fusion_data.get("validation")
    if (
        not isinstance(validation, dict)
        or validation.get("final_ready") is not True
        or validation.get("provisional") is not False
        or validation.get("chronology_valid") is not True
        or validation.get("unresolved_count") != 0
    ):
        raise ManifestValidationError("fusion.json is not a final-ready fusion audit.")
    regions = fusion_data.get("regions")
    history = fusion_data.get("decision_history")
    mappings = fusion_data.get("speaker_mappings")
    provenance = fusion_data.get("per_word_provenance")
    if (
        not isinstance(regions, list)
        or not isinstance(history, list)
        or not isinstance(mappings, dict)
        or not isinstance(provenance, list)
    ):
        raise ManifestValidationError("fusion.json audit collections are malformed.")
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            raise ManifestValidationError("fusion.json contains an invalid region audit.")
        if not region.get("automatically_resolved") and not isinstance(
            region.get("final_decision"), dict
        ):
            raise ManifestValidationError(
                f"fusion.json region {index + 1} is missing its final decision."
            )

    speakers_data, _speakers_digest = _read_json_object(
        speakers_json,
        "speakers.json",
        ManifestValidationError,
    )
    segments_data, _segments_digest = _read_json_object(
        segments_json,
        "segments.json",
        ManifestValidationError,
    )
    transcription = segments_data.get("transcription")
    if (
        speakers_data.get("engine") != "combined"
        or speakers_data.get("fusion_manifest") != "fusion.json"
        or not isinstance(transcription, dict)
        or transcription.get("engine") != "combined"
        or transcription.get("fusion_manifest") != "fusion.json"
        or transcription.get("comparison_pair_id") != pair_id
    ):
        raise ManifestValidationError(
            "Combined speakers.json or segments.json metadata is inconsistent."
        )

    stored_words = []
    for segment in segments_data.get("segments", ()):
        words = segment.get("words", ()) if isinstance(segment, dict) else ()
        if not isinstance(words, list):
            raise ManifestValidationError("Combined segments contain invalid word data.")
        stored_words.extend(words)
    if len(stored_words) != len(provenance) or validation.get("word_count") != len(
        stored_words
    ):
        raise ManifestValidationError(
            "Combined transcript and per-word provenance counts differ."
        )
    previous_end = None
    for ordinal, (stored, audit) in enumerate(zip(stored_words, provenance)):
        if not isinstance(stored, dict) or not isinstance(audit, dict):
            raise ManifestValidationError("Combined word provenance is malformed.")
        if audit.get("combined_word_ordinal") != ordinal:
            raise ManifestValidationError("Combined word ordinals are not contiguous.")
        if stored.get("word") != audit.get("text"):
            raise ManifestValidationError(
                "Combined transcript text differs from fusion word provenance."
            )
        start = stored.get("start")
        end = stored.get("end")
        audit_start = audit.get("start")
        audit_end = audit.get("end")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in (start, end, audit_start, audit_end)
        ):
            raise ManifestValidationError("Combined word timestamps are invalid.")
        start = float(start)
        end = float(end)
        if (
            start < 0.0
            or end <= start
            or not math.isclose(start, float(audit_start), abs_tol=1e-9)
            or not math.isclose(end, float(audit_end), abs_tol=1e-9)
            or (previous_end is not None and start < previous_end - 1e-9)
        ):
            raise ManifestValidationError(
                "Combined word timestamps differ from the validated fusion timeline."
            )
        previous_end = end


def _combined_provenance_source_states(
    result_root: Path,
    source_revisions: Mapping[str, Any],
) -> tuple[tuple[str, str], ...]:
    project_root = result_root.parent.parent
    states = []
    for engine in ("whisperx", "crisperwhisper"):
        record = source_revisions[engine]
        revision_id = record["revision_id"]
        manifest_path = project_root / engine / revision_id / "result.json"
        if not manifest_path.is_file():
            states.append((engine, "missing"))
            continue
        try:
            manifest = validate_result_manifest(
                manifest_path,
                project_root=project_root,
            )
            recorded = record["fingerprints"]
            current_manifest_sha256 = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
            expected_manifest_sha256 = recorded.get("result_manifest")
            exact = (
                manifest.engine == engine
                and manifest.result_id == revision_id
                and manifest.project_id == record.get("project_id")
                and manifest.model == record.get("model")
                and manifest.mode == record.get("mode")
                and manifest.execution_backend == record.get("execution_backend")
                and manifest.status == record.get("status")
                and manifest.comparison_job_id == record.get("comparison_job_id")
                and manifest.speakers_sha256 == recorded.get("speakers")
                and manifest.segments_sha256 == recorded.get("segments")
                and (
                    expected_manifest_sha256 is None
                    or current_manifest_sha256 == expected_manifest_sha256
                )
            )
        except Exception:
            exact = False
        states.append((engine, "available" if exact else "changed"))
    return tuple(states)


def _validate_result_coverage(value: Any, status: str, label: str) -> Optional[ResultCoverage]:
    if value is None:
        if status == "incomplete":
            raise ManifestValidationError(f"{label} is required for an incomplete result.")
        return None
    if status not in {"complete", "incomplete"} or not isinstance(value, dict):
        raise ManifestValidationError(f"{label} is invalid for result status {status!r}.")
    required = {
        "coverage_complete",
        "remaining_speech_active_gap_count",
        "remaining_speech_active_gap_duration",
        "remaining_speech_active_gap_ranges",
        "selected_strategy",
    }
    if set(value) != required:
        raise ManifestValidationError(f"{label} fields are invalid.")
    complete = value["coverage_complete"]
    count = value["remaining_speech_active_gap_count"]
    duration = value["remaining_speech_active_gap_duration"]
    ranges = value["remaining_speech_active_gap_ranges"]
    strategy = value["selected_strategy"]
    if (
        not isinstance(complete, bool)
        or complete != (status == "complete")
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) < 0
        or not isinstance(strategy, str)
        or not strategy.strip()
        or not isinstance(ranges, list)
        or len(ranges) != count
    ):
        raise ManifestValidationError(f"{label} summary is invalid.")
    if complete and (count != 0 or float(duration) != 0.0):
        raise ManifestValidationError(f"{label} reports gaps for a complete result.")
    if not complete and (count <= 0 or float(duration) <= 0.0):
        raise ManifestValidationError(f"{label} does not report gaps for an incomplete result.")
    validated_ranges = []
    for item in ranges:
        if not isinstance(item, dict) or set(item) != {
            "start",
            "end",
            "duration",
            "active_seconds",
            "active_blocks",
        }:
            raise ManifestValidationError(f"{label} ranges are invalid.")
        numeric = (item["start"], item["end"], item["duration"], item["active_seconds"])
        if any(
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(float(current))
            or float(current) < 0
            for current in numeric
        ) or (
            isinstance(item["active_blocks"], bool)
            or not isinstance(item["active_blocks"], int)
            or item["active_blocks"] < 0
            or float(item["end"]) <= float(item["start"])
            or not math.isclose(
                float(item["duration"]),
                float(item["end"]) - float(item["start"]),
                abs_tol=0.002,
            )
        ):
            raise ManifestValidationError(f"{label} ranges are invalid.")
        validated_ranges.append(dict(item))
    if not math.isclose(
        sum(float(item["duration"]) for item in validated_ranges),
        float(duration),
        abs_tol=max(0.002, 0.002 * max(1, count)),
    ):
        raise ManifestValidationError(f"{label} range duration is inconsistent.")
    return ResultCoverage(
        coverage_complete=complete,
        remaining_speech_active_gap_count=count,
        remaining_speech_active_gap_duration=float(duration),
        remaining_speech_active_gap_ranges=tuple(validated_ranges),
        selected_strategy=strategy.strip(),
    )


def validate_project_manifest(path: Path | str) -> ProjectManifest:
    manifest_path = Path(path)
    if manifest_path.is_dir():
        manifest_path = manifest_path / "project.json"
    manifest_path = manifest_path.resolve()
    data, _digest = _read_json_object(
        manifest_path,
        "project.json",
        ManifestValidationError,
    )
    if data.get("schema_version") != PROJECT_SCHEMA_VERSION:
        raise ManifestValidationError(
            f"project.json schema_version must be {PROJECT_SCHEMA_VERSION}."
        )
    project_root = manifest_path.parent
    project_id = _required_string(data.get("project_id"), "project.json project_id")
    display_title = _required_string(
        data.get("display_title"), "project.json display_title"
    )
    source_identity, source_path = _manifest_source(
        data.get("source"), "project.json source"
    )
    created_at = _parse_timestamp(data.get("created_at"), "project.json created_at")
    modified_at = _parse_timestamp(data.get("updated_at"), "project.json updated_at")
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        raise ManifestValidationError("project.json results must be an array.")

    records = []
    result_ids = set()
    for index, raw_record in enumerate(raw_results):
        label = f"project.json results[{index}]"
        if not isinstance(raw_record, dict):
            raise ManifestValidationError(f"{label} must be an object.")
        result_id = _required_string(raw_record.get("result_id"), f"{label}.result_id")
        if result_id in result_ids:
            raise ManifestValidationError(f"project.json contains duplicate result_id {result_id!r}.")
        result_ids.add(result_id)
        engine = _validate_engine(raw_record.get("engine"), f"{label}.engine")
        status = _validate_status(raw_record.get("status"), f"{label}.status")
        model = _optional_string(raw_record.get("model"), f"{label}.model")
        mode = _optional_string(raw_record.get("mode"), f"{label}.mode")
        execution_backend = _optional_string(
            raw_record.get("execution_backend"), f"{label}.execution_backend"
        )
        comparison_job_id = _optional_string(
            raw_record.get("comparison_job_id"), f"{label}.comparison_job_id"
        )
        paths = raw_record.get("paths")
        if not isinstance(paths, dict):
            raise ManifestValidationError(f"{label}.paths must be an object.")
        root = _safe_relative_path(
            project_root, paths.get("root"), project_root, f"{label}.paths.root"
        )
        metadata = _safe_relative_path(
            project_root, paths.get("metadata"), project_root, f"{label}.paths.metadata"
        )
        speakers_json = _safe_relative_path(
            project_root, paths.get("speakers"), project_root, f"{label}.paths.speakers"
        )
        segments_json = _safe_relative_path(
            project_root, paths.get("segments"), project_root, f"{label}.paths.segments"
        )
        if root.parent.name != engine:
            raise ManifestValidationError(
                f"{label}.paths.root must be inside the matching engine directory."
            )
        if root.name != result_id:
            raise ManifestValidationError(
                f"{label}.paths.root must end with its result_id."
            )
        if any(path.parent != root for path in (metadata, speakers_json, segments_json)):
            raise ManifestValidationError(
                f"{label} metadata and JSON paths must be directly inside its result root."
            )
        if metadata.name != "result.json":
            raise ManifestValidationError(f"{label}.paths.metadata must name result.json.")
        if speakers_json.name != "speakers.json" or segments_json.name != "segments.json":
            raise ManifestValidationError(
                f"{label} JSON paths must name speakers.json and segments.json."
            )
        speakers_sha256, segments_sha256 = _manifest_fingerprints(
            raw_record.get("fingerprint"), f"{label}.fingerprint"
        )
        fusion_json = None
        fusion_sha256 = None
        if engine == "combined":
            fusion_json = _safe_relative_path(
                project_root,
                paths.get("fusion"),
                project_root,
                f"{label}.paths.fusion",
            )
            if fusion_json.parent != root or fusion_json.name != "fusion.json":
                raise ManifestValidationError(
                    f"{label}.paths.fusion must name fusion.json in the result root."
                )
            fingerprints = raw_record.get("fingerprint")
            fusion_sha256 = (
                fingerprints.get("fusion")
                if isinstance(fingerprints, dict)
                else None
            )
            if not _is_sha256(fusion_sha256):
                raise ManifestValidationError(
                    f"{label}.fingerprint.fusion is invalid."
                )
        records.append(
            ProjectResultRecord(
                result_id=result_id,
                engine=engine,
                model=model,
                mode=mode,
                execution_backend=execution_backend,
                status=status,
                created_at=_parse_timestamp(raw_record.get("created_at"), f"{label}.created_at"),
                modified_at=_parse_timestamp(raw_record.get("updated_at"), f"{label}.updated_at"),
                root=root,
                metadata=metadata,
                speakers_json=speakers_json,
                segments_json=segments_json,
                speakers_sha256=speakers_sha256,
                segments_sha256=segments_sha256,
                comparison_job_id=comparison_job_id,
                fusion_json=fusion_json,
                fusion_sha256=fusion_sha256,
            )
        )

    active_results = data.get("active_results", {})
    if not isinstance(active_results, dict):
        raise ManifestValidationError("project.json active_results must be an object.")
    result_engines = {record.result_id: record.engine for record in records}
    for engine, result_id in active_results.items():
        if engine not in SUPPORTED_ENGINES or not isinstance(result_id, str):
            raise ManifestValidationError("project.json active_results contains an invalid entry.")
        if result_id not in result_ids:
            raise ManifestValidationError(
                f"project.json active result {result_id!r} is not present in results."
            )
        if result_engines[result_id] != engine:
            raise ManifestValidationError(
                f"project.json active result {result_id!r} belongs to a different engine."
            )

    return ProjectManifest(
        path=manifest_path,
        project_root=project_root,
        project_id=project_id,
        display_title=display_title,
        source_identity=source_identity,
        source_path=source_path,
        created_at=created_at,
        modified_at=modified_at,
        results=tuple(records),
    )


def validate_result_manifest(
    path: Path | str,
    *,
    project_root: Path | str | None = None,
) -> ResultManifest:
    manifest_path = Path(path)
    if manifest_path.is_dir():
        manifest_path = manifest_path / "result.json"
    manifest_path = manifest_path.resolve()
    result_root = manifest_path.parent
    if project_root is None:
        if result_root.parent.name not in SUPPORTED_ENGINES:
            raise ManifestValidationError(
                "result.json must be inside <project>/<engine>/<revision>."
            )
        resolved_project_root = result_root.parent.parent.resolve()
    else:
        resolved_project_root = Path(project_root).resolve()
    try:
        result_root.relative_to(resolved_project_root)
    except ValueError as exc:
        raise ManifestValidationError("result.json is outside the project directory.") from exc

    data, _digest = _read_json_object(
        manifest_path,
        "result.json",
        ManifestValidationError,
    )
    if data.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ManifestValidationError(
            f"result.json schema_version must be {RESULT_SCHEMA_VERSION}."
        )
    project_id = _required_string(data.get("project_id"), "result.json project_id")
    result_id = _required_string(data.get("result_id"), "result.json result_id")
    if result_root.name != result_id:
        raise ManifestValidationError(
            "result.json result_id must match its containing revision directory."
        )
    display_title = _required_string(
        data.get("display_title"), "result.json display_title"
    )
    engine = _validate_engine(data.get("engine"), "result.json engine")
    if result_root.parent.name != engine:
        raise ManifestValidationError(
            "result.json engine does not match its containing engine directory."
        )
    status = _validate_status(data.get("status"), "result.json status")
    coverage = _validate_result_coverage(
        data.get("coverage"),
        status,
        "result.json coverage",
    )
    source_identity, source_path = _manifest_source(data.get("source"), "result.json source")
    paths = data.get("paths")
    if not isinstance(paths, dict):
        raise ManifestValidationError("result.json paths must be an object.")
    speakers_json = _safe_relative_path(
        result_root,
        paths.get("speakers"),
        resolved_project_root,
        "result.json paths.speakers",
    )
    segments_json = _safe_relative_path(
        result_root,
        paths.get("segments"),
        resolved_project_root,
        "result.json paths.segments",
    )
    if speakers_json.parent != result_root or segments_json.parent != result_root:
        raise ManifestValidationError(
            "result.json JSON paths must be directly inside the result directory."
        )
    if speakers_json.name != "speakers.json" or segments_json.name != "segments.json":
        raise ManifestValidationError(
            "result.json paths must name speakers.json and segments.json."
        )
    speakers_sha256, segments_sha256 = _manifest_fingerprints(
        data.get("fingerprint"), "result.json fingerprint"
    )
    fusion_json = None
    fusion_sha256 = None
    output_files = ()
    provenance_source_states = ()
    if engine == "combined":
        if status != "complete":
            raise ManifestValidationError(
                "A Combined result must have Complete status."
            )
        if data.get("revision_id") != result_id:
            raise ManifestValidationError(
                "result.json revision_id must match its Combined result_id."
            )
        fusion_json = _safe_relative_path(
            result_root,
            paths.get("fusion"),
            resolved_project_root,
            "result.json paths.fusion",
        )
        if fusion_json.parent != result_root or fusion_json.name != "fusion.json":
            raise ManifestValidationError(
                "result.json paths.fusion must name fusion.json in the result directory."
            )
        fingerprint_data = data.get("fingerprint")
        fusion_sha256 = (
            fingerprint_data.get("fusion")
            if isinstance(fingerprint_data, dict)
            else None
        )
        if not _is_sha256(fusion_sha256):
            raise ManifestValidationError(
                "result.json fingerprint.fusion must be a SHA-256 fingerprint."
            )
        if not fusion_json.is_file():
            raise ManifestValidationError("Combined result is missing fusion.json.")
        _fusion_data, current_fusion_sha256 = _read_json_object(
            fusion_json,
            "fusion.json",
            ManifestValidationError,
        )
        if current_fusion_sha256 != fusion_sha256:
            raise ManifestValidationError(
                "result.json fusion fingerprint does not match fusion.json."
            )
        combined_data = data.get("combined")
        if not isinstance(combined_data, dict):
            raise ManifestValidationError(
                "A Combined result must contain combined metadata."
            )
        if combined_data.get("schema_version") != 1:
            raise ManifestValidationError(
                "result.json combined.schema_version must be 1."
            )
        pair_id = combined_data.get("comparison_pair_id")
        if not _is_sha256(pair_id):
            raise ManifestValidationError(
                "result.json combined.comparison_pair_id is invalid."
            )
        source_revisions = combined_data.get("source_revisions")
        _validate_combined_source_revisions(
            source_revisions,
            "result.json combined.source_revisions",
        )
        original_media = combined_data.get("original_media")
        if (
            not isinstance(original_media, dict)
            or original_media.get("path") != str(source_path)
            or original_media.get("source_state_at_save")
            not in SUPPORTED_SOURCE_STATES
        ):
            raise ManifestValidationError(
                "result.json combined.original_media is invalid."
            )
        outputs = data.get("outputs")
        if not isinstance(outputs, dict) or not {"srt", "txt"}.issubset(outputs):
            raise ManifestValidationError(
                "A Combined result must index SRT and TXT outputs."
            )
        validated_outputs = []
        for output_name, output_record in outputs.items():
            label = f"result.json outputs.{output_name}"
            if not isinstance(output_name, str) or not output_name.strip():
                raise ManifestValidationError("result.json output names are invalid.")
            if not isinstance(output_record, dict):
                raise ManifestValidationError(f"{label} must be an object.")
            output_path = _safe_relative_path(
                result_root,
                output_record.get("path"),
                resolved_project_root,
                f"{label}.path",
            )
            output_sha256 = output_record.get("sha256")
            if output_path.parent != result_root or not _is_sha256(output_sha256):
                raise ManifestValidationError(f"{label} is invalid.")
            if not output_path.is_file():
                raise ManifestValidationError(f"{label} is missing.")
            try:
                with output_path.open("rb") as output_file:
                    digest = hashlib.sha256()
                    for block in iter(lambda: output_file.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError as exc:
                raise ManifestValidationError(f"Could not read {label}: {exc}") from exc
            if digest.hexdigest() != output_sha256:
                raise ManifestValidationError(f"{label} fingerprint does not match.")
            validated_outputs.append((output_name, output_path, output_sha256))
        output_files = tuple(validated_outputs)
        _validate_combined_payloads(
            result_id=result_id,
            pair_id=pair_id,
            source_revisions=source_revisions,
            fusion_json=fusion_json,
            speakers_json=speakers_json,
            segments_json=segments_json,
        )
        provenance_source_states = _combined_provenance_source_states(
            result_root,
            source_revisions,
        )
    return ResultManifest(
        path=manifest_path,
        project_root=resolved_project_root,
        result_root=result_root,
        project_id=project_id,
        result_id=result_id,
        display_title=display_title,
        engine=engine,
        model=_optional_string(data.get("model"), "result.json model"),
        mode=_optional_string(data.get("mode"), "result.json mode"),
        execution_backend=_optional_string(
            data.get("execution_backend"), "result.json execution_backend"
        ),
        status=status,
        coverage=coverage,
        source_identity=source_identity,
        source_path=source_path,
        created_at=_parse_timestamp(data.get("created_at"), "result.json created_at"),
        modified_at=_parse_timestamp(data.get("updated_at"), "result.json updated_at"),
        speakers_json=speakers_json,
        segments_json=segments_json,
        speakers_sha256=speakers_sha256,
        segments_sha256=segments_sha256,
        comparison_job_id=_optional_string(
            data.get("comparison_job_id"), "result.json comparison_job_id"
        ),
        fusion_json=fusion_json,
        fusion_sha256=fusion_sha256,
        output_files=output_files,
        provenance_source_states=provenance_source_states,
    )


def _clean_optional_text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _legacy_engine_metadata(segments_data: Mapping[str, Any]):
    transcription = segments_data.get("transcription")
    if not isinstance(transcription, dict):
        return "whisperx", None, None, None
    engine = str(transcription.get("engine") or "").strip().lower()
    if engine != "crisperwhisper":
        engine = "whisperx"

    model_value = transcription.get("model")
    if isinstance(model_value, dict):
        model = _clean_optional_text(model_value.get("family")) or _clean_optional_text(
            model_value.get("model_id")
        )
    else:
        model = _clean_optional_text(model_value)

    mode = _clean_optional_text(transcription.get("mode"))
    if mode is None:
        for settings_key in ("effective_settings", "requested_settings"):
            settings = transcription.get(settings_key)
            if not isinstance(settings, dict):
                continue
            transcription_settings = settings.get("transcription")
            if isinstance(transcription_settings, dict):
                mode = _clean_optional_text(transcription_settings.get("mode"))
                if mode:
                    break
    execution_backend = _clean_optional_text(transcription.get("execution_backend"))
    return engine, model, mode, execution_backend


def _file_times(paths: Iterable[Path]) -> tuple[Optional[datetime], Optional[datetime]]:
    stats = []
    for path in paths:
        try:
            stats.append(path.stat())
        except OSError:
            pass
    if not stats:
        return None, None
    created = min(stat_result.st_ctime for stat_result in stats)
    modified = max(stat_result.st_mtime for stat_result in stats)
    return (
        datetime.fromtimestamp(created, timezone.utc),
        datetime.fromtimestamp(modified, timezone.utc),
    )


def _legacy_descriptor(preflight: ResultPreflight, pending: bool) -> ResultDescriptor:
    speakers_data = preflight.speakers_data
    segments_data = preflight.segments_data
    result_dir = preflight.identity.speakers_json.parent
    engine, model, mode, execution_backend = _legacy_engine_metadata(segments_data)
    display_title = _clean_optional_text(speakers_data.get("title")) or _clean_optional_text(
        segments_data.get("title")
    ) or result_dir.name
    source_value = segments_data.get("source_path")
    source_path = None
    if isinstance(source_value, str) and source_value.strip():
        candidate = Path(source_value).expanduser()
        source_path = candidate if candidate.is_absolute() else result_dir / candidate
    source_identity = segments_data.get("source_identity")
    if not is_valid_source_identity(source_identity):
        source_identity = None
    created_at, modified_at = _file_times(preflight.identity.paths)
    normalized_result_path = os.path.normcase(os.path.normpath(str(result_dir.resolve())))
    result_id = "legacy-" + hashlib.sha256(
        normalized_result_path.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:16]
    return ResultDescriptor(
        project_id=None,
        result_id=result_id,
        layout="legacy",
        display_title=display_title,
        engine=engine,
        model=model,
        mode=mode,
        execution_backend=execution_backend,
        status="complete",
        source_identity=source_identity,
        source_path=source_path,
        source_state=determine_source_state(source_path, source_identity),
        speakers_json=preflight.identity.speakers_json,
        segments_json=preflight.identity.segments_json,
        speakers_sha256=preflight.identity.speakers_sha256,
        segments_sha256=preflight.identity.segments_sha256,
        created_at=created_at,
        modified_at=modified_at,
        comparison_job_id=None,
        coverage=None,
        pending=pending,
    )


def _matching_project_record(
    project: ProjectManifest,
    result_manifest: ResultManifest,
) -> Optional[ProjectResultRecord]:
    for record in project.results:
        if record.result_id == result_manifest.result_id:
            return record
    return None


def _project_descriptor(
    preflight: ResultPreflight,
    result_manifest: ResultManifest,
    project_manifest: Optional[ProjectManifest],
    pending: bool,
) -> ResultDescriptor:
    identity = preflight.identity
    if identity.paths != (result_manifest.speakers_json, result_manifest.segments_json):
        raise ManifestValidationError(
            "result.json paths do not identify the requested JSON pair."
        )
    if (
        identity.speakers_sha256 != result_manifest.speakers_sha256
        or identity.segments_sha256 != result_manifest.segments_sha256
    ):
        raise ManifestValidationError(
            "result.json fingerprints do not match the current JSON files."
        )

    if project_manifest is not None:
        if project_manifest.project_id != result_manifest.project_id:
            raise ManifestValidationError("project.json and result.json project IDs differ.")
        if project_manifest.display_title != result_manifest.display_title:
            raise ManifestValidationError("project.json and result.json display titles differ.")
        if dict(project_manifest.source_identity) != dict(result_manifest.source_identity):
            raise ManifestValidationError("project.json and result.json source identities differ.")
        record = _matching_project_record(project_manifest, result_manifest)
        if record is not None:
            comparisons = (
                (record.engine, result_manifest.engine, "engine"),
                (record.model, result_manifest.model, "model"),
                (record.mode, result_manifest.mode, "mode"),
                (
                    record.execution_backend,
                    result_manifest.execution_backend,
                    "execution_backend",
                ),
                (record.status, result_manifest.status, "status"),
                (record.comparison_job_id, result_manifest.comparison_job_id, "comparison_job_id"),
                (record.speakers_json, result_manifest.speakers_json, "speakers path"),
                (record.segments_json, result_manifest.segments_json, "segments path"),
                (record.speakers_sha256, result_manifest.speakers_sha256, "speakers fingerprint"),
                (record.segments_sha256, result_manifest.segments_sha256, "segments fingerprint"),
                (record.fusion_json, result_manifest.fusion_json, "fusion path"),
                (record.fusion_sha256, result_manifest.fusion_sha256, "fusion fingerprint"),
            )
            for indexed, local, label in comparisons:
                if indexed != local:
                    raise ManifestValidationError(
                        f"project.json and result.json {label} values differ."
                    )

    return ResultDescriptor(
        project_id=result_manifest.project_id,
        result_id=result_manifest.result_id,
        layout="project",
        display_title=result_manifest.display_title,
        engine=result_manifest.engine,
        model=result_manifest.model,
        mode=result_manifest.mode,
        execution_backend=result_manifest.execution_backend,
        status=result_manifest.status,
        coverage=result_manifest.coverage,
        source_identity=result_manifest.source_identity,
        source_path=result_manifest.source_path,
        source_state=determine_source_state(
            result_manifest.source_path,
            result_manifest.source_identity,
        ),
        speakers_json=identity.speakers_json,
        segments_json=identity.segments_json,
        speakers_sha256=identity.speakers_sha256,
        segments_sha256=identity.segments_sha256,
        created_at=result_manifest.created_at,
        modified_at=result_manifest.modified_at,
        comparison_job_id=result_manifest.comparison_job_id,
        pending=pending,
        provenance_source_states=result_manifest.provenance_source_states,
    )


def descriptor_from_json_pair(
    speakers_json: Path | str,
    segments_json: Path | str,
    *,
    pending: bool = False,
) -> ResultDescriptor:
    preflight = preflight_result_pair(speakers_json, segments_json)
    result_root = preflight.identity.speakers_json.parent
    result_manifest_path = result_root / "result.json"
    if not result_manifest_path.exists():
        return _legacy_descriptor(preflight, pending)

    result_manifest = validate_result_manifest(result_manifest_path)
    project_path = result_manifest.project_root / "project.json"
    project_manifest = None
    if project_path.is_file():
        try:
            project_manifest = validate_project_manifest(project_path)
        except ManifestValidationError:
            # The per-result sidecar remains sufficient authority for transcript access.
            project_manifest = None
    return _project_descriptor(preflight, result_manifest, project_manifest, pending)


def revalidate_descriptor(descriptor: ResultDescriptor) -> ResultDescriptor:
    """Re-read and fingerprint a descriptor's JSON files immediately before use."""

    return descriptor_from_json_pair(
        descriptor.speakers_json,
        descriptor.segments_json,
        pending=descriptor.pending,
    )


def _descriptor_sort_key(descriptor: ResultDescriptor):
    modified_timestamp = (
        descriptor.modified_at.timestamp()
        if descriptor.modified_at is not None
        else float("-inf")
    )
    return (
        0 if descriptor.pending else 1,
        -modified_timestamp,
        descriptor.display_title.casefold(),
        descriptor.engine,
        os.path.normcase(str(descriptor.speakers_json)),
    )


def _descriptor_dedup_key(descriptor: ResultDescriptor):
    return (
        os.path.normcase(str(descriptor.speakers_json)),
        os.path.normcase(str(descriptor.segments_json)),
        descriptor.speakers_sha256,
        descriptor.segments_sha256,
    )


def _discover_project_directory(
    project_root: Path,
    descriptors: list[ResultDescriptor],
    issues: list[CatalogIssue],
) -> None:
    project_manifest = None
    project_path = project_root / "project.json"
    if project_path.is_file():
        try:
            project_manifest = validate_project_manifest(project_path)
        except Exception as exc:
            issues.append(CatalogIssue(project_path, str(exc)))

    seen_result_roots = set()
    if project_manifest is not None:
        for record in project_manifest.results:
            seen_result_roots.add(record.root)
            try:
                result_manifest = validate_result_manifest(
                    record.metadata,
                    project_root=project_root,
                )
                preflight = preflight_result_pair(record.speakers_json, record.segments_json)
                descriptors.append(
                    _project_descriptor(preflight, result_manifest, project_manifest, False)
                )
            except Exception as exc:
                issues.append(CatalogIssue(record.root, str(exc)))

    for engine in sorted(SUPPORTED_ENGINES):
        engine_root = project_root / engine
        if not engine_root.is_dir():
            continue
        try:
            revisions = list(engine_root.iterdir())
        except OSError as exc:
            issues.append(CatalogIssue(engine_root, str(exc)))
            continue
        for revision in revisions:
            if not revision.is_dir() or revision in seen_result_roots:
                continue
            result_path = revision / "result.json"
            if not result_path.is_file():
                if (revision / "speakers.json").exists() or (revision / "segments.json").exists():
                    issues.append(
                        CatalogIssue(revision, "Project result is missing result.json.")
                    )
                continue
            try:
                result_manifest = validate_result_manifest(
                    result_path,
                    project_root=project_root,
                )
                preflight = preflight_result_pair(
                    result_manifest.speakers_json,
                    result_manifest.segments_json,
                )
                descriptors.append(
                    _project_descriptor(preflight, result_manifest, project_manifest, False)
                )
            except Exception as exc:
                issues.append(CatalogIssue(revision, str(exc)))


def discover_results(
    output_root: Path | str,
    *,
    pending: Iterable[ResultDescriptor] = (),
) -> CatalogDiscovery:
    root = Path(output_root).resolve()
    descriptors: list[ResultDescriptor] = []
    issues: list[CatalogIssue] = []

    for pending_descriptor in pending:
        try:
            descriptors.append(replace(revalidate_descriptor(pending_descriptor), pending=True))
        except Exception as exc:
            issues.append(CatalogIssue(pending_descriptor.speakers_json.parent, str(exc)))

    if root.is_dir():
        try:
            children = list(root.iterdir())
        except OSError as exc:
            return CatalogDiscovery(tuple(descriptors), (CatalogIssue(root, str(exc)),))
        for child in children:
            if not child.is_dir():
                continue
            speakers_path = child / "speakers.json"
            segments_path = child / "segments.json"
            if speakers_path.exists() or segments_path.exists():
                if not speakers_path.is_file() or not segments_path.is_file():
                    issues.append(
                        CatalogIssue(child, "Legacy result is missing speakers.json or segments.json.")
                    )
                else:
                    try:
                        descriptors.append(
                            descriptor_from_json_pair(speakers_path, segments_path)
                        )
                    except Exception as exc:
                        issues.append(CatalogIssue(child, str(exc)))
            if (child / "project.json").exists() or any(
                (child / engine).is_dir() for engine in SUPPORTED_ENGINES
            ):
                _discover_project_directory(child, descriptors, issues)

    deduplicated = {}
    for descriptor in descriptors:
        key = _descriptor_dedup_key(descriptor)
        existing = deduplicated.get(key)
        if existing is None or (descriptor.pending and not existing.pending):
            deduplicated[key] = descriptor
    ordered = tuple(sorted(deduplicated.values(), key=_descriptor_sort_key))
    return CatalogDiscovery(ordered, tuple(issues))


__all__ = [
    "CatalogDiscovery",
    "CatalogIssue",
    "ManifestValidationError",
    "ProjectManifest",
    "ResultCatalogError",
    "ResultCoverage",
    "ResultDescriptor",
    "ResultFileIdentity",
    "ResultManifest",
    "ResultPreflight",
    "ResultPreflightError",
    "build_source_identity",
    "descriptor_from_json_pair",
    "determine_source_state",
    "discover_results",
    "is_valid_source_identity",
    "preflight_result_pair",
    "revalidate_descriptor",
    "source_identities_match",
    "validate_project_manifest",
    "validate_result_manifest",
]
