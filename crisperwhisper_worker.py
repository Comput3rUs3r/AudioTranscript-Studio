"""Isolated CrisperWhisper 2.0 worker for Transcript Studio.

Run this module with the project-local CrisperWhisper environment::

    venv-crisper\\Scripts\\python.exe crisperwhisper_worker.py probe
    venv-crisper\\Scripts\\python.exe crisperwhisper_worker.py transcribe \
        --request job.json --output result.json

The worker intentionally depends only on the Python standard library until a
request has passed schema and settings validation.  This keeps CrisperWhisper's
runtime isolated from Transcript Studio's main environment.
"""

from __future__ import annotations

import argparse
import copy
import gc
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from typing import Any, Callable


PROTOCOL_VERSION = 1
EVENT_PREFIX = "@@ATS_CRISPER@@"
SUPPORTED_CRISPERWHISPER_VERSION = "2.0.2"

OFFICIAL_MODEL_IDS = {
    "small": "nyralabs/CrisperWhisper2.0_small",
    "medium": "nyralabs/CrisperWhisper2.0_medium",
    "large": "nyralabs/CrisperWhisper2.0_large",
    "turbo": "nyralabs/CrisperWhisper2.0_turbo",
    "small_pro": "nyralabs/CrisperWhisper2.0_small_pro",
    "medium_pro": "nyralabs/CrisperWhisper2.0_medium_pro",
    "large_pro": "nyralabs/CrisperWhisper2.0_large_pro",
    "turbo_pro": "nyralabs/CrisperWhisper2.0_turbo_pro",
}
SUPPORTED_MODEL_FAMILIES = ("small", "medium", "large", "turbo")

_ROOT_KEYS = {"protocol_version", "operation", "job"}
_JOB_KEYS = {"audio_path", "settings"}
_SETTINGS_KEYS = {
    "model",
    "transcription",
    "longform",
    "decoding",
    "speculative",
}
_MODEL_KEYS = {
    "family",
    "model_id",
    "execution_backend",
    "compute_type",
    "device",
    "device_index",
}
_TRANSCRIPTION_KEYS = {"language", "mode", "word_timestamps", "hotwords"}
_LONGFORM_KEYS = {
    "strategy",
    "chunk_duration",
    "stride",
    "context_words",
    "drop_words",
    "timestamp_aware_drop",
}
_DECODING_KEYS = {
    "temperature_fallback",
    "hallucination_mitigation",
    "early_eot_recovery",
    "max_new_tokens",
    "suppress_tokens",
    "alignment_heads",
}
_SPECULATIVE_KEYS = {
    "enabled",
    "draft_model",
    "mode",
    "k",
    "min_tokens",
    "max_tokens",
}


class WorkerError(Exception):
    """An expected worker failure with a public, non-sensitive message."""

    exit_code = 1
    error_code = "worker_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = copy.deepcopy(details) if details is not None else None


class RequestError(WorkerError):
    exit_code = 2
    error_code = "invalid_request"


class UnsupportedSettingError(RequestError):
    error_code = "unsupported_setting"


class DependencyError(WorkerError):
    exit_code = 3
    error_code = "runtime_unavailable"


class ModelLoadError(WorkerError):
    exit_code = 4
    error_code = "model_load_failed"


class TranscriptionError(WorkerError):
    exit_code = 5
    error_code = "transcription_failed"


class CoverageError(TranscriptionError):
    error_code = "coverage_incomplete"


class OutputError(WorkerError):
    exit_code = 6
    error_code = "output_failed"


class WorkerCancelled(WorkerError):
    exit_code = 130
    error_code = "cancelled"


