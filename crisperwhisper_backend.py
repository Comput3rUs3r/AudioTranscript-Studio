"""Isolated CrisperWhisper backend adapter for Transcript Studio.

This module contains no CrisperWhisper, Torch, Transformers, or CTranslate2
imports.  It communicates with ``crisperwhisper_worker.py`` through a strict,
versioned JSON protocol and returns ordinary Python data to the main pipeline.
"""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Optional


PROTOCOL_VERSION = 1
SUPPORTED_CRISPERWHISPER_VERSION = "2.0.2"
EVENT_PREFIX = "@@ATS_CRISPER@@"
PREFLIGHT_ENV_VAR = "ATS_CRISPER_PREFLIGHT_JSON"

OFFICIAL_MODEL_IDS = {
    "small": "nyralabs/CrisperWhisper2.0_small",
    "medium": "nyralabs/CrisperWhisper2.0_medium",
    "large": "nyralabs/CrisperWhisper2.0_large",
    "turbo": "nyralabs/CrisperWhisper2.0_turbo",
}

DEFAULT_CRISPERWHISPER_CONFIG = {
    "schema_version": 1,
    "model": {
        "family": "medium",
        "model_id": OFFICIAL_MODEL_IDS["medium"],
        "execution_backend": "transformers",
        "compute_type": "float16",
        "device": "cuda",
        "device_index": 0,
    },
    "transcription": {
        "mode": "verbatim",
        "word_timestamps": True,
        "hotwords": [],
    },
    "longform": {
        "strategy": "continuation",
        "chunk_duration": 30.0,
        "stride": 26.0,
        "context_words": 12,
        "drop_words": 2,
        "timestamp_aware_drop": True,
    },
    "decoding": {
        "temperature_fallback": True,
        "hallucination_mitigation": True,
        "early_eot_recovery": True,
        "max_new_tokens": 256,
        "suppress_tokens": None,
        "alignment_heads": None,
    },
    "speculative": {
        "enabled": False,
        "draft_model": None,
        "mode": "strict",
        "k": "auto",
        "min_tokens": 0,
        "max_tokens": 0,
    },
}

_CONFIG_SECTIONS = {"model", "transcription", "longform", "decoding", "speculative"}


class CrisperWhisperBackendError(RuntimeError):
    """A safe, actionable adapter failure suitable for the pipeline log."""


class CrisperWhisperConfigurationError(CrisperWhisperBackendError):
    pass


class CrisperWhisperRuntimeError(CrisperWhisperBackendError):
    pass


class CrisperWhisperProtocolError(CrisperWhisperBackendError):
    pass


def normalize_backend_name(value: Any) -> str:
    backend = str(value or "whisperx").strip().lower()
    if backend not in {"whisperx", "crisperwhisper"}:
        raise CrisperWhisperConfigurationError(
            "transcription_backend must be 'whisperx' or 'crisperwhisper'; "
            f"received {backend!r}."
        )
    return backend


def _merge_config_section(
    defaults: dict[str, Any],
    supplied: Any,
    label: str,
) -> dict[str, Any]:
    if supplied is None:
        return copy.deepcopy(defaults)
    if not isinstance(supplied, dict):
        raise CrisperWhisperConfigurationError(f"crisperwhisper.{label} must be an object.")
    unknown = sorted(set(supplied) - set(defaults))
    if unknown:
        raise CrisperWhisperConfigurationError(
            f"crisperwhisper.{label} contains unknown setting(s): {', '.join(unknown)}."
        )
    merged = copy.deepcopy(defaults)
    merged.update(copy.deepcopy(supplied))
    return merged


def resolve_crisperwhisper_settings(config: Any, language: Any) -> dict[str, Any]:
    """Resolve the versioned config into the worker's exact settings schema."""

    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise CrisperWhisperConfigurationError("crisperwhisper must be a configuration object.")
    unknown = sorted(set(config) - (_CONFIG_SECTIONS | {"schema_version"}))
    if unknown:
        raise CrisperWhisperConfigurationError(
            f"crisperwhisper contains unknown setting(s): {', '.join(unknown)}."
        )
    schema_version = config.get("schema_version", PROTOCOL_VERSION)
    if isinstance(schema_version, bool) or schema_version != PROTOCOL_VERSION:
        raise CrisperWhisperConfigurationError(
            f"Unsupported crisperwhisper schema_version {schema_version!r}; expected {PROTOCOL_VERSION}."
        )

    resolved = {
        section: _merge_config_section(
            DEFAULT_CRISPERWHISPER_CONFIG[section], config.get(section), section
        )
        for section in sorted(_CONFIG_SECTIONS)
    }
    model = resolved["model"]
    supplied_model = config.get("model") if isinstance(config.get("model"), dict) else {}
    family = model.get("family")
    if family in OFFICIAL_MODEL_IDS and "model_id" not in supplied_model:
        model["model_id"] = OFFICIAL_MODEL_IDS[family]

    language_text = str(language or "en").strip() or "en"
    resolved["transcription"]["language"] = language_text
    return resolved


