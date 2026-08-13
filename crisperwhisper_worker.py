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
        raise UnsupportedSettingError(f"Unknown or custom model family '{family}' is not supported.")

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


def _serialize_words(words: Any, full_text: str) -> list[dict[str, Any]]:
    if words is None or not isinstance(words, (list, tuple)):
        raise TranscriptionError("CrisperWhisper did not return the required word timestamps.")
    serialized = []
    previous_start = -1.0
    for index, word in enumerate(words):
        text = _value(word, "word")
        if not isinstance(text, str) or not text:
            raise TranscriptionError("CrisperWhisper returned a word without speech text.")
        start = _finite_result_number(_value(word, "start"), "word start time")
        end = _finite_result_number(_value(word, "end"), "word end time")
        if end < start:
            raise TranscriptionError("CrisperWhisper returned a word whose end precedes its start.")
        if start < previous_start:
            raise TranscriptionError("CrisperWhisper returned word timestamps out of order.")
        previous_start = start
        serialized.append({"index": index, "word": text, "start": start, "end": end})
    if full_text.strip() and not serialized:
        raise TranscriptionError("CrisperWhisper returned text without the required word timestamps.")
    return serialized


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


def transcribe_job(
    validated_request: dict[str, Any],
    *,
    runtime_loader: Callable[[], tuple[dict[str, Any], Any, Any]] | None = None,
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

        emit_event("status", "transcribe", stage="transcribing", message="Transcribing audio.")
        try:
            native_result = model.transcribe(
                validated_request["job"]["audio_path"],
                language=transcription["language"],
                mode=transcription["mode"],
                hotwords=None,
                longform_strategy="continuation",
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
            raise TranscriptionError(
                f"CrisperWhisper transcription failed ({type(exc).__name__})."
            ) from exc

        full_text = _value(native_result, "text")
        if not isinstance(full_text, str):
            raise TranscriptionError("CrisperWhisper returned an invalid transcript text value.")
        result_mode = _value(native_result, "mode")
        if result_mode != transcription["mode"]:
            raise TranscriptionError("CrisperWhisper returned a different transcription mode than requested.")
        result_language = _value(native_result, "language")
        if not isinstance(result_language, str) or not result_language:
            raise TranscriptionError("CrisperWhisper returned an invalid language value.")

        words = _serialize_words(_value(native_result, "words"), full_text)
        chunks = _serialize_chunks(_value(native_result, "chunks"))
        duration = _finite_result_number(_value(native_result, "duration"), "media duration")
        processing_time = _finite_result_number(
            _value(native_result, "processing_time"), "processing time"
        )

        requested_settings = copy.deepcopy(settings)
        effective_settings = copy.deepcopy(settings)
        effective_settings["model"]["resolved_model_id"] = model_settings["model_id"]
        effective_settings["model"]["execution_backend"] = "transformers"
        effective_settings["transcription"]["word_timestamps"] = True
        effective_settings["transcription"]["hotwords"] = []
        effective_settings["longform"]["strategy"] = "continuation"
        effective_settings["decoding"]["alignment_heads"] = None
        effective_settings["decoding"]["suppress_tokens"] = None
        effective_settings["speculative"] = copy.deepcopy(speculative)

        not_applied = []
        if duration <= 30.0:
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
                "text": full_text,
                "language": result_language,
                "mode": result_mode,
                "duration": duration,
                "processing_time": processing_time,
                "chunks": chunks,
                "words": words,
            },
        }
        native_result = None
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcript Studio CrisperWhisper worker")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    probe_parser = subparsers.add_parser("probe", help="Report isolated runtime capabilities")
    probe_parser.add_argument("--output", type=Path, help="Optionally write the probe response JSON")

    transcribe_parser = subparsers.add_parser("transcribe", help="Run one transcription job")
    transcribe_parser.add_argument("--request", type=Path, required=True, help="Protocol-v1 job JSON")
    transcribe_parser.add_argument("--output", type=Path, required=True, help="Atomic result JSON target")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        emit_event("error", operation, code=error.error_code, message=str(error))
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