def emit_event(event: str, operation: str, **fields: Any) -> None:
    """Print one compact, flushed machine-readable status event."""

    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "event": event,
        "operation": operation,
    }
    payload.update(fields)
    print(
        EVENT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


def _expect_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RequestError(f"{label} must be a JSON object.")
    return value


def _expect_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise RequestError(f"{label} is missing required field(s): {', '.join(missing)}.")
    if unknown:
        raise RequestError(f"{label} contains unknown field(s): {', '.join(unknown)}.")


def _expect_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise RequestError(f"{label} must be true or false.")
    return value


def _expect_int(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RequestError(f"{label} must be an integer greater than or equal to {minimum}.")
    return value


def _expect_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        raise RequestError(f"{label} must be a finite number.")
    return result


def _expect_clean_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RequestError(f"{label} must be a non-empty string without surrounding whitespace.")
    return value


def _validate_model_settings(model: dict[str, Any]) -> None:
    _expect_exact_keys(model, _MODEL_KEYS, "job.settings.model")
    family = _expect_clean_string(model["family"], "model.family")
    model_id = _expect_clean_string(model["model_id"], "model.model_id")

    if family.endswith("_pro"):
        raise UnsupportedSettingError(
            "Pro models are not supported by the Stage 1 worker. They require separate licensing."
        )
    if family not in SUPPORTED_MODEL_FAMILIES:
        if family in OFFICIAL_MODEL_IDS:
            raise UnsupportedSettingError(f"Model family '{family}' is not supported by this worker stage.")
        raise UnsupportedSettingError("Unknown or custom model families are not supported.")

    expected_model_id = OFFICIAL_MODEL_IDS[family]
    if model_id != expected_model_id:
        raise UnsupportedSettingError(
            f"model.model_id must be exactly '{expected_model_id}' for family '{family}'; "
            "custom model IDs are not supported."
        )
    if model["execution_backend"] != "transformers":
        raise UnsupportedSettingError(
            "Only execution_backend='transformers' is supported by the Stage 1 worker."
        )
    if model["compute_type"] != "float16":
        raise UnsupportedSettingError("Only compute_type='float16' is supported.")
    if model["device"] != "cuda":
        raise UnsupportedSettingError("Only device='cuda' is supported.")
    _expect_int(model["device_index"], "model.device_index", minimum=0)


def _validate_transcription_settings(transcription: dict[str, Any]) -> None:
    _expect_exact_keys(transcription, _TRANSCRIPTION_KEYS, "job.settings.transcription")
    _expect_clean_string(transcription["language"], "transcription.language")
    if transcription["mode"] not in ("verbatim", "intended"):
        raise UnsupportedSettingError(
            "transcription.mode must be exactly 'verbatim' or 'intended'."
        )
    if _expect_bool(transcription["word_timestamps"], "transcription.word_timestamps") is not True:
        raise UnsupportedSettingError("Word timestamps are mandatory and cannot be disabled.")
    if not isinstance(transcription["hotwords"], list):
        raise RequestError("transcription.hotwords must be a JSON array.")
    if transcription["hotwords"]:
        raise UnsupportedSettingError(
            "Hotwords are not supported by the Stage 1 worker and are not enabled for standard models."
        )


def _validate_longform_settings(longform: dict[str, Any]) -> None:
    _expect_exact_keys(longform, _LONGFORM_KEYS, "job.settings.longform")
    if longform["strategy"] != "continuation":
        raise UnsupportedSettingError(
            "Only longform.strategy='continuation' is supported by the Stage 1 worker."
        )
    chunk_duration = _expect_number(longform["chunk_duration"], "longform.chunk_duration")
    stride = _expect_number(longform["stride"], "longform.stride")
    if not 0.0 < chunk_duration <= 30.0:
        raise RequestError("longform.chunk_duration must be greater than 0 and at most 30 seconds.")
    if not 0.0 < stride <= chunk_duration:
        raise RequestError("longform.stride must be greater than 0 and no larger than chunk_duration.")
    _expect_int(longform["context_words"], "longform.context_words", minimum=0)
    _expect_int(longform["drop_words"], "longform.drop_words", minimum=0)
    _expect_bool(longform["timestamp_aware_drop"], "longform.timestamp_aware_drop")


def _validate_decoding_settings(decoding: dict[str, Any]) -> None:
    _expect_exact_keys(decoding, _DECODING_KEYS, "job.settings.decoding")
    _expect_bool(decoding["temperature_fallback"], "decoding.temperature_fallback")
    _expect_bool(decoding["hallucination_mitigation"], "decoding.hallucination_mitigation")
    _expect_bool(decoding["early_eot_recovery"], "decoding.early_eot_recovery")
    _expect_int(decoding["max_new_tokens"], "decoding.max_new_tokens", minimum=1)
    if decoding["suppress_tokens"] is not None:
        raise UnsupportedSettingError(
            "Token-suppression overrides are not supported by the Stage 1 worker."
        )
    if decoding["alignment_heads"] is not None:
        raise UnsupportedSettingError(
            "Alignment-head overrides are not supported by the Stage 1 worker."
        )


def _validate_speculative_settings(speculative: dict[str, Any]) -> None:
    _expect_exact_keys(speculative, _SPECULATIVE_KEYS, "job.settings.speculative")
    enabled = _expect_bool(speculative["enabled"], "speculative.enabled")
    if enabled:
        raise UnsupportedSettingError(
            "Speculative decoding is not supported with the Stage 1 Transformers backend."
        )
    if speculative["draft_model"] is not None:
        raise UnsupportedSettingError("Draft models are not supported by the Stage 1 worker.")
    if speculative["mode"] != "strict":
        raise UnsupportedSettingError("Only speculative.mode='strict' is accepted in Stage 1.")
    if speculative["k"] != "auto":
        raise UnsupportedSettingError("Speculative K overrides are not supported by the Stage 1 worker.")
    if _expect_int(speculative["min_tokens"], "speculative.min_tokens", minimum=0) != 0:
        raise UnsupportedSettingError("Speculative token-window overrides are not supported.")
    if _expect_int(speculative["max_tokens"], "speculative.max_tokens", minimum=0) != 0:
        raise UnsupportedSettingError("Speculative token-window overrides are not supported.")


def validate_transcribe_request(request: Any) -> dict[str, Any]:
    """Validate and return a defensive copy of a protocol-v1 job request."""

    root = _expect_object(request, "request")
    _expect_exact_keys(root, _ROOT_KEYS, "request")
    if (
        isinstance(root["protocol_version"], bool)
        or not isinstance(root["protocol_version"], int)
        or root["protocol_version"] != PROTOCOL_VERSION
    ):
        raise RequestError(
            f"Unsupported protocol_version {root['protocol_version']!r}; expected {PROTOCOL_VERSION}."
        )
    if root["operation"] != "transcribe":
        raise RequestError("request.operation must be exactly 'transcribe'.")

    job = _expect_object(root["job"], "request.job")
    _expect_exact_keys(job, _JOB_KEYS, "request.job")
    audio_path_text = _expect_clean_string(job["audio_path"], "job.audio_path")
    audio_path = Path(audio_path_text)
    if not audio_path.is_file():
        raise RequestError("The requested audio file does not exist or is not a regular file.")

    settings = _expect_object(job["settings"], "request.job.settings")
    _expect_exact_keys(settings, _SETTINGS_KEYS, "request.job.settings")
    model = _expect_object(settings["model"], "job.settings.model")
    transcription = _expect_object(settings["transcription"], "job.settings.transcription")
    longform = _expect_object(settings["longform"], "job.settings.longform")
    decoding = _expect_object(settings["decoding"], "job.settings.decoding")
    speculative = _expect_object(settings["speculative"], "job.settings.speculative")

    _validate_model_settings(model)
    _validate_transcription_settings(transcription)
    _validate_longform_settings(longform)
    _validate_decoding_settings(decoding)
    _validate_speculative_settings(speculative)
    return copy.deepcopy(root)


def read_request(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RequestError("The request file could not be read as UTF-8 JSON.") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RequestError("The request file contains malformed JSON.") from exc


def _package_version(distribution_name: str) -> str:
    try:
        return importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise DependencyError(f"Required package '{distribution_name}' is not installed.") from exc


def load_runtime() -> tuple[dict[str, Any], Any, Any]:
    """Import and describe the isolated runtime without exposing private paths."""

    versions = {
        "crisperwhisper": _package_version("crisperwhisper"),
        "torch": _package_version("torch"),
        "transformers": _package_version("transformers"),
        "ctranslate2": _package_version("ctranslate2"),
    }
    modules: dict[str, Any] = {}
    for module_name in ("crisperwhisper", "torch", "transformers", "ctranslate2"):
        try:
            modules[module_name] = importlib.import_module(module_name)
        except Exception as exc:
            raise DependencyError(f"Required package '{module_name}' could not be imported.") from exc

    torch_module = modules["torch"]
    try:
        cuda_available = bool(torch_module.cuda.is_available())
    except Exception:
        cuda_available = False
    cuda_version = getattr(getattr(torch_module, "version", None), "cuda", None)
    gpu_name = None
    if cuda_available:
        try:
            gpu_name = str(torch_module.cuda.get_device_name(0))
        except Exception:
            gpu_name = None

    available_backends = []
    if modules.get("torch") is not None and modules.get("transformers") is not None:
        available_backends.append("transformers")
    if modules.get("ctranslate2") is not None:
        available_backends.append("ct2")

    runtime = {
        "versions": versions,
        "cuda_available": cuda_available,
        "cuda_version": str(cuda_version) if cuda_version is not None else None,
        "gpu_name": gpu_name,
        "available_execution_backends": available_backends,
    }
    return runtime, modules["crisperwhisper"], torch_module


def build_probe_response() -> dict[str, Any]:
    runtime, _crisper_module, _torch_module = load_runtime()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "operation": "probe",
        "status": "success",
        "runtime": runtime,
        "official_model_families": list(OFFICIAL_MODEL_IDS),
        "supported_model_families": list(SUPPORTED_MODEL_FAMILIES),
        "capabilities": {
            "worker_stage": 1,
            "supported_execution_backends": ["transformers"],
            "modes": ["verbatim", "intended"],
            "word_timestamps": "required",
            "longform_strategies": ["continuation"],
            "configurable_longform_windows": True,
            "timestamp_aware_overlap_removal": True,
            "temperature_fallback": True,
            "hallucination_mitigation": True,
            "early_eot_recovery": True,
            "hotwords": False,
            "pro_models": False,
            "custom_models": False,
            "speculative_decoding": False,
            "draft_models": False,
            "token_suppression_overrides": False,
            "alignment_head_overrides": False,
        },
        "compatibility": {
            "supported_crisperwhisper_version": SUPPORTED_CRISPERWHISPER_VERSION,
            "installed_version_supported": (
                runtime["versions"]["crisperwhisper"] == SUPPORTED_CRISPERWHISPER_VERSION
            ),
        },
    }


def _public_runtime_metadata(runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "versions": copy.deepcopy(runtime["versions"]),
        "cuda_available": runtime["cuda_available"],
        "cuda_version": runtime["cuda_version"],
        "gpu_name": runtime["gpu_name"],
        "execution_backend": "transformers",
    }


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _finite_result_number(value: Any, label: str, *, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptionError(f"CrisperWhisper returned an invalid {label}.")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (not allow_zero and result == 0):
        raise TranscriptionError(f"CrisperWhisper returned an invalid {label}.")
    return result


WORD_BOUNDARY_TOLERANCE_SECONDS = 0.25
WORD_MIN_REPAIR_DURATION_SECONDS = 0.02
WORD_MAX_INFERRED_DURATION_SECONDS = 1.0

COVERAGE_SUBSTANTIAL_GAP_SECONDS = 5.0
COVERAGE_SAMPLE_RATE = 16_000
COVERAGE_FRAME_SECONDS = 0.03
COVERAGE_BLOCK_SECONDS = 1.0
COVERAGE_ABSOLUTE_FLOOR_DBFS = -50.0
COVERAGE_NOISE_PERCENTILE = 20.0
COVERAGE_REFERENCE_PERCENTILE = 90.0
COVERAGE_NOISE_MARGIN_DB = 10.0
COVERAGE_MIN_DYNAMIC_RANGE_DB = 6.0
COVERAGE_MIN_ACTIVE_SECONDS = 0.75
COVERAGE_MIN_ACTIVE_BLOCKS = 2
COVERAGE_MIN_BLOCK_ACTIVE_RATIO = 0.20
KNOWN_DIAGNOSTIC_INTERVAL = (168.20, 177.42)
TARGET_RECOVERY_MAX_INPUT_SECONDS = 25.0
TARGET_RECOVERY_CORE_SECONDS = 21.0
TARGET_RECOVERY_CORE_OVERLAP_SECONDS = 1.0
TARGET_RECOVERY_FRAMING_ATTEMPTS = (
    {"padding_seconds": 2.0, "shift_seconds": 0.0},
    {"padding_seconds": 3.0, "shift_seconds": 0.5},
)
TARGET_RECOVERY_MAX_TOKEN_OVERLAP = 8
TIMESTAMP_DIAGNOSTIC_REASONS = {
    "current_start_precedes_previous_end_beyond_tolerance",
    "previous_end_exceeds_current_start_beyond_tolerance",
    "overlap_has_no_positive_repair_interval",
}
COVERAGE_REJECTION_REASONS = {
    "candidate_duration_mismatch",
    "speech_active_gaps_remain",
    "coverage_not_improved",
    "fallback_strict_validation_failed",
    "targeted_recovery_incomplete",
}
TARGETED_RECOVERY_WARNINGS = {
    "One or more targeted windows failed strict validation and were not merged.",
}


def _normalize_diagnostic_token(value: str) -> str:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )
    return normalized or " ".join(value.casefold().split())


def _word_chunk_assignments(
    word_texts: list[str],
    chunks: list[dict[str, Any]] | None,
) -> list[dict[str, Any] | None]:
    assignments: list[dict[str, Any] | None] = [None] * len(word_texts)
    if not chunks:
        return assignments

    chunk_tokens: list[tuple[str, dict[str, Any]]] = []
    for chunk in chunks:
        safe_chunk = {
            "chunk_index": chunk["chunk_index"],
            "start": float(chunk["start"]),
            "end": float(chunk["end"]),
        }
        chunk_tokens.extend(
            (_normalize_diagnostic_token(token), safe_chunk)
            for token in chunk["text"].split()
        )

    cursor = 0
    for word_index, text in enumerate(word_texts):
        target = _normalize_diagnostic_token(text)
        for token_index in range(cursor, len(chunk_tokens)):
            if chunk_tokens[token_index][0] == target:
                assignments[word_index] = copy.deepcopy(
                    chunk_tokens[token_index][1]
                )
                cursor = token_index + 1
                break
    return assignments


def _repeated_boundary_sequence_length(
    parsed: list[dict[str, Any]],
    previous_position: int,
    current_position: int,
) -> int:
    if current_position != previous_position + 1:
        return 0
    maximum = min(8, previous_position + 1, len(parsed) - current_position)
    normalized = [
        _normalize_diagnostic_token(item["word"])
        for item in parsed
    ]
    for length in range(maximum, 0, -1):
        left = normalized[previous_position - length + 1 : previous_position + 1]
        right = normalized[current_position : current_position + length]
        if all(left) and left == right:
            return length
    return 0


def _timestamp_overlap_details(
    parsed: list[dict[str, Any]],
    assignments: list[dict[str, Any] | None],
    previous_position: int,
    current_position: int,
    overlap_duration: float,
    *,
    model_family: str,
    strategy: str,
    reason: str,
) -> dict[str, Any]:
    def optional_float(value: Any) -> float | None:
        return None if value is None else float(value)

    previous = parsed[previous_position]
    current = parsed[current_position]
    previous_chunk = assignments[previous_position]
    current_chunk = assignments[current_position]
    previous_chunk_index = (
        previous_chunk["chunk_index"] if previous_chunk is not None else None
    )
    current_chunk_index = (
        current_chunk["chunk_index"] if current_chunk is not None else None
    )
    crosses_boundary = (
        previous_chunk_index is not None
        and current_chunk_index is not None
        and previous_chunk_index != current_chunk_index
    )
    current_start = float(current["start"])
    timestamp_reset = bool(
        crosses_boundary
        and current_chunk is not None
        and current_start
        < float(current_chunk["start"]) - WORD_BOUNDARY_TOLERANCE_SECONDS
    )
    repeated_length = (
        _repeated_boundary_sequence_length(
            parsed,
            previous_position,
            current_position,
        )
        if crosses_boundary
        else 0
    )
    repair_actions = sorted(
        set(previous.get("repair_actions", []))
        | set(current.get("repair_actions", []))
    )
    return {
        "diagnostic_type": "timestamp_overlap",
        "model_family": model_family,
        "effective_strategy": strategy,
        "previous_native_chunk_index": previous_chunk_index,
        "current_native_chunk_index": current_chunk_index,
        "previous_word_index": int(previous["index"]),
        "current_word_index": int(current["index"]),
        "previous_start": optional_float(previous["start"]),
        "previous_end": optional_float(previous["end"]),
        "current_start": current_start,
        "current_end": optional_float(current["end"]),
        "overlap_duration": round(float(overlap_duration), 6),
        "crosses_native_chunk_boundary": crosses_boundary,
        "previous_chunk_window": copy.deepcopy(previous_chunk),
        "current_chunk_window": copy.deepcopy(current_chunk),
        "normalized_tokens_identical": bool(
            _normalize_diagnostic_token(previous["word"])
            == _normalize_diagnostic_token(current["word"])
        ),
        "repeated_token_sequence": bool(repeated_length),
        "repeated_sequence_length": repeated_length,
        "timestamp_reset_relative_to_chunk_start": timestamp_reset,
        "previous_boundary_was_repaired": bool(
            previous.get("repair_actions")
        ),
        "current_boundary_was_repaired": bool(current.get("repair_actions")),
        "repair_actions_attempted": repair_actions,
        "reason": reason,
        "tolerance_seconds": WORD_BOUNDARY_TOLERANCE_SECONDS,
    }


def _word_boundary(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptionError(f"CrisperWhisper returned an invalid {label}.")
    result = float(value)
    if not math.isfinite(result):
        raise TranscriptionError(f"CrisperWhisper returned an invalid {label}.")
    return result


def normalize_word_timestamps(
    words: Any,
    full_text: str,
    duration: float,
    *,
    diagnostic_context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Conservatively normalize minor native word-alignment anomalies."""

    if words is None or not isinstance(words, (list, tuple)):
        raise TranscriptionError("CrisperWhisper did not return the required word timestamps.")
    if duration < 0 or not math.isfinite(duration):
        raise TranscriptionError("CrisperWhisper returned an invalid media duration.")

    categories: dict[str, int] = {}

    def repaired(category: str, repair_actions: list[str] | None = None) -> None:
        categories[category] = categories.get(category, 0) + 1
        if repair_actions is not None:
            repair_actions.append(category)

    parsed = []
    for index, word in enumerate(words):
        text = _value(word, "word")
        if not isinstance(text, str) or not text:
            raise TranscriptionError("CrisperWhisper returned a word without speech text.")
        start = _word_boundary(_value(word, "start"), "word start time")
        end = _word_boundary(_value(word, "end"), "word end time")
        word_repairs: list[str] = []
        if start is None and end is None:
            raise TranscriptionError(
                "CrisperWhisper returned a word without usable timestamp boundaries."
            )

        for boundary_name, boundary in (("start", start), ("end", end)):
            if boundary is None:
                continue
            if boundary < -WORD_BOUNDARY_TOLERANCE_SECONDS:
                raise TranscriptionError(
                    f"CrisperWhisper returned a grossly negative word {boundary_name} time."
                )
            if boundary > duration + WORD_BOUNDARY_TOLERANCE_SECONDS:
                raise TranscriptionError(
                    f"CrisperWhisper returned a word {boundary_name} time far beyond the media duration."
                )
        if start is not None and start < 0:
            start = 0.0
            repaired("clamped_to_media_start", word_repairs)
        if end is not None and end < 0:
            end = 0.0
            repaired("clamped_to_media_start", word_repairs)
        if start is not None and start > duration:
            start = duration
            repaired("clamped_to_media_end", word_repairs)
        if end is not None and end > duration:
            end = duration
            repaired("clamped_to_media_end", word_repairs)
        parsed.append(
            {
                "index": index,
                "word": text,
                "start": start,
                "end": end,
                "repair_actions": word_repairs,
            }
        )

    context = diagnostic_context or {}
    model_family = str(context.get("model_family") or "unknown")
    strategy = str(context.get("strategy") or "unknown")
    chunk_assignments = _word_chunk_assignments(
        [item["word"] for item in parsed],
        context.get("chunks"),
    )

    def next_valid_start(position: int) -> tuple[int | None, float | None]:
        for later_position, later in enumerate(
            parsed[position + 1 :],
            start=position + 1,
        ):
            candidate = later["start"]
            if candidate is not None:
                return later_position, float(candidate)
        return None, None

    serialized = []
    previous_end = 0.0
    previous_timed_position: int | None = None
    untimed_word_count = 0
    for position, item in enumerate(parsed):
        start = item["start"]
        end = item["end"]
        following_position, following_start = next_valid_start(position)
        boundary_was_missing = start is None or end is None

        if start is None:
            if end is not None and end > previous_end:
                available = end - previous_end
                start = (
                    previous_end
                    if available <= WORD_MAX_INFERRED_DURATION_SECONDS
                    else max(
                        previous_end,
                        end - WORD_MIN_REPAIR_DURATION_SECONDS,
                    )
                )
                repaired("inferred_missing_start", item["repair_actions"])
            elif (
                end is not None
                and previous_end - end <= WORD_BOUNDARY_TOLERANCE_SECONDS
                and following_start is not None
                and following_start > previous_end
            ):
                start = previous_end
                repaired("inferred_missing_start", item["repair_actions"])
            elif end is not None and previous_end - end > WORD_BOUNDARY_TOLERANCE_SECONDS:
                raise TranscriptionError(
                    "CrisperWhisper returned a missing word start with an unreconcilable end time."
                )
            else:
                untimed_word_count += 1
                repaired("retained_untimed_text", item["repair_actions"])
                continue

        if start < previous_end:
            overlap = previous_end - start
            if overlap > WORD_BOUNDARY_TOLERANCE_SECONDS:
                item["start"] = start
                item["end"] = end
                if previous_timed_position is not None:
                    details = _timestamp_overlap_details(
                        parsed,
                        chunk_assignments,
                        previous_timed_position,
                        position,
                        overlap,
                        model_family=model_family,
                        strategy=strategy,
                        reason="current_start_precedes_previous_end_beyond_tolerance",
                    )
                else:
                    details = None
                raise TranscriptionError(
                    "CrisperWhisper returned a word sequence that cannot be made monotonic safely.",
                    details=details,
                )
            start = previous_end
            repaired("adjacent_overlap", item["repair_actions"])

        if end is None:
            if following_start is not None and following_start > start:
                available = following_start - start
                end = (
                    following_start
                    if available <= WORD_MAX_INFERRED_DURATION_SECONDS
                    else min(
                        following_start,
                        start + WORD_MIN_REPAIR_DURATION_SECONDS,
                    )
                )
                repaired("inferred_missing_end", item["repair_actions"])
            elif duration > start:
                end = min(duration, start + WORD_MIN_REPAIR_DURATION_SECONDS)
                repaired("inferred_missing_end", item["repair_actions"])
            else:
                untimed_word_count += 1
                repaired("retained_untimed_text", item["repair_actions"])
                continue

        if end < start:
            reversal = start - end
            if (
                reversal > WORD_BOUNDARY_TOLERANCE_SECONDS
                and not (
                    following_start is not None
                    and following_start > start
                    and following_start - start <= WORD_MAX_INFERRED_DURATION_SECONDS
                )
            ):
                raise TranscriptionError(
                    "CrisperWhisper returned a large word timestamp reversal that cannot be reconciled safely."
                )
            if (
                following_start is not None
                and following_start > start
                and following_start - start <= WORD_MAX_INFERRED_DURATION_SECONDS
            ):
                repaired_end = min(duration, following_start)
            else:
                repaired_end = min(
                    duration,
                    start + WORD_MIN_REPAIR_DURATION_SECONDS,
                )
            if repaired_end > start:
                end = repaired_end
                repaired(
                    "tiny_end_before_start"
                    if reversal <= WORD_BOUNDARY_TOLERANCE_SECONDS
                    else "neighbor_reconciled_reversal",
                    item["repair_actions"],
                )
            elif boundary_was_missing:
                untimed_word_count += 1
                repaired("retained_untimed_text", item["repair_actions"])
                continue
            else:
                raise TranscriptionError(
                    "CrisperWhisper returned a word timestamp reversal that has no safe repair interval."
                )

        if end == start:
            upper_bound = duration
            if following_start is not None and following_start > start:
                upper_bound = min(upper_bound, following_start)
            if upper_bound > start:
                end = (
                    upper_bound
                    if upper_bound - start <= WORD_MAX_INFERRED_DURATION_SECONDS
                    else min(upper_bound, start + WORD_MIN_REPAIR_DURATION_SECONDS)
                )
            else:
                lower_bound = previous_end
                candidate_start = max(
                    lower_bound,
                    end - WORD_MIN_REPAIR_DURATION_SECONDS,
                )
                if candidate_start < end:
                    start = candidate_start
                elif boundary_was_missing:
                    untimed_word_count += 1
                    repaired("retained_untimed_text", item["repair_actions"])
                    continue
                else:
                    raise TranscriptionError(
                        "CrisperWhisper returned a zero-duration word with no safe repair interval."
                    )
            repaired("zero_duration", item["repair_actions"])

        if following_start is not None and end > following_start:
            overlap = end - following_start
            if overlap > WORD_BOUNDARY_TOLERANCE_SECONDS or following_start <= start:
                item["start"] = start
                item["end"] = end
                details = _timestamp_overlap_details(
                    parsed,
                    chunk_assignments,
                    position,
                    following_position,
                    overlap,
                    model_family=model_family,
                    strategy=strategy,
                    reason=(
                        "previous_end_exceeds_current_start_beyond_tolerance"
                        if overlap > WORD_BOUNDARY_TOLERANCE_SECONDS
                        else "overlap_has_no_positive_repair_interval"
                    ),
                )
                raise TranscriptionError(
                    "CrisperWhisper returned overlapping word timestamps that cannot be reconciled safely.",
                    details=details,
                )
            end = following_start
            repaired("adjacent_overlap", item["repair_actions"])

        if not (0.0 <= start < end <= duration):
            raise TranscriptionError(
                "CrisperWhisper word timestamp normalization could not produce a valid media interval."
            )
        serialized.append(
            {
                "index": item["index"],
                "word": item["word"],
                "start": float(start),
                "end": float(end),
            }
        )
        item["start"] = float(start)
        item["end"] = float(end)
        previous_end = float(end)
        previous_timed_position = position

    if full_text.strip() and not serialized:
        raise TranscriptionError("CrisperWhisper returned text without the required word timestamps.")
    repair_count = sum(categories.values())
    warnings = []
    if repair_count:
        warnings.append("Minor word timestamp anomalies were normalized conservatively.")
    if untimed_word_count:
        warnings.append(
            "Some word entries remain untimed; their transcript text is retained in native text."
        )
    metadata = {
        "repair_count": repair_count,
        "categories": dict(sorted(categories.items())),
        "words_remained_untimed": bool(untimed_word_count),
        "untimed_word_count": untimed_word_count,
        "warnings": warnings,
    }
    return serialized, metadata


def _load_coverage_audio(audio_path: str) -> Any:
    from crisperwhisper.audio import load_audio

    return load_audio(audio_path)


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1.0e-12))


def _substantial_word_gaps(words: list[dict[str, Any]]) -> list[dict[str, float]]:
    gaps = []
    for previous, following in zip(words, words[1:]):
        start = float(previous["end"])
        end = float(following["start"])
        duration = end - start
        if duration >= COVERAGE_SUBSTANTIAL_GAP_SECONDS:
            gaps.append({"start": start, "end": end, "duration": duration})
    return gaps


def _known_diagnostic_interval_covered(
    words: list[dict[str, Any]],
    gaps: list[dict[str, float]],
    duration: float,
) -> bool | None:
    interval_start, interval_end = KNOWN_DIAGNOSTIC_INTERVAL
    if duration < interval_end:
        return None
    has_timed_word = any(
        float(word["end"]) >= interval_start
        and float(word["start"]) <= interval_end
        for word in words
    )
    blocked_by_substantial_gap = any(
        gap["end"] > interval_start and gap["start"] < interval_end
        for gap in gaps
    )
    return bool(has_timed_word and not blocked_by_substantial_gap)


def audit_longform_coverage(
    words: list[dict[str, Any]],
    duration: float,
    audio_supplier: Callable[[], Any],
) -> dict[str, Any]:
    """Classify substantial interior word gaps using conservative audio activity."""

    gaps = _substantial_word_gaps(words)
    configuration: dict[str, Any] = {
        "substantial_gap_seconds": COVERAGE_SUBSTANTIAL_GAP_SECONDS,
        "sample_rate": COVERAGE_SAMPLE_RATE,
        "frame_seconds": COVERAGE_FRAME_SECONDS,
        "block_seconds": COVERAGE_BLOCK_SECONDS,
        "absolute_floor_dbfs": COVERAGE_ABSOLUTE_FLOOR_DBFS,
        "noise_percentile": COVERAGE_NOISE_PERCENTILE,
        "reference_percentile": COVERAGE_REFERENCE_PERCENTILE,
        "noise_margin_db": COVERAGE_NOISE_MARGIN_DB,
        "minimum_dynamic_range_db": COVERAGE_MIN_DYNAMIC_RANGE_DB,
        "minimum_active_seconds": COVERAGE_MIN_ACTIVE_SECONDS,
        "minimum_active_blocks": COVERAGE_MIN_ACTIVE_BLOCKS,
        "minimum_block_active_ratio": COVERAGE_MIN_BLOCK_ACTIVE_RATIO,
        "minimum_zero_crossing_rate": 0.01,
        "maximum_zero_crossing_rate": 0.40,
        "effective_threshold_dbfs": None,
        "measured_noise_floor_dbfs": None,
        "measured_reference_dbfs": None,
    }
    result = {
        "substantial_gap_count": len(gaps),
        "substantial_gap_duration": round(sum(gap["duration"] for gap in gaps), 3),
        "speech_active_gap_count": 0,
        "speech_active_gap_duration": 0.0,
        "silence_gap_count": len(gaps),
        "silence_gap_duration": round(sum(gap["duration"] for gap in gaps), 3),
        "speech_active_gap_ranges": [],
        "active_frame_seconds": 0.0,
        "known_diagnostic_interval": {
            "start": KNOWN_DIAGNOSTIC_INTERVAL[0],
            "end": KNOWN_DIAGNOSTIC_INTERVAL[1],
            "covered": _known_diagnostic_interval_covered(
                words,
                gaps,
                duration,
            ),
        },
        "configuration": configuration,
        "_speech_active_gaps": [],
    }
    if not gaps:
        return result

    try:
        import numpy as np

        audio = np.asarray(audio_supplier(), dtype=np.float32)
    except (KeyboardInterrupt, WorkerCancelled):
        raise
    except Exception as exc:
        raise CoverageError(
            f"CrisperWhisper coverage audit could not load source audio ({type(exc).__name__})."
        ) from exc

    if audio.ndim > 1:
        audio = audio.mean(axis=-1) if audio.shape[-1] <= audio.shape[0] else audio.mean(axis=0)
    audio = audio.reshape(-1)
    if not audio.size or not bool(np.all(np.isfinite(audio))):
        raise CoverageError("CrisperWhisper coverage audit received invalid source audio.")
    audio_duration = len(audio) / COVERAGE_SAMPLE_RATE
    if abs(audio_duration - duration) > 1.0:
        raise CoverageError(
            "CrisperWhisper coverage audit found a duration mismatch in source audio."
        )

    frame_samples = max(1, int(round(COVERAGE_FRAME_SECONDS * COVERAGE_SAMPLE_RATE)))
    frame_count = len(audio) // frame_samples
    if frame_count <= 0:
        raise CoverageError("CrisperWhisper coverage audit received insufficient source audio.")
    framed = audio[: frame_count * frame_samples].reshape(frame_count, frame_samples)
    rms = np.sqrt(np.einsum("ij,ij->i", framed, framed) / frame_samples)
    signs = framed >= 0
    zero_crossing = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)
    noise_rms = float(np.percentile(rms, COVERAGE_NOISE_PERCENTILE))
    reference_rms = float(np.percentile(rms, COVERAGE_REFERENCE_PERCENTILE))
    absolute_floor = 10.0 ** (COVERAGE_ABSOLUTE_FLOOR_DBFS / 20.0)
    adaptive_floor = noise_rms * (10.0 ** (COVERAGE_NOISE_MARGIN_DB / 20.0))
    dynamic_range_db = _dbfs(reference_rms) - _dbfs(noise_rms)
    if dynamic_range_db >= COVERAGE_MIN_DYNAMIC_RANGE_DB:
        threshold = max(
            absolute_floor,
            min(adaptive_floor, reference_rms * 0.75),
        )
    else:
        # A nearly constant noise floor is not enough evidence of speech.
        threshold = max(absolute_floor, reference_rms * 1.05)
    configuration["effective_threshold_dbfs"] = round(_dbfs(threshold), 3)
    configuration["measured_noise_floor_dbfs"] = round(_dbfs(noise_rms), 3)
    configuration["measured_reference_dbfs"] = round(_dbfs(reference_rms), 3)
    configuration["measured_dynamic_range_db"] = round(dynamic_range_db, 3)

    speech_active_gaps = []
    total_active_seconds = 0.0
    frames_per_block = max(1, int(round(COVERAGE_BLOCK_SECONDS / COVERAGE_FRAME_SECONDS)))
    for gap in gaps:
        start_frame = max(0, int(math.floor(gap["start"] / COVERAGE_FRAME_SECONDS)))
        end_frame = min(frame_count, int(math.ceil(gap["end"] / COVERAGE_FRAME_SECONDS)))
        gap_rms = rms[start_frame:end_frame]
        gap_zcr = zero_crossing[start_frame:end_frame]
        active = (
            (gap_rms >= threshold)
            & (gap_zcr >= configuration["minimum_zero_crossing_rate"])
            & (gap_zcr <= configuration["maximum_zero_crossing_rate"])
        )
        active_seconds = float(np.count_nonzero(active)) * COVERAGE_FRAME_SECONDS
        active_blocks = 0
        for block_start in range(0, len(active), frames_per_block):
            block = active[block_start : block_start + frames_per_block]
            if len(block) and float(np.mean(block)) >= COVERAGE_MIN_BLOCK_ACTIVE_RATIO:
                active_blocks += 1
        speech_active = (
            active_seconds >= COVERAGE_MIN_ACTIVE_SECONDS
            and active_blocks >= COVERAGE_MIN_ACTIVE_BLOCKS
        )
        total_active_seconds += active_seconds
        if speech_active:
            measured = dict(gap)
            measured["active_seconds"] = active_seconds
            measured["active_blocks"] = active_blocks
            speech_active_gaps.append(measured)

    result["speech_active_gap_count"] = len(speech_active_gaps)
    result["speech_active_gap_duration"] = round(
        sum(gap["duration"] for gap in speech_active_gaps), 3
    )
    result["silence_gap_count"] = len(gaps) - len(speech_active_gaps)
    result["silence_gap_duration"] = round(
        sum(gap["duration"] for gap in gaps)
        - sum(gap["duration"] for gap in speech_active_gaps),
        3,
    )
    result["speech_active_gap_ranges"] = [
        {
            "start": round(float(gap["start"]), 3),
            "end": round(float(gap["end"]), 3),
            "duration": round(float(gap["duration"]), 3),
            "active_seconds": round(float(gap["active_seconds"]), 3),
            "active_blocks": int(gap["active_blocks"]),
        }
        for gap in speech_active_gaps
    ]
    result["active_frame_seconds"] = round(total_active_seconds, 3)
    result["_speech_active_gaps"] = speech_active_gaps
    return result


def _public_coverage_audit(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in audit.items()
        if not key.startswith("_")
    }


def _format_timeline_time(seconds: float) -> str:
    value = max(0.0, float(seconds))
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    remainder = value % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remainder:05.2f}"
    return f"{minutes:02d}:{remainder:05.2f}"


def _emit_coverage_summary(strategy_label: str, audit: dict[str, Any]) -> None:
    count = int(audit["speech_active_gap_count"])
    duration = float(audit["speech_active_gap_duration"])
    emit_event(
        "status",
        "transcribe",
        stage="coverage_summary",
        message=(
            f"{strategy_label} coverage: {count} speech-active "
            f"gap{'s' if count != 1 else ''} totaling {duration:.2f} seconds."
        ),
    )


def _emit_remaining_gap_ranges(audit: dict[str, Any]) -> None:
    ranges = audit.get("speech_active_gap_ranges") or []
    if not ranges:
        return
    formatted = ", ".join(
        f"{_format_timeline_time(item['start'])}-{_format_timeline_time(item['end'])}"
        for item in ranges
    )
    emit_event(
        "status",
        "transcribe",
        stage="coverage_remaining_ranges",
        message=f"Remaining gap ranges: {formatted}",
    )


def _emit_timestamp_overlap_status(details: dict[str, Any] | None) -> None:
    if not isinstance(details, dict) or details.get("diagnostic_type") != "timestamp_overlap":
        return
    overlap = float(details["overlap_duration"])
    strategy = str(details["effective_strategy"])
    previous_chunk = details.get("previous_native_chunk_index")
    current_chunk = details.get("current_native_chunk_index")
    if details.get("crosses_native_chunk_boundary"):
        location = f"{strategy} chunk boundary {previous_chunk}->{current_chunk}"
    elif previous_chunk is not None:
        location = f"{strategy} chunk {previous_chunk}"
    else:
        location = f"the {strategy} word timeline"
    emit_event(
        "status",
        "transcribe",
        stage="timestamp_rejected",
        message=(
            f"Timestamp validation rejected a {overlap:.2f}-second overlap "
            f"at {location}."
        ),
    )
    if details.get("repeated_token_sequence"):
        emit_event(
            "status",
            "transcribe",
            stage="timestamp_repeated_sequence",
            message=(
                "The boundary contains a repeated token sequence; "
                "no transcript was committed."
            ),
        )


def _coverage_failure_details(
    primary_audit: dict[str, Any],
    fallback_audit: dict[str, Any] | None,
    *,
    model_family: str,
    rejection_reason: str,
    fallback_diagnostic: dict[str, Any] | None = None,
    targeted_recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fallback_public = (
        _public_coverage_audit(fallback_audit)
        if fallback_audit is not None
        else None
    )
    remaining_count = (
        int(targeted_recovery["unresolved_gap_count"])
        if targeted_recovery is not None
        else int(fallback_audit["speech_active_gap_count"])
        if fallback_audit is not None
        else int(primary_audit["speech_active_gap_count"])
    )
    remaining_duration = (
        float(targeted_recovery["unresolved_gap_duration"])
        if targeted_recovery is not None
        else float(fallback_audit["speech_active_gap_duration"])
        if fallback_audit is not None
        else float(primary_audit["speech_active_gap_duration"])
    )
    recovered_count = max(
        0,
        int(primary_audit["speech_active_gap_count"]) - remaining_count,
    )
    recovered_duration = max(
        0.0,
        float(primary_audit["speech_active_gap_duration"])
        - remaining_duration,
    )
    return {
        "diagnostic_type": "coverage_failure",
        "model_family": model_family,
        "requested_strategy": "continuation",
        "attempted_strategies": ["continuation", "chunked_lcs"],
        "fallback_rejection_reason": rejection_reason,
        "recovered_gap_count": recovered_count,
        "recovered_duration": round(recovered_duration, 3),
        "remaining_speech_active_gap_count": remaining_count,
        "remaining_speech_active_gap_duration": round(remaining_duration, 3),
        "candidate_audits": {
            "continuation": _public_coverage_audit(primary_audit),
            **(
                {"chunked_lcs": fallback_public}
                if fallback_public is not None
                else {}
            ),
        },
        **(
            {"fallback_diagnostic": copy.deepcopy(fallback_diagnostic)}
            if fallback_diagnostic is not None
            else {}
        ),
        **(
            {"targeted_recovery": copy.deepcopy(targeted_recovery)}
            if targeted_recovery is not None
            else {}
        ),
    }


def _safe_diagnostic_coverage_audit(audit: Any) -> dict[str, Any] | None:
    if not isinstance(audit, dict):
        return None
    safe_audit = {
        key: copy.deepcopy(audit[key])
        for key in (
            "substantial_gap_count",
            "substantial_gap_duration",
            "speech_active_gap_count",
            "speech_active_gap_duration",
            "silence_gap_count",
            "silence_gap_duration",
            "active_frame_seconds",
        )
        if key in audit
        and isinstance(audit[key], (int, float))
        and not isinstance(audit[key], bool)
        and math.isfinite(float(audit[key]))
    }
    ranges = audit.get("speech_active_gap_ranges")
    if isinstance(ranges, list):
        safe_audit["speech_active_gap_ranges"] = [
            {
                key: copy.deepcopy(item[key])
                for key in (
                    "start",
                    "end",
                    "duration",
                    "active_seconds",
                    "active_blocks",
                )
                if key in item
                and isinstance(item[key], (int, float))
                and not isinstance(item[key], bool)
                and math.isfinite(float(item[key]))
            }
            for item in ranges
            if isinstance(item, dict)
        ]
    known = audit.get("known_diagnostic_interval")
    if isinstance(known, dict):
        safe_audit["known_diagnostic_interval"] = {
            key: copy.deepcopy(known[key])
            for key in ("start", "end", "covered")
            if key in known
            and (
                key == "covered"
                and (known[key] is None or isinstance(known[key], bool))
                or key != "covered"
                and isinstance(known[key], (int, float))
                and not isinstance(known[key], bool)
                and math.isfinite(float(known[key]))
            )
        }
    configuration = audit.get("configuration")
    if isinstance(configuration, dict):
        safe_configuration_keys = {
            "substantial_gap_seconds",
            "sample_rate",
            "frame_seconds",
            "block_seconds",
            "absolute_floor_dbfs",
            "noise_percentile",
            "reference_percentile",
            "noise_margin_db",
            "minimum_dynamic_range_db",
            "minimum_active_seconds",
            "minimum_active_blocks",
            "minimum_block_active_ratio",
            "minimum_zero_crossing_rate",
            "maximum_zero_crossing_rate",
            "effective_threshold_dbfs",
            "measured_noise_floor_dbfs",
            "measured_reference_dbfs",
            "measured_dynamic_range_db",
        }
        safe_audit["configuration"] = {
            key: copy.deepcopy(value)
            for key, value in configuration.items()
            if key in safe_configuration_keys
            and (
                value is None
                or isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        }
    return safe_audit


def _safe_targeted_recovery_details(details: Any) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    if details.get("base_strategy") not in {"continuation", "chunked_lcs"}:
        return None
    sanitized = {
        "base_strategy": details["base_strategy"],
        "base_selection_reason": details.get("base_selection_reason"),
        "attempted": details.get("attempted") is True,
    }
    if sanitized["base_selection_reason"] not in {
        "chunked_lcs_improved_but_incomplete",
        "chunked_lcs_not_improved",
    }:
        return None
    for key in (
        "target_gap_count",
        "short_window_attempt_count",
        "failed_window_attempt_count",
        "recovered_gap_count",
        "unresolved_gap_count",
    ):
        value = details.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        sanitized[key] = value
    for key in (
        "target_gap_duration",
        "recovered_duration",
        "unresolved_gap_duration",
    ):
        value = details.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            return None
        sanitized[key] = copy.deepcopy(value)
    geometry_fields = {
        "attempt",
        "gap_index",
        "window_index",
        "core_start",
        "core_end",
        "slice_start",
        "slice_end",
        "padding_seconds",
        "shift_seconds",
        "input_duration",
    }
    geometries = details.get("framing_geometries")
    if not isinstance(geometries, list):
        return None
    sanitized_geometries = []
    for geometry in geometries:
        if not isinstance(geometry, dict) or set(geometry) != geometry_fields:
            return None
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            for value in geometry.values()
        ):
            return None
        sanitized_geometries.append(copy.deepcopy(geometry))
    sanitized["framing_geometries"] = sanitized_geometries
    final_audit = _safe_diagnostic_coverage_audit(
        details.get("final_coverage_audit")
    )
    if final_audit is None:
        return None
    sanitized["final_coverage_audit"] = final_audit
    warnings = details.get("warnings")
    if (
        not isinstance(warnings, list)
        or any(warning not in TARGETED_RECOVERY_WARNINGS for warning in warnings)
    ):
        return None
    sanitized["warnings"] = list(warnings)
    return sanitized


def _safe_error_details(details: Any) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    diagnostic_type = details.get("diagnostic_type")
    if diagnostic_type == "timestamp_overlap":
        allowed_keys = {
            "diagnostic_type",
            "model_family",
            "effective_strategy",
            "previous_native_chunk_index",
            "current_native_chunk_index",
            "previous_word_index",
            "current_word_index",
            "previous_start",
            "previous_end",
            "current_start",
            "current_end",
            "overlap_duration",
            "crosses_native_chunk_boundary",
            "previous_chunk_window",
            "current_chunk_window",
            "normalized_tokens_identical",
            "repeated_token_sequence",
            "repeated_sequence_length",
            "timestamp_reset_relative_to_chunk_start",
            "previous_boundary_was_repaired",
            "current_boundary_was_repaired",
            "repair_actions_attempted",
            "reason",
            "tolerance_seconds",
        }
        if details.get("reason") not in TIMESTAMP_DIAGNOSTIC_REASONS:
            return None
        return {
            key: copy.deepcopy(details[key])
            for key in allowed_keys
            if key in details
        }
    if diagnostic_type == "coverage_failure":
        allowed_keys = {
            "diagnostic_type",
            "model_family",
            "requested_strategy",
            "attempted_strategies",
            "fallback_rejection_reason",
            "recovered_gap_count",
            "recovered_duration",
            "remaining_speech_active_gap_count",
            "remaining_speech_active_gap_duration",
            "candidate_audits",
        }
        if details.get("fallback_rejection_reason") not in COVERAGE_REJECTION_REASONS:
            return None
        sanitized = {
            key: copy.deepcopy(details[key])
            for key in allowed_keys
            if key in details and key != "candidate_audits"
        }
        candidate_audits = details.get("candidate_audits")
        if isinstance(candidate_audits, dict):
            safe_audits = {}
            for strategy in ("continuation", "chunked_lcs"):
                audit = candidate_audits.get(strategy)
                safe_audit = _safe_diagnostic_coverage_audit(audit)
                if safe_audit is not None:
                    safe_audits[strategy] = safe_audit
            sanitized["candidate_audits"] = safe_audits
        fallback_diagnostic = _safe_error_details(
            details.get("fallback_diagnostic")
        )
        if fallback_diagnostic is not None:
            sanitized["fallback_diagnostic"] = fallback_diagnostic
        targeted_recovery = _safe_targeted_recovery_details(
            details.get("targeted_recovery")
        )
        if targeted_recovery is not None:
            sanitized["targeted_recovery"] = targeted_recovery
        return sanitized
    return None


def _serialize_chunks(chunks: Any) -> list[dict[str, Any]] | None:
    if chunks is None:
        return None
    if not isinstance(chunks, (list, tuple)):
        raise TranscriptionError("CrisperWhisper returned an invalid native chunk collection.")
    serialized = []
    for position, chunk in enumerate(chunks):
        chunk_index = _value(chunk, "chunk_idx")
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
            raise TranscriptionError("CrisperWhisper returned an invalid native chunk index.")
        start = _finite_result_number(_value(chunk, "start_sec"), "chunk start time")
        end = _finite_result_number(_value(chunk, "end_sec"), "chunk end time")
        text = _value(chunk, "text")
        context = _value(chunk, "context")
        is_last = _value(chunk, "is_last", False)
        if end < start or not isinstance(text, str):
            raise TranscriptionError("CrisperWhisper returned an invalid native chunk.")
        if context is not None and not isinstance(context, str):
            raise TranscriptionError("CrisperWhisper returned an invalid native chunk context.")
        if not isinstance(is_last, bool):
            raise TranscriptionError("CrisperWhisper returned an invalid native chunk final flag.")
        serialized.append(
            {
                "position": position,
                "chunk_index": chunk_index,
                "start": start,
                "end": end,
                "text": text,
                "context": context,
                "is_last": is_last,
                "stitch_lcs_length": _value(chunk, "stitch_lcs_length"),
                "stitch_lcs_words": _value(chunk, "stitch_lcs_words"),
            }
        )
    return serialized


def _cleanup_gpu(torch_module: Any) -> None:
    gc.collect()
    try:
        if torch_module is not None and torch_module.cuda.is_available():
            torch_module.cuda.empty_cache()
            ipc_collect = getattr(torch_module.cuda, "ipc_collect", None)
            if callable(ipc_collect):
                ipc_collect()
    except Exception:
        pass


def _transcribe_with_strategy(
    model: Any,
    audio_path: str,
    transcription: dict[str, Any],
    longform: dict[str, Any],
    decoding: dict[str, Any],
    strategy: str,
) -> Any:
    try:
        return model.transcribe(
            audio_path,
            language=transcription["language"],
            mode=transcription["mode"],
            hotwords=None,
            longform_strategy=strategy,
            chunk_duration=float(longform["chunk_duration"]),
            stride=float(longform["stride"]),
            context_words=longform["context_words"],
            drop_words=longform["drop_words"],
            timestamp_aware_drop=longform["timestamp_aware_drop"],
            temperature_fallback=decoding["temperature_fallback"],
            max_new_tokens=decoding["max_new_tokens"],
            speculative_decoding=False,
            speculative_mode="strict",
            hallucination_mitigation=decoding["hallucination_mitigation"],
            early_eot_recovery=decoding["early_eot_recovery"],
            word_timestamps=True,
            alignment_heads=None,
            suppress_tokens=None,
        )
    except (KeyboardInterrupt, WorkerCancelled):
        raise
    except Exception as exc:
        label = "continuation" if strategy == "continuation" else "chunked-LCS fallback"
        raise TranscriptionError(
            f"CrisperWhisper {label} transcription failed ({type(exc).__name__})."
        ) from exc


def _transcribe_short_recovery_window(
    model: Any,
    audio_path: str,
    transcription: dict[str, Any],
    decoding: dict[str, Any],
) -> Any:
    """Transcribe one context-free short WAV using the request's safe settings."""

    try:
        return model.transcribe(
            audio_path,
            language=transcription["language"],
            mode=transcription["mode"],
            hotwords=None,
            temperature_fallback=decoding["temperature_fallback"],
            max_new_tokens=decoding["max_new_tokens"],
            speculative_decoding=False,
            speculative_mode="strict",
            hallucination_mitigation=decoding["hallucination_mitigation"],
            early_eot_recovery=decoding["early_eot_recovery"],
            word_timestamps=True,
            alignment_heads=None,
            suppress_tokens=None,
        )
    except (KeyboardInterrupt, WorkerCancelled):
        raise
    except Exception as exc:
        raise TranscriptionError(
            f"CrisperWhisper targeted short-window transcription failed ({type(exc).__name__})."
        ) from exc


def _join_word_texts(words: list[dict[str, Any]]) -> str:
    text = ""
    closing = tuple(",.!?;:%)]}\u2026\u3002\uff0c\uff01\uff1f\uff1b\uff1a")
    opening = tuple("([{\u201c\u2018")
    for word in words:
        token = word["word"]
        if (
            not text
            or token[:1].isspace()
            or token.startswith(closing)
            or text.endswith(opening)
        ):
            text += token
        else:
            text += " " + token
    return text.strip()


def _speech_active_ranges(audit: dict[str, Any]) -> list[dict[str, Any]]:
    ranges = audit.get("speech_active_gap_ranges")
    if not isinstance(ranges, list):
        ranges = audit.get("_speech_active_gaps")
    if not isinstance(ranges, list):
        return []
    return [
        {
            "start": float(item["start"]),
            "end": float(item["end"]),
            "duration": float(item["end"]) - float(item["start"]),
        }
        for item in ranges
        if isinstance(item, dict)
        and isinstance(item.get("start"), (int, float))
        and not isinstance(item.get("start"), bool)
        and isinstance(item.get("end"), (int, float))
        and not isinstance(item.get("end"), bool)
        and math.isfinite(float(item["start"]))
        and math.isfinite(float(item["end"]))
        and float(item["end"]) > float(item["start"])
    ]


def _targeted_recovery_frames(
    gaps: list[dict[str, Any]],
    duration: float,
    *,
    attempt_index: int,
) -> list[dict[str, Any]]:
    framing = TARGET_RECOVERY_FRAMING_ATTEMPTS[attempt_index]
    padding = float(framing["padding_seconds"])
    shift = float(framing["shift_seconds"])
    frames = []
    for gap_index, gap in enumerate(gaps):
        gap_start = max(0.0, min(duration, float(gap["start"])))
        gap_end = max(gap_start, min(duration, float(gap["end"])))
        core_start = gap_start
        window_index = 0
        while core_start < gap_end:
            core_end = min(gap_end, core_start + TARGET_RECOVERY_CORE_SECONDS)
            slice_start = max(0.0, core_start - padding + shift)
            slice_end = min(duration, core_end + padding + shift)
            if slice_start > core_start:
                slice_start = core_start
            if slice_end < core_end:
                slice_end = core_end
            if slice_end - slice_start > TARGET_RECOVERY_MAX_INPUT_SECONDS:
                slice_end = slice_start + TARGET_RECOVERY_MAX_INPUT_SECONDS
                if slice_end < core_end:
                    slice_end = core_end
                    slice_start = max(0.0, slice_end - TARGET_RECOVERY_MAX_INPUT_SECONDS)
            frames.append(
                {
                    "attempt": attempt_index + 1,
                    "gap_index": gap_index + 1,
                    "window_index": window_index + 1,
                    "core_start": round(core_start, 6),
                    "core_end": round(core_end, 6),
                    "slice_start": round(slice_start, 6),
                    "slice_end": round(slice_end, 6),
                    "padding_seconds": padding,
                    "shift_seconds": shift,
                }
            )
            if core_end >= gap_end:
                break
            core_start = core_end - TARGET_RECOVERY_CORE_OVERLAP_SECONDS
            window_index += 1
    return frames


def _write_targeted_recovery_wav(
    audio: Any,
    start: float,
    end: float,
    output_path: Path,
) -> tuple[float, float]:
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim > 1:
        samples = (
            samples.mean(axis=-1)
            if samples.shape[-1] <= samples.shape[0]
            else samples.mean(axis=0)
        )
    samples = samples.reshape(-1)
    start_sample = max(0, min(len(samples), int(round(start * COVERAGE_SAMPLE_RATE))))
    end_sample = max(
        start_sample,
        min(len(samples), int(round(end * COVERAGE_SAMPLE_RATE))),
    )
    if end_sample <= start_sample:
        raise CoverageError("CrisperWhisper targeted recovery produced an empty audio slice.")
    if end_sample - start_sample > int(
        round(TARGET_RECOVERY_MAX_INPUT_SECONDS * COVERAGE_SAMPLE_RATE)
    ):
        raise CoverageError("CrisperWhisper targeted recovery exceeded the short-form limit.")
    clipped = np.clip(samples[start_sample:end_sample], -1.0, 1.0)
    pcm = np.rint(clipped * 32767.0).astype("<i2", copy=False)
    try:
        with wave.open(str(output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(COVERAGE_SAMPLE_RATE)
            handle.writeframes(pcm.tobytes())
    except (KeyboardInterrupt, WorkerCancelled):
        raise
    except (OSError, wave.Error) as exc:
        raise CoverageError(
            f"CrisperWhisper targeted recovery could not write a WAV slice ({type(exc).__name__})."
        ) from exc
    return (
        start_sample / COVERAGE_SAMPLE_RATE,
        end_sample / COVERAGE_SAMPLE_RATE,
    )


def _sequence_time_overlaps(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> bool:
    if not left or not right:
        return False
    return (
        float(right[0]["start"]) < float(left[-1]["end"])
        and float(right[-1]["end"]) > float(left[0]["start"])
    )


def _left_boundary_duplicate_count(
    existing: list[dict[str, Any]],
    recovered: list[dict[str, Any]],
    core_start: float,
) -> int:
    left = [word for word in existing if float(word["start"]) < core_start]
    maximum = min(TARGET_RECOVERY_MAX_TOKEN_OVERLAP, len(left), len(recovered))
    for length in range(maximum, 0, -1):
        old = left[-length:]
        new = recovered[:length]
        if [
            _normalize_diagnostic_token(word["word"]) for word in old
        ] == [
            _normalize_diagnostic_token(word["word"]) for word in new
        ] and _sequence_time_overlaps(old, new):
            return length
    return 0


def _right_boundary_duplicate_count(
    existing: list[dict[str, Any]],
    recovered: list[dict[str, Any]],
    core_end: float,
) -> int:
    right = [word for word in existing if float(word["end"]) > core_end]
    maximum = min(TARGET_RECOVERY_MAX_TOKEN_OVERLAP, len(right), len(recovered))
    for length in range(maximum, 0, -1):
        old = right[:length]
        new = recovered[-length:]
        if [
            _normalize_diagnostic_token(word["word"]) for word in old
        ] == [
            _normalize_diagnostic_token(word["word"]) for word in new
        ] and _sequence_time_overlaps(new, old):
            return length
    return 0


def _merge_targeted_words(
    existing_words: list[dict[str, Any]],
    recovered_words: list[dict[str, Any]],
    *,
    core_start: float,
    core_end: float,
    duration: float,
) -> tuple[list[dict[str, Any]], int]:
    existing = [
        {
            "word": item["word"],
            "start": float(item["start"]),
            "end": float(item["end"]),
            "origin": "base",
        }
        for item in existing_words
    ]
    recovered = [
        {
            "word": item["word"],
            "start": float(item["start"]),
            "end": float(item["end"]),
            "origin": "recovery",
        }
        for item in recovered_words
    ]
    left_duplicates = _left_boundary_duplicate_count(existing, recovered, core_start)
    if left_duplicates:
        recovered = recovered[left_duplicates:]
    right_duplicates = _right_boundary_duplicate_count(existing, recovered, core_end)
    if right_duplicates:
        recovered = recovered[:-right_duplicates]

    recovered = [
        item
        for item in recovered
        if core_start
        <= (float(item["start"]) + float(item["end"])) / 2.0
        <= core_end
    ]
    unique = []
    for candidate in recovered:
        duplicate = any(
            _normalize_diagnostic_token(candidate["word"])
            == _normalize_diagnostic_token(current["word"])
            and candidate["start"] < current["end"]
            and candidate["end"] > current["start"]
            for current in existing + unique
        )
        if not duplicate:
            unique.append(candidate)
    if not unique:
        return copy.deepcopy(existing_words), 0

    combined = existing + unique
    combined.sort(
        key=lambda item: (
            float(item["start"]),
            0 if item["origin"] == "base" else 1,
            float(item["end"]),
        )
    )
    monotonic = []
    for item in combined:
        current = dict(item)
        if monotonic and current["start"] < monotonic[-1]["end"]:
            previous = monotonic[-1]
            overlap = previous["end"] - current["start"]
            if overlap > WORD_BOUNDARY_TOLERANCE_SECONDS:
                raise TranscriptionError(
                    "CrisperWhisper targeted recovery produced a non-monotonic merged timeline."
                )
            if current["origin"] == "recovery" and current["end"] > previous["end"]:
                current["start"] = previous["end"]
            elif previous["origin"] == "recovery" and current["origin"] == "base":
                if previous["start"] >= current["start"]:
                    raise TranscriptionError(
                        "CrisperWhisper targeted recovery produced an unsafe boundary overlap."
                    )
                previous["end"] = current["start"]
            else:
                raise TranscriptionError(
                    "CrisperWhisper targeted recovery would alter the selected base transcript."
                )
        if current["end"] <= current["start"]:
            raise TranscriptionError(
                "CrisperWhisper targeted recovery produced an invalid merged word interval."
            )
        monotonic.append(current)

    plain = [
        {
            "word": item["word"],
            "start": item["start"],
            "end": item["end"],
        }
        for item in monotonic
    ]
    normalized, _repairs = normalize_word_timestamps(
        plain,
        _join_word_texts(plain),
        duration,
        diagnostic_context={
            "model_family": "unknown",
            "strategy": "targeted_short_window",
            "chunks": None,
        },
    )
    base_texts = [word["word"] for word in existing_words]
    merged_texts = [word["word"] for word in normalized]
    cursor = 0
    for base_text in base_texts:
        try:
            cursor = merged_texts.index(base_text, cursor) + 1
        except ValueError as exc:
            raise TranscriptionError(
                "CrisperWhisper targeted recovery did not preserve every base word."
            ) from exc
    return normalized, len(unique)


def _normalize_transcription_candidate(
    native_result: Any,
    expected_mode: str,
    *,
    model_family: str,
    strategy: str,
) -> dict[str, Any]:
    full_text = _value(native_result, "text")
    if not isinstance(full_text, str):
        raise TranscriptionError("CrisperWhisper returned an invalid transcript text value.")
    result_mode = _value(native_result, "mode")
    if result_mode != expected_mode:
        raise TranscriptionError("CrisperWhisper returned a different transcription mode than requested.")
    result_language = _value(native_result, "language")
    if not isinstance(result_language, str) or not result_language:
        raise TranscriptionError("CrisperWhisper returned an invalid language value.")

    duration = _finite_result_number(_value(native_result, "duration"), "media duration")
    chunks = _serialize_chunks(_value(native_result, "chunks"))
    words, word_timestamp_repairs = normalize_word_timestamps(
        _value(native_result, "words"),
        full_text,
        duration,
        diagnostic_context={
            "model_family": model_family,
            "strategy": strategy,
            "chunks": chunks,
        },
    )
    return {
        "text": full_text,
        "language": result_language,
        "mode": result_mode,
        "duration": duration,
        "processing_time": _finite_result_number(
            _value(native_result, "processing_time"), "processing time"
        ),
        "chunks": chunks,
        "words": words,
        "word_timestamp_repairs": word_timestamp_repairs,
    }


def _merge_repair_metadata(
    base: dict[str, Any],
    additions: list[dict[str, Any]],
) -> dict[str, Any]:
    categories = dict(base.get("categories") or {})
    warnings = list(base.get("warnings") or [])
    untimed_count = int(base.get("untimed_word_count") or 0)
    for metadata in additions:
        for category, count in (metadata.get("categories") or {}).items():
            categories[category] = categories.get(category, 0) + int(count)
        untimed_count += int(metadata.get("untimed_word_count") or 0)
        for warning in metadata.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
    return {
        "repair_count": sum(categories.values()),
        "categories": dict(sorted(categories.items())),
        "words_remained_untimed": bool(untimed_count),
        "untimed_word_count": untimed_count,
        "warnings": warnings,
    }


def _run_targeted_recovery(
    base_candidate: dict[str, Any],
    base_audit: dict[str, Any],
    *,
    base_strategy: str,
    base_selection_reason: str,
    model: Any,
    model_family: str,
    transcription: dict[str, Any],
    decoding: dict[str, Any],
    audio_supplier: Callable[[], Any],
    auditor: Callable[
        [list[dict[str, Any]], float, Callable[[], Any]], dict[str, Any]
    ],
    slice_writer: Callable[
        [Any, float, float, Path], tuple[float, float]
    ] = _write_targeted_recovery_wav,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if base_candidate["word_timestamp_repairs"].get("words_remained_untimed"):
        raise CoverageError(
            "CrisperWhisper targeted recovery cannot safely place words around untimed base text."
        )

    target_ranges = _speech_active_ranges(base_audit)
    target_count = int(base_audit["speech_active_gap_count"])
    target_duration = float(base_audit["speech_active_gap_duration"])
    emit_event(
        "status",
        "transcribe",
        stage="targeted_recovery_start",
        message=(
            f"Starting targeted recovery for {target_count} speech-active "
            f"gap{'s' if target_count != 1 else ''}."
        ),
    )
    for gap_index, gap in enumerate(target_ranges, start=1):
        emit_event(
            "status",
            "transcribe",
            stage="targeted_recovery_gap",
            message=(
                f"Recovering gap {gap_index}/{target_count}: "
                f"{_format_timeline_time(gap['start'])}-"
                f"{_format_timeline_time(gap['end'])}."
            ),
        )

    working = copy.deepcopy(base_candidate)
    working_words = copy.deepcopy(base_candidate["words"])
    duration = float(base_candidate["duration"])
    current_audit = copy.deepcopy(base_audit)
    framing_geometries = []
    short_window_attempt_count = 0
    failed_window_attempt_count = 0
    applied_repair_metadata: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="ats-crisper-recovery-") as temp_name:
        temporary_root = Path(temp_name)
        for attempt_index in range(len(TARGET_RECOVERY_FRAMING_ATTEMPTS)):
            unresolved = _speech_active_ranges(current_audit)
            if not unresolved:
                break
            frames = _targeted_recovery_frames(
                unresolved,
                duration,
                attempt_index=attempt_index,
            )
            for frame_index, frame in enumerate(frames):
                slice_path = temporary_root / (
                    f"attempt-{attempt_index + 1:02d}-window-{frame_index + 1:04d}.wav"
                )
                short_window_attempt_count += 1
                try:
                    actual_start, actual_end = slice_writer(
                        audio_supplier(),
                        float(frame["slice_start"]),
                        float(frame["slice_end"]),
                        slice_path,
                    )
                    actual_duration = actual_end - actual_start
                    if (
                        actual_duration <= 0
                        or actual_duration
                        > TARGET_RECOVERY_MAX_INPUT_SECONDS + 1.0e-6
                    ):
                        raise CoverageError(
                            "CrisperWhisper targeted recovery created an invalid short window."
                        )
                    geometry = copy.deepcopy(frame)
                    geometry["slice_start"] = round(actual_start, 6)
                    geometry["slice_end"] = round(actual_end, 6)
                    geometry["input_duration"] = round(actual_duration, 6)
                    framing_geometries.append(geometry)

                    native = _transcribe_short_recovery_window(
                        model,
                        str(slice_path),
                        transcription,
                        decoding,
                    )
                    candidate = _normalize_transcription_candidate(
                        native,
                        transcription["mode"],
                        model_family=model_family,
                        strategy="targeted_short_window",
                    )
                    if (
                        candidate["duration"]
                        > actual_duration + WORD_BOUNDARY_TOLERANCE_SECONDS
                    ):
                        raise TranscriptionError(
                            "CrisperWhisper targeted recovery reported an invalid slice duration."
                        )
                    absolute_words = [
                        {
                            "word": word["word"],
                            "start": float(word["start"]) + actual_start,
                            "end": float(word["end"]) + actual_start,
                        }
                        for word in candidate["words"]
                    ]
                    merged_words, inserted = _merge_targeted_words(
                        working_words,
                        absolute_words,
                        core_start=float(frame["core_start"]),
                        core_end=float(frame["core_end"]),
                        duration=duration,
                    )
                    if inserted:
                        working_words = merged_words
                        applied_repair_metadata.append(
                            candidate["word_timestamp_repairs"]
                        )
                except (KeyboardInterrupt, WorkerCancelled):
                    raise
                except TranscriptionError:
                    failed_window_attempt_count += 1
                finally:
                    try:
                        slice_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            current_audit = auditor(
                working_words,
                duration,
                audio_supplier,
            )

    unresolved_count = int(current_audit["speech_active_gap_count"])
    unresolved_duration = float(current_audit["speech_active_gap_duration"])
    recovered_count = max(0, target_count - unresolved_count)
    recovered_duration = max(0.0, target_duration - unresolved_duration)
    warnings = []
    if failed_window_attempt_count:
        warnings.append(
            "One or more targeted windows failed strict validation and were not merged."
        )
    targeted_metadata = {
        "base_strategy": base_strategy,
        "base_selection_reason": base_selection_reason,
        "attempted": True,
        "target_gap_count": target_count,
        "target_gap_duration": round(target_duration, 3),
        "short_window_attempt_count": short_window_attempt_count,
        "failed_window_attempt_count": failed_window_attempt_count,
        "framing_geometries": framing_geometries,
        "recovered_gap_count": recovered_count,
        "recovered_duration": round(recovered_duration, 3),
        "unresolved_gap_count": unresolved_count,
        "unresolved_gap_duration": round(unresolved_duration, 3),
        "final_coverage_audit": _public_coverage_audit(current_audit),
        "warnings": warnings,
    }
    emit_event(
        "status",
        "transcribe",
        stage="targeted_recovery_summary",
        message=(
            f"Targeted recovery restored {recovered_count} "
            f"gap{'s' if recovered_count != 1 else ''} totaling "
            f"{recovered_duration:.2f} seconds."
        ),
    )
    if unresolved_count:
        emit_event(
            "status",
            "transcribe",
            stage="targeted_recovery_failed",
            message=(
                f"Targeted recovery left {unresolved_count} speech-active "
                f"gap{'s' if unresolved_count != 1 else ''} totaling "
                f"{unresolved_duration:.2f} seconds; no result was committed."
            ),
        )
    else:
        emit_event(
            "status",
            "transcribe",
            stage="targeted_recovery_passed",
            message="Final coverage audit passed.",
        )

    working["words"] = working_words
    working["text"] = _join_word_texts(working_words)
    working["word_timestamp_repairs"] = _merge_repair_metadata(
        base_candidate["word_timestamp_repairs"],
        applied_repair_metadata,
    )
    return working, current_audit, targeted_metadata


def _coverage_metadata(
    primary_audit: dict[str, Any],
    final_audit: dict[str, Any],
    attempted_strategies: list[str],
    selected_strategy: str,
    *,
    fallback_audit: dict[str, Any] | None = None,
    targeted_recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_strategy = (
        targeted_recovery["base_strategy"]
        if targeted_recovery is not None
        else selected_strategy
    )
    fallback_used = base_strategy == "chunked_lcs"
    recovered_count = max(
        0,
        int(primary_audit["speech_active_gap_count"])
        - int(final_audit["speech_active_gap_count"]),
    )
    recovered_duration = max(
        0.0,
        float(primary_audit["speech_active_gap_duration"])
        - float(final_audit["speech_active_gap_duration"]),
    )
    warnings = []
    if primary_audit["substantial_gap_count"] and not primary_audit["speech_active_gap_count"]:
        warnings.append(
            "Substantial interior timeline gaps were classified as non-speech audio."
        )
    if fallback_used:
        warnings.append(
            "Continuation coverage was incomplete; chunked LCS passed the speech-active coverage audit."
            if targeted_recovery is None
            else "Continuation coverage was incomplete; chunked LCS was selected as the better base candidate."
        )
    if targeted_recovery is not None:
        warnings.append(
            "Targeted context-free short windows passed the final speech-active coverage audit."
        )
    return {
        "requested_strategy": "continuation",
        "attempted_strategies": list(attempted_strategies),
        "selected_strategy": selected_strategy,
        "fallback_used": fallback_used,
        "substantial_gap_count": primary_audit["substantial_gap_count"],
        "substantial_gap_duration": primary_audit["substantial_gap_duration"],
        "speech_active_gap_count": primary_audit["speech_active_gap_count"],
        "speech_active_gap_duration": primary_audit["speech_active_gap_duration"],
        "silence_gap_count": primary_audit["silence_gap_count"],
        "silence_gap_duration": primary_audit["silence_gap_duration"],
        "speech_active_gap_ranges": copy.deepcopy(
            primary_audit["speech_active_gap_ranges"]
        ),
        "recovered_gap_count": recovered_count,
        "recovered_duration": recovered_duration,
        "remaining_speech_active_gap_count": final_audit["speech_active_gap_count"],
        "remaining_speech_active_gap_duration": final_audit["speech_active_gap_duration"],
        "remaining_speech_active_gap_ranges": copy.deepcopy(
            final_audit["speech_active_gap_ranges"]
        ),
        "known_diagnostic_interval": copy.deepcopy(
            final_audit["known_diagnostic_interval"]
        ),
        "configuration": copy.deepcopy(primary_audit["configuration"]),
        "candidate_audits": {
            "continuation": _public_coverage_audit(primary_audit),
            **(
                {"chunked_lcs": _public_coverage_audit(fallback_audit)}
                if fallback_audit is not None
                else {}
            ),
        },
        **(
            {"targeted_recovery": copy.deepcopy(targeted_recovery)}
            if targeted_recovery is not None
            else {}
        ),
        "warnings": warnings,
    }


def transcribe_job(
    validated_request: dict[str, Any],
    *,
    runtime_loader: Callable[[], tuple[dict[str, Any], Any, Any]] | None = None,
    coverage_audio_loader: Callable[[str], Any] | None = None,
    coverage_auditor: Callable[
        [list[dict[str, Any]], float, Callable[[], Any]], dict[str, Any]
    ] | None = None,
    targeted_slice_writer: Callable[
        [Any, float, float, Path], tuple[float, float]
    ] | None = None,
) -> dict[str, Any]:
    """Run one already validated request and return its protocol response."""

    settings = validated_request["job"]["settings"]
    model_settings = settings["model"]
    transcription = settings["transcription"]
    longform = settings["longform"]
    decoding = settings["decoding"]
    speculative = settings["speculative"]

    runtime, crisper_module, torch_module = (runtime_loader or load_runtime)()
    if runtime["versions"]["crisperwhisper"] != SUPPORTED_CRISPERWHISPER_VERSION:
        raise DependencyError(
            "This worker stage requires CrisperWhisper "
            f"{SUPPORTED_CRISPERWHISPER_VERSION}; the installed version is unsupported."
        )
    if not runtime["cuda_available"]:
        raise DependencyError("CUDA is not available in the CrisperWhisper environment.")
    if "transformers" not in runtime["available_execution_backends"]:
        raise DependencyError("The Transformers execution backend is unavailable.")

    model = None
    emit_event("status", "transcribe", stage="loading_model", message="Loading CrisperWhisper model.")
    try:
        try:
            model = crisper_module.CrisperWhisperModel(
                model_settings["model_id"],
                backend="transformers",
                compute_type="float16",
                device="cuda",
                device_index=model_settings["device_index"],
            )
        except (KeyboardInterrupt, WorkerCancelled):
            raise
        except Exception as exc:
            raise ModelLoadError(
                f"CrisperWhisper model loading failed ({type(exc).__name__})."
            ) from exc

        resolved_backend = getattr(model, "backend", "transformers")
        if resolved_backend != "transformers":
            raise ModelLoadError(
                "CrisperWhisper did not initialize the explicitly requested Transformers backend."
            )
        model_version = getattr(model, "model_version", 2)
        if model_version != 2:
            raise ModelLoadError("The selected checkpoint is not a CrisperWhisper 2.0 model.")

        audio_path = validated_request["job"]["audio_path"]
        emit_event("status", "transcribe", stage="transcribing", message="Transcribing audio.")
        native_result = _transcribe_with_strategy(
            model,
            audio_path,
            transcription,
            longform,
            decoding,
            "continuation",
        )
        try:
            selected = _normalize_transcription_candidate(
                native_result,
                transcription["mode"],
                model_family=model_settings["family"],
                strategy="continuation",
            )
        except TranscriptionError as exc:
            _emit_timestamp_overlap_status(exc.details)
            raise
        native_result = None
        selected_strategy = "continuation"
        attempted_strategies = ["continuation"]
        coverage = None

        if selected["duration"] > 30.0:
            emit_event(
                "status",
                "transcribe",
                stage="auditing_coverage",
                message="Auditing long-form transcript coverage.",
            )
            loader = coverage_audio_loader or _load_coverage_audio
            auditor = coverage_auditor or audit_longform_coverage
            loaded_audio: list[Any] = []

            def audio_supplier() -> Any:
                if not loaded_audio:
                    loaded_audio.append(loader(audio_path))
                return loaded_audio[0]

            primary_audit = auditor(
                selected["words"],
                selected["duration"],
                audio_supplier,
            )
            _emit_coverage_summary("Continuation", primary_audit)
            active_count = primary_audit["speech_active_gap_count"]
            final_audit = primary_audit
            fallback_audit = None
            targeted_recovery = None
            if active_count:
                emit_event(
                    "status",
                    "transcribe",
                    stage="coverage_fallback",
                    message=(
                        f"Detected {active_count} speech-active coverage "
                        f"gap{'s' if active_count != 1 else ''}; retrying with chunked LCS."
                    ),
                )
                attempted_strategies.append("chunked_lcs")
                try:
                    fallback_native = _transcribe_with_strategy(
                        model,
                        audio_path,
                        transcription,
                        longform,
                        decoding,
                        "chunked_lcs",
                    )
                    fallback = _normalize_transcription_candidate(
                        fallback_native,
                        transcription["mode"],
                        model_family=model_settings["family"],
                        strategy="chunked_lcs",
                    )
                    fallback_native = None
                    if abs(fallback["duration"] - selected["duration"]) > 0.25:
                        raise CoverageError(
                            "CrisperWhisper coverage candidates reported inconsistent media durations.",
                            details=_coverage_failure_details(
                                primary_audit,
                                None,
                                model_family=model_settings["family"],
                                rejection_reason="candidate_duration_mismatch",
                            ),
                        )
                    fallback_audit = auditor(
                        fallback["words"],
                        fallback["duration"],
                        audio_supplier,
                    )
                    _emit_coverage_summary("Chunked LCS", fallback_audit)
                    remaining_count = fallback_audit["speech_active_gap_count"]
                    recovered_count = max(
                        0,
                        int(primary_audit["speech_active_gap_count"])
                        - int(remaining_count),
                    )
                    recovered_duration = max(
                        0.0,
                        float(primary_audit["speech_active_gap_duration"])
                        - float(fallback_audit["speech_active_gap_duration"]),
                    )
                    emit_event(
                        "status",
                        "transcribe",
                        stage="coverage_recovery_summary",
                        message=(
                            f"Chunked LCS recovered {recovered_count} "
                            f"gap{'s' if recovered_count != 1 else ''} totaling "
                            f"{recovered_duration:.2f} seconds."
                        ),
                    )
                    improved = (
                        remaining_count < primary_audit["speech_active_gap_count"]
                        and fallback_audit["speech_active_gap_duration"]
                        < primary_audit["speech_active_gap_duration"]
                    )
                    if improved and remaining_count == 0:
                        selected = fallback
                        final_audit = fallback_audit
                        selected_strategy = "chunked_lcs"
                        emit_event(
                            "status",
                            "transcribe",
                            stage="coverage_recovered",
                            message="Coverage recovery succeeded using chunked LCS.",
                        )
                    else:
                        rejection_reason = (
                            "coverage_not_improved"
                            if not improved
                            else "speech_active_gaps_remain"
                        )
                        rejection_message = (
                            "Coverage recovery rejected: chunked LCS did not improve "
                            "both speech-active gap count and duration."
                            if not improved
                            else (
                                f"Coverage recovery rejected: {remaining_count} "
                                f"speech-active gap"
                                f"{'s remain' if remaining_count != 1 else ' remains'}."
                            )
                        )
                        emit_event(
                            "status",
                            "transcribe",
                            stage="coverage_rejected",
                            message=rejection_message,
                        )
                        _emit_remaining_gap_ranges(fallback_audit)
                        base_candidate = fallback if improved else selected
                        base_audit = fallback_audit if improved else primary_audit
                        base_strategy = "chunked_lcs" if improved else "continuation"
                        selected, final_audit, targeted_recovery = _run_targeted_recovery(
                            base_candidate,
                            base_audit,
                            base_strategy=base_strategy,
                            base_selection_reason=(
                                "chunked_lcs_improved_but_incomplete"
                                if improved
                                else "chunked_lcs_not_improved"
                            ),
                            model=model,
                            model_family=model_settings["family"],
                            transcription=transcription,
                            decoding=decoding,
                            audio_supplier=audio_supplier,
                            auditor=auditor,
                            slice_writer=(
                                targeted_slice_writer
                                or _write_targeted_recovery_wav
                            ),
                        )
                        selected_strategy = (
                            f"{base_strategy}_plus_targeted_recovery"
                        )
                        if final_audit["speech_active_gap_count"]:
                            raise CoverageError(
                                "CrisperWhisper long-form coverage remained incomplete "
                                "after targeted short-window recovery.",
                                details=_coverage_failure_details(
                                    primary_audit,
                                    fallback_audit,
                                    model_family=model_settings["family"],
                                    rejection_reason="targeted_recovery_incomplete",
                                    targeted_recovery=targeted_recovery,
                                ),
                            )
                except (KeyboardInterrupt, WorkerCancelled):
                    raise
                except TranscriptionError as exc:
                    _emit_timestamp_overlap_status(exc.details)
                    emit_event(
                        "status",
                        "transcribe",
                        stage="coverage_failed",
                        message="Coverage recovery failed; no incomplete result was committed.",
                    )
                    if isinstance(exc, CoverageError):
                        raise
                    raise CoverageError(
                        "CrisperWhisper chunked LCS recovery did not produce a valid result.",
                        details=_coverage_failure_details(
                            primary_audit,
                            fallback_audit,
                            model_family=model_settings["family"],
                            rejection_reason="fallback_strict_validation_failed",
                            fallback_diagnostic=exc.details,
                        ),
                    ) from exc
            coverage = _coverage_metadata(
                primary_audit,
                final_audit,
                attempted_strategies,
                selected_strategy,
                fallback_audit=fallback_audit,
                targeted_recovery=targeted_recovery,
            )

        requested_settings = copy.deepcopy(settings)
        effective_settings = copy.deepcopy(settings)
        effective_settings["model"]["resolved_model_id"] = model_settings["model_id"]
        effective_settings["model"]["execution_backend"] = "transformers"
        effective_settings["transcription"]["word_timestamps"] = True
        effective_settings["transcription"]["hotwords"] = []
        effective_settings["longform"]["strategy"] = selected_strategy
        effective_settings["decoding"]["alignment_heads"] = None
        effective_settings["decoding"]["suppress_tokens"] = None
        effective_settings["speculative"] = copy.deepcopy(speculative)

        not_applied = []
        if selected["duration"] <= 30.0:
            not_applied.append(
                {
                    "settings": [
                        "longform.strategy",
                        "longform.chunk_duration",
                        "longform.stride",
                        "longform.context_words",
                        "longform.drop_words",
                        "longform.timestamp_aware_drop",
                        "decoding.early_eot_recovery",
                    ],
                    "reason": "The media duration did not exceed 30 seconds.",
                }
            )

        response = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": "transcribe",
            "status": "success",
            "engine": "crisperwhisper",
            "runtime": _public_runtime_metadata(runtime),
            "settings": {
                "requested": requested_settings,
                "effective": effective_settings,
                "not_applied": not_applied,
            },
            "transcription": {
                **selected,
                **({"longform_coverage": coverage} if coverage is not None else {}),
            },
        }
        return response
    finally:
        model = None
        _cleanup_gpu(torch_module)


def atomic_write_json(path: Path, payload: Any) -> None:
    """Serialize fully, then atomically replace *path* without partial output."""

    try:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OutputError("The worker result could not be encoded as JSON.") from exc

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError("The result directory could not be prepared.") from exc

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (KeyboardInterrupt, WorkerCancelled):
        raise
    except OSError as exc:
        raise OutputError("The result JSON could not be written atomically.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _termination_handler(_signum: int, _frame: Any) -> None:
    raise WorkerCancelled("CrisperWhisper worker cancellation was requested.")


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _termination_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _termination_handler)


def _start_parent_watch(args: argparse.Namespace) -> None:
    """Exit if the adapter process disappears while the worker is active."""

    parent_pid = getattr(args, "parent_pid", None)
    if parent_pid is None:
        return

    cleanup_root = None
    request_path = getattr(args, "request", None)
    output_path = getattr(args, "output", None)
    output_existed = bool(output_path and Path(output_path).exists())
    if request_path and output_path:
        request_parent = Path(request_path).resolve().parent
        output_parent = Path(output_path).resolve().parent
        if request_parent == output_parent and request_parent.name.startswith("ats-crisper-"):
            cleanup_root = request_parent

    def parent_is_alive() -> bool:
        if os.name == "nt":
            try:
                import ctypes

                synchronize = 0x00100000
                wait_timeout = 0x00000102
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(synchronize, False, parent_pid)
                if not handle:
                    return False
                try:
                    return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                return False
        try:
            os.kill(parent_pid, 0)
            return True
        except OSError:
            return False

    def watch_parent() -> None:
        while parent_is_alive():
            time.sleep(0.5)
        if cleanup_root is not None:
            try:
                Path(request_path).unlink(missing_ok=True)
            except OSError:
                pass
            if not output_existed:
                try:
                    Path(output_path).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                for partial in cleanup_root.glob(f".{Path(output_path).name}.*.tmp"):
                    partial.unlink(missing_ok=True)
                cleanup_root.rmdir()
            except OSError:
                pass
        os._exit(WorkerCancelled.exit_code)

    threading.Thread(target=watch_parent, name="crisper-parent-watch", daemon=True).start()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcript Studio CrisperWhisper worker")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    probe_parser = subparsers.add_parser("probe", help="Report isolated runtime capabilities")
    probe_parser.add_argument("--output", type=Path, help="Optionally write the probe response JSON")
    probe_parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)

    transcribe_parser = subparsers.add_parser("transcribe", help="Run one transcription job")
    transcribe_parser.add_argument("--request", type=Path, required=True, help="Protocol-v1 job JSON")
    transcribe_parser.add_argument("--output", type=Path, required=True, help="Atomic result JSON target")
    transcribe_parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _start_parent_watch(args)
    operation = args.operation
    try:
        if operation == "probe":
            emit_event("status", operation, stage="probing", message="Inspecting CrisperWhisper runtime.")
            response = build_probe_response()
            if args.output is not None:
                atomic_write_json(args.output, response)
            emit_event("success", operation, response=response)
            return 0

        emit_event("status", operation, stage="validating", message="Validating transcription request.")
        request = read_request(args.request)
        validated = validate_transcribe_request(request)
        response = transcribe_job(validated)
        emit_event("status", operation, stage="writing_result", message="Writing transcription result.")
        atomic_write_json(args.output, response)
        emit_event(
            "success",
            operation,
            model=response["settings"]["effective"]["model"]["model_id"],
            mode=response["transcription"]["mode"],
            word_count=len(response["transcription"]["words"]),
        )
        return 0
    except KeyboardInterrupt:
        error = WorkerCancelled("CrisperWhisper worker cancellation was requested.")
        emit_event("error", operation, code=error.error_code, message=str(error))
        return error.exit_code
    except WorkerError as error:
        details = _safe_error_details(error.details)
        emit_event(
            "error",
            operation,
            code=error.error_code,
            message=str(error),
            **({"details": details} if details is not None else {}),
        )
        return error.exit_code
    except Exception as exc:
        emit_event(
            "error",
            operation,
            code="unexpected_error",
            message=f"The CrisperWhisper worker failed unexpectedly ({type(exc).__name__}).",
        )
        return 1


if __name__ == "__main__":
    _install_signal_handlers()
    raise SystemExit(main())