def build_transcribe_request(audio_path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "operation": "transcribe",
        "job": {
            "audio_path": str(Path(audio_path).resolve()),
            "settings": copy.deepcopy(settings),
        },
    }


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CrisperWhisperProtocolError(f"Worker result has an invalid {label}.")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise CrisperWhisperProtocolError(f"Worker result has an invalid {label}.")
    return number


def _expect_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CrisperWhisperProtocolError(f"Worker result {label} must be an object.")
    return value


def validate_probe_response(response: Any) -> dict[str, Any]:
    root = _expect_object(response, "probe response")
    if root.get("protocol_version") != PROTOCOL_VERSION:
        raise CrisperWhisperProtocolError(
            f"CrisperWhisper worker protocol mismatch; expected version {PROTOCOL_VERSION}."
        )
    if root.get("operation") != "probe" or root.get("status") != "success":
        raise CrisperWhisperProtocolError("CrisperWhisper worker returned an invalid probe response.")
    runtime = _expect_object(root.get("runtime"), "runtime")
    versions = _expect_object(runtime.get("versions"), "runtime.versions")
    installed = versions.get("crisperwhisper")
    if installed != SUPPORTED_CRISPERWHISPER_VERSION:
        raise CrisperWhisperRuntimeError(
            "The CrisperWhisper environment is incompatible: expected CrisperWhisper "
            f"{SUPPORTED_CRISPERWHISPER_VERSION}, found {installed or 'unknown'}."
        )
    if runtime.get("cuda_available") is not True:
        raise CrisperWhisperRuntimeError(
            "CUDA is unavailable in venv-crisper. Verify its CUDA-enabled PyTorch installation."
        )
    available = runtime.get("available_execution_backends")
    if not isinstance(available, list) or "transformers" not in available:
        raise CrisperWhisperRuntimeError(
            "The Transformers backend is unavailable in venv-crisper."
        )
    capabilities = _expect_object(root.get("capabilities"), "capabilities")
    supported_backends = capabilities.get("supported_execution_backends")
    if not isinstance(supported_backends, list) or "transformers" not in supported_backends:
        raise CrisperWhisperRuntimeError(
            "The installed worker does not support the Transformers execution backend."
        )
    compatibility = _expect_object(root.get("compatibility"), "compatibility")
    if compatibility.get("installed_version_supported") is not True:
        raise CrisperWhisperRuntimeError(
            "The installed CrisperWhisper version is not supported by this worker."
        )
    return copy.deepcopy(root)


def validate_transcribe_response(
    response: Any,
    requested_settings: dict[str, Any],
) -> dict[str, Any]:
    root = _expect_object(response, "transcription response")
    if root.get("protocol_version") != PROTOCOL_VERSION:
        raise CrisperWhisperProtocolError(
            f"CrisperWhisper worker protocol mismatch; expected version {PROTOCOL_VERSION}."
        )
    if (
        root.get("operation") != "transcribe"
        or root.get("status") != "success"
        or root.get("engine") != "crisperwhisper"
    ):
        raise CrisperWhisperProtocolError("CrisperWhisper worker returned an invalid success response.")

    runtime = _expect_object(root.get("runtime"), "runtime")
    versions = _expect_object(runtime.get("versions"), "runtime.versions")
    if versions.get("crisperwhisper") != SUPPORTED_CRISPERWHISPER_VERSION:
        raise CrisperWhisperProtocolError("Worker result reports an unsupported CrisperWhisper version.")
    if runtime.get("execution_backend") != "transformers":
        raise CrisperWhisperProtocolError("Worker result did not use the requested Transformers backend.")

    settings = _expect_object(root.get("settings"), "settings")
    requested = _expect_object(settings.get("requested"), "settings.requested")
    effective = _expect_object(settings.get("effective"), "settings.effective")
    if requested != requested_settings:
        raise CrisperWhisperProtocolError("Worker result does not match the requested settings.")

    requested_model = requested_settings["model"]
    effective_model = _expect_object(effective.get("model"), "settings.effective.model")
    effective_transcription = _expect_object(
        effective.get("transcription"), "settings.effective.transcription"
    )
    if effective_model.get("model_id") != requested_model["model_id"]:
        raise CrisperWhisperProtocolError("Worker substituted a different CrisperWhisper model.")
    if effective_model.get("execution_backend") != "transformers":
        raise CrisperWhisperProtocolError("Worker substituted a different execution backend.")
    if effective_transcription.get("mode") != requested_settings["transcription"]["mode"]:
        raise CrisperWhisperProtocolError("Worker substituted a different transcription mode.")
    if effective_transcription.get("word_timestamps") is not True:
        raise CrisperWhisperProtocolError("Worker result did not retain mandatory word timestamps.")

    transcription = _expect_object(root.get("transcription"), "transcription")
    if not isinstance(transcription.get("text"), str):
        raise CrisperWhisperProtocolError("Worker result transcript text is invalid.")
    if transcription.get("mode") != requested_settings["transcription"]["mode"]:
        raise CrisperWhisperProtocolError("Worker transcript mode differs from the request.")
    if not isinstance(transcription.get("language"), str) or not transcription["language"]:
        raise CrisperWhisperProtocolError("Worker result language is invalid.")
    _finite_number(transcription.get("duration"), "duration")
    _finite_number(transcription.get("processing_time"), "processing time")

    words = transcription.get("words")
    if not isinstance(words, list):
        raise CrisperWhisperProtocolError("Worker result words must be an array.")
    previous_start = -1.0
    for word in words:
        item = _expect_object(word, "word")
        if not isinstance(item.get("word"), str) or not item["word"].strip():
            raise CrisperWhisperProtocolError("Worker result contains a word without speech text.")
        start = _finite_number(item.get("start"), "word start time")
        end = _finite_number(item.get("end"), "word end time")
        if end <= start or start < previous_start:
            raise CrisperWhisperProtocolError("Worker result word timestamps are invalid or out of order.")
        previous_start = start
    if transcription["text"].strip() and not words:
        raise CrisperWhisperProtocolError("Worker returned transcript text without word timestamps.")

    chunks = transcription.get("chunks")
    if chunks is not None and not isinstance(chunks, list):
        raise CrisperWhisperProtocolError("Worker result native chunks must be an array or null.")
    if isinstance(chunks, list):
        for chunk in chunks:
            item = _expect_object(chunk, "native chunk")
            start = _finite_number(item.get("start"), "chunk start time")
            end = _finite_number(item.get("end"), "chunk end time")
            if end < start or not isinstance(item.get("text"), str):
                raise CrisperWhisperProtocolError("Worker result contains an invalid native chunk.")
    return copy.deepcopy(root)


def _join_word_texts(words: list[dict[str, Any]]) -> str:
    text = ""
    closing = tuple(",.!?;:%)]}…。，！？；：")
    opening = tuple("([{“‘")
    for word in words:
        token = word["word"]
        if not text or token[:1].isspace() or token.startswith(closing) or text.endswith(opening):
            text += token
        else:
            text += " " + token
    return text.strip()


def _native_chunk_starts(chunks: Any) -> list[float]:
    if not isinstance(chunks, list):
        return []
    starts = {
        float(chunk["start"])
        for chunk in chunks
        if isinstance(chunk, dict)
        and isinstance(chunk.get("start"), (int, float))
        and not isinstance(chunk.get("start"), bool)
        and math.isfinite(float(chunk["start"]))
        and float(chunk["start"]) > 0
    }
    return sorted(starts)


def _locate_words_in_native_text(
    native_text: str,
    words: list[dict[str, Any]],
) -> list[tuple[int, int]]:
    """Locate every timed word in order so untimed native text is retained."""

    spans = []
    cursor = 0
    for word in words:
        token = word["word"]
        start = native_text.find(token, cursor)
        if start < 0:
            raise CrisperWhisperProtocolError(
                "CrisperWhisper words could not be reconciled with the native transcript text."
            )
        end = start + len(token)
        spans.append((start, end))
        cursor = end
    return spans


def normalize_worker_result(response: dict[str, Any]) -> dict[str, Any]:
    """Build downstream-compatible segments while retaining native metadata."""

    transcription = response["transcription"]
    source_words = transcription["words"]
    chunks = transcription.get("chunks")
    chunk_starts = _native_chunk_starts(chunks)
    native_text = transcription["text"]
    word_text_spans = _locate_words_in_native_text(native_text, source_words)
    next_chunk_index = 0
    segments: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_start_index = 0

    for index, source_word in enumerate(source_words):
        word = {
            "word": source_word["word"],
            "start": float(source_word["start"]),
            "end": float(source_word["end"]),
        }
        current.append(word)
        next_word = source_words[index + 1] if index + 1 < len(source_words) else None
        terminal = word["word"].rstrip().endswith((".", "?", "!", "…", "。", "？", "！"))
        pause = (
            next_word is not None
            and float(next_word["start"]) - word["end"] >= 0.8
        )
        max_duration = word["end"] - current[0]["start"] >= 12.0
        max_words = len(current) >= 40
        final_word = next_word is None

        native_boundary = False
        if next_word is not None:
            while (
                next_chunk_index < len(chunk_starts)
                and chunk_starts[next_chunk_index] <= current[0]["start"]
            ):
                next_chunk_index += 1
            if (
                next_chunk_index < len(chunk_starts)
                and float(next_word["start"]) >= chunk_starts[next_chunk_index]
            ):
                native_boundary = True
                next_chunk_index += 1

        if terminal or pause or max_duration or max_words or native_boundary or final_word:
            segments.append(
                {
                    "start": current[0]["start"],
                    "end": current[-1]["end"],
                    "text": "",
                    "words": current,
                    "_word_start_index": current_start_index,
                    "_word_end_index": index,
                }
            )
            current = []
            current_start_index = index + 1

    for segment_index, segment in enumerate(segments):
        word_start_index = segment.pop("_word_start_index")
        segment.pop("_word_end_index")
        text_start = 0 if segment_index == 0 else word_text_spans[word_start_index][0]
        if segment_index + 1 < len(segments):
            next_word_index = segments[segment_index + 1]["_word_start_index"]
            text_end = word_text_spans[next_word_index][0]
        else:
            text_end = len(native_text)
        segment_text = native_text[text_start:text_end].strip()
        if not segment_text:
            segment_text = _join_word_texts(segment["words"])
        segment["text"] = segment_text

    runtime = response["runtime"]
    requested = response["settings"]["requested"]
    effective = response["settings"]["effective"]
    metadata = {
        "schema_version": 1,
        "engine": "crisperwhisper",
        "package_version": runtime["versions"]["crisperwhisper"],
        "model": {
            "family": requested["model"]["family"],
            "model_id": effective["model"]["model_id"],
            "license_tier": "nyra-health-non-commercial-research",
        },
        "execution_backend": effective["model"]["execution_backend"],
        "runtime_versions": copy.deepcopy(runtime["versions"]),
        "requested_settings": copy.deepcopy(requested),
        "effective_settings": copy.deepcopy(effective),
        "not_applied_settings": copy.deepcopy(response["settings"].get("not_applied", [])),
        "native_text": transcription["text"],
        "native_chunks": copy.deepcopy(chunks),
        "duration": float(transcription["duration"]),
        "processing_time": float(transcription["processing_time"]),
    }
    return {"segments": segments, "transcription": metadata}


class CrisperWhisperBackend:
    """Launch and supervise the project-local CrisperWhisper worker."""

    def __init__(
        self,
        project_root: Path,
        *,
        interpreter_path: Optional[Path] = None,
        worker_path: Optional[Path] = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        log_callback: Callable[[str], None] = print,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.interpreter_path = Path(interpreter_path) if interpreter_path else (
            self.project_root / "venv-crisper" / "Scripts" / "python.exe"
        )
        self.worker_path = Path(worker_path) if worker_path else (
            self.project_root / "crisperwhisper_worker.py"
        )
        self._popen = popen_factory
        self._log = log_callback
        self._probe_response: Optional[dict[str, Any]] = None
        self._active_process: Any = None

    def _validate_files(self) -> None:
        if not self.interpreter_path.is_file():
            raise CrisperWhisperRuntimeError(
                "CrisperWhisper is unavailable: venv-crisper\\Scripts\\python.exe was not found. "
                "Create or restore the separate project-local CrisperWhisper environment."
            )
        if not self.worker_path.is_file():
            raise CrisperWhisperRuntimeError(
                "CrisperWhisper is unavailable: crisperwhisper_worker.py was not found in the project folder."
            )

    @staticmethod
    def _safe_event(line: str) -> Optional[dict[str, Any]]:
        if not line.startswith(EVENT_PREFIX):
            return None
        try:
            event = json.loads(line[len(EVENT_PREFIX) :])
        except json.JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None

    def _terminate_active_process(self) -> None:
        process = self._active_process
        if process is None:
            return
        try:
            if process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=3)
                return
            except (subprocess.TimeoutExpired, TimeoutError):
                pass
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=3)
                except Exception:
                    pass
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def cancel(self) -> None:
        self._terminate_active_process()

    def _run_worker(
        self,
        arguments: list[str],
        *,
        status_callback: Optional[Callable[[str, str], None]] = None,
    ) -> tuple[int, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        self._validate_files()
        command = [
            str(self.interpreter_path),
            "-u",
            str(self.worker_path),
            arguments[0],
            "--parent-pid",
            str(os.getpid()),
            *arguments[1:],
        ]
        final_success = None
        final_error = None
        try:
            self._active_process = self._popen(
                command,
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
            if self._active_process.stdout is None:
                raise CrisperWhisperRuntimeError("CrisperWhisper worker output could not be captured.")
            for raw_line in self._active_process.stdout:
                event = self._safe_event(raw_line.strip())
                if event is None or event.get("protocol_version") != PROTOCOL_VERSION:
                    continue
                event_type = event.get("event")
                if event_type == "status":
                    stage = event.get("stage")
                    message = event.get("message")
                    if isinstance(stage, str) and isinstance(message, str):
                        self._log(f"[crisper] {message}")
                        if status_callback is not None:
                            status_callback(stage, message)
                elif event_type == "success":
                    final_success = event
                elif event_type == "error":
                    final_error = event
            return_code = self._active_process.wait()
            return return_code, final_success, final_error
        except KeyboardInterrupt:
            self._terminate_active_process()
            raise
        except CrisperWhisperBackendError:
            self._terminate_active_process()
            raise
        except Exception as exc:
            self._terminate_active_process()
            raise CrisperWhisperRuntimeError(
                f"CrisperWhisper worker {arguments[0]} could not be launched or monitored "
                f"({type(exc).__name__})."
            ) from exc
        finally:
            process = self._active_process
            if process is not None and getattr(process, "stdout", None) is not None:
                try:
                    process.stdout.close()
                except Exception:
                    pass
            self._active_process = None

    @staticmethod
    def _worker_failure(operation: str, return_code: int, event: Optional[dict[str, Any]]) -> None:
        if isinstance(event, dict) and event.get("code") == "cancelled":
            raise KeyboardInterrupt
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, str) or not message:
            message = f"CrisperWhisper worker {operation} failed with exit code {return_code}."
        raise CrisperWhisperRuntimeError(message)

    def probe(self, *, force: bool = False) -> dict[str, Any]:
        if self._probe_response is not None and not force:
            return copy.deepcopy(self._probe_response)
        return_code, success, error = self._run_worker(["probe"])
        if return_code != 0:
            self._worker_failure("probe", return_code, error)
        response = success.get("response") if isinstance(success, dict) else None
        validated = validate_probe_response(response)
        self._probe_response = validated
        return copy.deepcopy(validated)

    def seed_probe_response(self, response: Any) -> dict[str, Any]:
        """Reuse a freshly validated probe supplied by the parent GUI process."""

        validated = validate_probe_response(response)
        self._probe_response = validated
        return copy.deepcopy(validated)

    def transcribe(
        self,
        audio_path: Path,
        settings: dict[str, Any],
        *,
        status_callback: Optional[Callable[[str, str], None]] = None,
    ) -> dict[str, Any]:
        self.probe()
        request = build_transcribe_request(audio_path, settings)
        with tempfile.TemporaryDirectory(prefix="ats-crisper-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            request_path = temporary_root / "request.json"
            result_path = temporary_root / "result.json"
            request_path.write_text(
                json.dumps(request, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return_code, success, error = self._run_worker(
                [
                    "transcribe",
                    "--request",
                    str(request_path),
                    "--output",
                    str(result_path),
                ],
                status_callback=status_callback,
            )
            if return_code != 0:
                self._worker_failure("transcription", return_code, error)
            if not isinstance(success, dict):
                raise CrisperWhisperProtocolError(
                    "CrisperWhisper worker exited without a final success event."
                )
            if not result_path.is_file():
                raise CrisperWhisperProtocolError(
                    "CrisperWhisper worker did not create its result JSON."
                )
            try:
                response = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise CrisperWhisperProtocolError(
                    "CrisperWhisper worker result JSON is missing, unreadable, or malformed."
                ) from exc
            return validate_transcribe_response(response, settings)
