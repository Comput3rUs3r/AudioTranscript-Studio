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
_TIMESTAMP_DIAGNOSTIC_REASONS = {
    "current_start_precedes_previous_end_beyond_tolerance",
    "previous_end_exceeds_current_start_beyond_tolerance",
    "overlap_has_no_positive_repair_interval",
}
_COVERAGE_REJECTION_REASONS = {
    "candidate_duration_mismatch",
    "speech_active_gaps_remain",
    "coverage_not_improved",
    "fallback_strict_validation_failed",
    "targeted_recovery_incomplete",
}
_TIMESTAMP_REPAIR_ACTIONS = {
    "clamped_to_media_start",
    "clamped_to_media_end",
    "inferred_missing_start",
    "inferred_missing_end",
    "retained_untimed_text",
    "adjacent_overlap",
    "tiny_end_before_start",
    "neighbor_reconciled_reversal",
    "zero_duration",
}
_TARGETED_SELECTED_STRATEGIES = {
    "continuation_plus_targeted_recovery": "continuation",
    "chunked_lcs_plus_targeted_recovery": "chunked_lcs",
}
_TARGETED_RECOVERY_WARNINGS = {
    "One or more targeted windows failed strict validation and were not merged.",
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

    def __init__(
        self,
        message: str,
        *,
        diagnostic_details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic_details = (
            copy.deepcopy(diagnostic_details)
            if diagnostic_details is not None
            else None
        )


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


def _validate_word_timestamp_repairs(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    metadata = _expect_object(value, "transcription.word_timestamp_repairs")
    repair_count = metadata.get("repair_count")
    untimed_count = metadata.get("untimed_word_count")
    remained_untimed = metadata.get("words_remained_untimed")
    if (
        isinstance(repair_count, bool)
        or not isinstance(repair_count, int)
        or repair_count < 0
        or isinstance(untimed_count, bool)
        or not isinstance(untimed_count, int)
        or untimed_count < 0
        or not isinstance(remained_untimed, bool)
    ):
        raise CrisperWhisperProtocolError(
            "Worker result word timestamp repair summary is invalid."
        )
    categories = metadata.get("categories")
    if not isinstance(categories, dict) or any(
        not isinstance(category, str)
        or not category
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        for category, count in categories.items()
    ):
        raise CrisperWhisperProtocolError(
            "Worker result word timestamp repair categories are invalid."
        )
    if sum(categories.values()) != repair_count:
        raise CrisperWhisperProtocolError(
            "Worker result word timestamp repair counts are inconsistent."
        )
    if remained_untimed != bool(untimed_count):
        raise CrisperWhisperProtocolError(
            "Worker result untimed-word repair summary is inconsistent."
        )
    warnings = metadata.get("warnings")
    if not isinstance(warnings, list) or any(
        not isinstance(warning, str)
        or not warning
        or len(warning) > 240
        for warning in warnings
    ):
        raise CrisperWhisperProtocolError(
            "Worker result word timestamp repair warnings are invalid."
        )
    return copy.deepcopy(metadata)


def _validate_coverage_audit(value: Any, label: str) -> dict[str, Any]:
    audit = _expect_object(value, label)
    count_keys = ("substantial_gap_count", "speech_active_gap_count")
    duration_keys = (
        "substantial_gap_duration",
        "speech_active_gap_duration",
        "active_frame_seconds",
    )
    for key in count_keys:
        current = audit.get(key)
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise CrisperWhisperProtocolError(
                "Worker result long-form coverage counts are invalid."
            )
    for key in duration_keys:
        current = audit.get(key)
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(float(current))
            or float(current) < 0
        ):
            raise CrisperWhisperProtocolError(
                "Worker result long-form coverage durations are invalid."
            )
    if audit["speech_active_gap_count"] > audit["substantial_gap_count"]:
        raise CrisperWhisperProtocolError(
            "Worker result long-form coverage counts are inconsistent."
        )
    silence_count = audit.get("silence_gap_count")
    silence_duration = audit.get("silence_gap_duration")
    if silence_count is not None or silence_duration is not None:
        if (
            isinstance(silence_count, bool)
            or not isinstance(silence_count, int)
            or silence_count < 0
            or isinstance(silence_duration, bool)
            or not isinstance(silence_duration, (int, float))
            or not math.isfinite(float(silence_duration))
            or float(silence_duration) < 0
            or silence_count + audit["speech_active_gap_count"]
            != audit["substantial_gap_count"]
        ):
            raise CrisperWhisperProtocolError(
                "Worker result long-form silence-gap summary is invalid."
            )
    ranges = audit.get("speech_active_gap_ranges")
    if ranges is not None:
        if not isinstance(ranges, list) or len(ranges) != audit["speech_active_gap_count"]:
            raise CrisperWhisperProtocolError(
                "Worker result long-form coverage gap ranges are invalid."
            )
        for item in ranges:
            if not isinstance(item, dict) or set(item) != {
                "start",
                "end",
                "duration",
                "active_seconds",
                "active_blocks",
            }:
                raise CrisperWhisperProtocolError(
                    "Worker result long-form coverage gap ranges are invalid."
                )
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
                raise CrisperWhisperProtocolError(
                    "Worker result long-form coverage gap ranges are invalid."
                )
    known = audit.get("known_diagnostic_interval")
    if known is not None:
        if not isinstance(known, dict) or set(known) != {"start", "end", "covered"}:
            raise CrisperWhisperProtocolError(
                "Worker result diagnostic interval coverage is invalid."
            )
        start = known["start"]
        end = known["end"]
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or not math.isfinite(float(start))
            or isinstance(end, bool)
            or not isinstance(end, (int, float))
            or not math.isfinite(float(end))
            or float(end) <= float(start)
            or known["covered"] is not None
            and not isinstance(known["covered"], bool)
        ):
            raise CrisperWhisperProtocolError(
                "Worker result diagnostic interval coverage is invalid."
            )
    configuration = _expect_object(audit.get("configuration"), f"{label}.configuration")
    if not configuration:
        raise CrisperWhisperProtocolError(
            "Worker result long-form coverage configuration is missing."
        )
    allowed_configuration_keys = {
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
    for key, current in configuration.items():
        if key not in allowed_configuration_keys:
            raise CrisperWhisperProtocolError(
                "Worker result long-form coverage configuration is invalid."
            )
        if current is None:
            continue
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(float(current))
        ):
            raise CrisperWhisperProtocolError(
                "Worker result long-form coverage configuration is invalid."
            )
    return copy.deepcopy(audit)


def _validate_targeted_recovery_metadata(
    value: Any,
    *,
    require_complete: bool,
) -> dict[str, Any]:
    metadata = _expect_object(value, "targeted recovery metadata")
    required = {
        "base_strategy",
        "base_selection_reason",
        "attempted",
        "target_gap_count",
        "target_gap_duration",
        "short_window_attempt_count",
        "failed_window_attempt_count",
        "framing_geometries",
        "recovered_gap_count",
        "recovered_duration",
        "unresolved_gap_count",
        "unresolved_gap_duration",
        "final_coverage_audit",
        "warnings",
    }
    if set(metadata) != required:
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery metadata fields are invalid."
        )
    if metadata["base_strategy"] not in {"continuation", "chunked_lcs"}:
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery base strategy is invalid."
        )
    expected_reason = {
        "continuation": "chunked_lcs_not_improved",
        "chunked_lcs": "chunked_lcs_improved_but_incomplete",
    }[metadata["base_strategy"]]
    if metadata["base_selection_reason"] != expected_reason:
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery base selection reason is invalid."
        )
    if metadata["attempted"] is not True:
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery attempt state is invalid."
        )
    for key in (
        "target_gap_count",
        "short_window_attempt_count",
        "failed_window_attempt_count",
        "recovered_gap_count",
        "unresolved_gap_count",
    ):
        current = metadata[key]
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise CrisperWhisperProtocolError(
                "Worker targeted recovery counts are invalid."
            )
    if metadata["target_gap_count"] <= 0:
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery target count is invalid."
        )
    for key in (
        "target_gap_duration",
        "recovered_duration",
        "unresolved_gap_duration",
    ):
        current = metadata[key]
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(float(current))
            or float(current) < 0
        ):
            raise CrisperWhisperProtocolError(
                "Worker targeted recovery durations are invalid."
            )
    if (
        metadata["recovered_gap_count"]
        != max(0, metadata["target_gap_count"] - metadata["unresolved_gap_count"])
        or not math.isclose(
            float(metadata["recovered_duration"]),
            max(
                0.0,
                float(metadata["target_gap_duration"])
                - float(metadata["unresolved_gap_duration"]),
            ),
            abs_tol=0.002,
        )
        or metadata["failed_window_attempt_count"]
        > metadata["short_window_attempt_count"]
    ):
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery summary is inconsistent."
        )
    if require_complete and (
        metadata["unresolved_gap_count"] != 0
        or float(metadata["unresolved_gap_duration"]) != 0.0
    ):
        raise CrisperWhisperProtocolError(
            "Worker accepted incomplete targeted recovery metadata."
        )

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
    geometries = metadata["framing_geometries"]
    if not isinstance(geometries, list):
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery framing data is invalid."
        )
    for geometry in geometries:
        if not isinstance(geometry, dict) or set(geometry) != geometry_fields:
            raise CrisperWhisperProtocolError(
                "Worker targeted recovery framing data is invalid."
            )
        for key, current in geometry.items():
            if (
                isinstance(current, bool)
                or not isinstance(current, (int, float))
                or not math.isfinite(float(current))
                or float(current) < 0
            ):
                raise CrisperWhisperProtocolError(
                    "Worker targeted recovery framing values are invalid."
                )
        if any(
            not isinstance(geometry[key], int) or isinstance(geometry[key], bool)
            for key in ("attempt", "gap_index", "window_index")
        ):
            raise CrisperWhisperProtocolError(
                "Worker targeted recovery framing indexes are invalid."
            )
        expected = {
            1: (2.0, 0.0),
            2: (3.0, 0.5),
        }.get(geometry["attempt"])
        if (
            expected is None
            or geometry["gap_index"] <= 0
            or geometry["window_index"] <= 0
            or not math.isclose(float(geometry["padding_seconds"]), expected[0])
            or not math.isclose(float(geometry["shift_seconds"]), expected[1])
            or not (
                float(geometry["slice_start"])
                <= float(geometry["core_start"])
                < float(geometry["core_end"])
                <= float(geometry["slice_end"])
            )
            or float(geometry["core_end"]) - float(geometry["core_start"])
            > 21.0 + 1.0e-6
            or float(geometry["input_duration"]) > 25.0 + 1.0e-6
            or not math.isclose(
                float(geometry["input_duration"]),
                float(geometry["slice_end"]) - float(geometry["slice_start"]),
                abs_tol=0.001,
            )
        ):
            raise CrisperWhisperProtocolError(
                "Worker targeted recovery framing geometry is inconsistent."
            )
    if len(geometries) > metadata["short_window_attempt_count"]:
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery framing count is inconsistent."
        )

    final_audit = _validate_coverage_audit(
        metadata["final_coverage_audit"],
        "targeted recovery final coverage audit",
    )
    if (
        final_audit.get("speech_active_gap_count")
        != metadata["unresolved_gap_count"]
        or not math.isclose(
            float(final_audit.get("speech_active_gap_duration")),
            float(metadata["unresolved_gap_duration"]),
            abs_tol=0.002,
        )
    ):
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery final audit is inconsistent."
        )
    warnings = metadata["warnings"]
    if (
        not isinstance(warnings, list)
        or any(warning not in _TARGETED_RECOVERY_WARNINGS for warning in warnings)
    ):
        raise CrisperWhisperProtocolError(
            "Worker targeted recovery warnings are invalid."
        )
    return copy.deepcopy(metadata)


def _validate_longform_coverage(
    value: Any,
    effective_settings: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if value is None:
        effective_longform = _expect_object(
            effective_settings.get("longform"), "settings.effective.longform"
        )
        if effective_longform.get("strategy") != "continuation":
            raise CrisperWhisperProtocolError(
                "Worker result used an unreported long-form recovery strategy."
            )
        return None
    coverage = _expect_object(value, "transcription.longform_coverage")
    attempted = coverage.get("attempted_strategies")
    selected = coverage.get("selected_strategy")
    requested = coverage.get("requested_strategy")
    fallback_used = coverage.get("fallback_used")
    if requested != "continuation" or attempted not in (
        ["continuation"],
        ["continuation", "chunked_lcs"],
    ):
        raise CrisperWhisperProtocolError(
            "Worker result long-form coverage strategies are invalid."
        )
    targeted_value = coverage.get("targeted_recovery")
    targeted = None
    if selected in _TARGETED_SELECTED_STRATEGIES:
        targeted = _validate_targeted_recovery_metadata(
            targeted_value,
            require_complete=True,
        )
        base_strategy = _TARGETED_SELECTED_STRATEGIES[selected]
        if targeted["base_strategy"] != base_strategy:
            raise CrisperWhisperProtocolError(
                "Worker result targeted recovery strategy is inconsistent."
            )
    else:
        base_strategy = selected
        if targeted_value is not None:
            raise CrisperWhisperProtocolError(
                "Worker result reports unused targeted recovery metadata."
            )
    if (
        base_strategy not in attempted
        or selected
        not in {
            "continuation",
            "chunked_lcs",
            *_TARGETED_SELECTED_STRATEGIES,
        }
    ):
        raise CrisperWhisperProtocolError(
            "Worker result selected an invalid long-form coverage strategy."
        )
    if not isinstance(fallback_used, bool) or fallback_used != (
        base_strategy == "chunked_lcs"
    ):
        raise CrisperWhisperProtocolError(
            "Worker result long-form coverage fallback state is inconsistent."
        )
    effective_longform = _expect_object(
        effective_settings.get("longform"), "settings.effective.longform"
    )
    if effective_longform.get("strategy") != selected:
        raise CrisperWhisperProtocolError(
            "Worker result effective long-form strategy differs from its coverage selection."
        )

    count_keys = (
        "substantial_gap_count",
        "speech_active_gap_count",
        "recovered_gap_count",
        "remaining_speech_active_gap_count",
    )
    duration_keys = (
        "substantial_gap_duration",
        "speech_active_gap_duration",
        "recovered_duration",
        "remaining_speech_active_gap_duration",
    )
    for key in count_keys:
        current = coverage.get(key)
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise CrisperWhisperProtocolError(
                "Worker result long-form coverage summary is invalid."
            )
    for key in duration_keys:
        current = coverage.get(key)
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(float(current))
            or float(current) < 0
        ):
            raise CrisperWhisperProtocolError(
                "Worker result long-form coverage summary is invalid."
            )
    if coverage["remaining_speech_active_gap_count"] != 0 or float(
        coverage["remaining_speech_active_gap_duration"]
    ) != 0.0:
        raise CrisperWhisperProtocolError(
            "Worker result still contains unrecovered speech-active coverage gaps."
        )
    targeted_used = targeted is not None
    if fallback_used or targeted_used:
        if (
            coverage["speech_active_gap_count"] <= 0
            or coverage["recovered_gap_count"] != coverage["speech_active_gap_count"]
            or float(coverage["recovered_duration"])
            != float(coverage["speech_active_gap_duration"])
        ):
            raise CrisperWhisperProtocolError(
                "Worker result long-form coverage recovery summary is inconsistent."
            )
    elif coverage["recovered_gap_count"] != 0 or float(coverage["recovered_duration"]) != 0.0:
        raise CrisperWhisperProtocolError(
            "Worker result reports coverage recovery without using its fallback."
        )

    configuration = _expect_object(
        coverage.get("configuration"), "transcription.longform_coverage.configuration"
    )
    if not configuration:
        raise CrisperWhisperProtocolError(
            "Worker result long-form coverage configuration is missing."
        )
    candidate_audits = _expect_object(
        coverage.get("candidate_audits"),
        "transcription.longform_coverage.candidate_audits",
    )
    if set(candidate_audits) != set(attempted):
        raise CrisperWhisperProtocolError(
            "Worker result long-form coverage candidate audits are incomplete."
        )
    for strategy in attempted:
        _validate_coverage_audit(candidate_audits[strategy], f"coverage audit {strategy}")
    if configuration != candidate_audits["continuation"].get("configuration"):
        raise CrisperWhisperProtocolError(
            "Worker result long-form coverage configuration is inconsistent."
        )
    if targeted is not None:
        base_audit = candidate_audits[targeted["base_strategy"]]
        if (
            targeted["target_gap_count"]
            != base_audit["speech_active_gap_count"]
            or not math.isclose(
                float(targeted["target_gap_duration"]),
                float(base_audit["speech_active_gap_duration"]),
                abs_tol=0.002,
            )
            or targeted["final_coverage_audit"]["speech_active_gap_count"]
            != coverage["remaining_speech_active_gap_count"]
            or not math.isclose(
                float(
                    targeted["final_coverage_audit"][
                        "speech_active_gap_duration"
                    ]
                ),
                float(coverage["remaining_speech_active_gap_duration"]),
                abs_tol=0.002,
            )
        ):
            raise CrisperWhisperProtocolError(
                "Worker result targeted recovery coverage is inconsistent."
            )
    warnings = coverage.get("warnings")
    if not isinstance(warnings, list) or any(
        not isinstance(warning, str) or not warning or len(warning) > 240
        for warning in warnings
    ):
        raise CrisperWhisperProtocolError(
            "Worker result long-form coverage warnings are invalid."
        )
    return copy.deepcopy(coverage)


def _validate_diagnostic_chunk_window(value: Any, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    window = _expect_object(value, label)
    if set(window) != {"chunk_index", "start", "end"}:
        raise CrisperWhisperProtocolError("Worker timestamp diagnostic chunk data is invalid.")
    chunk_index = window["chunk_index"]
    start = window["start"]
    end = window["end"]
    if (
        isinstance(chunk_index, bool)
        or not isinstance(chunk_index, int)
        or chunk_index < 0
        or isinstance(start, bool)
        or not isinstance(start, (int, float))
        or not math.isfinite(float(start))
        or float(start) < 0
        or isinstance(end, bool)
        or not isinstance(end, (int, float))
        or not math.isfinite(float(end))
        or float(end) < float(start)
    ):
        raise CrisperWhisperProtocolError("Worker timestamp diagnostic chunk data is invalid.")
    return copy.deepcopy(window)


def _validate_timestamp_overlap_diagnostic(value: Any) -> dict[str, Any]:
    details = _expect_object(value, "timestamp-overlap diagnostic")
    required = {
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
    if set(details) != required:
        raise CrisperWhisperProtocolError("Worker timestamp diagnostic fields are invalid.")
    if (
        details["diagnostic_type"] != "timestamp_overlap"
        or details["model_family"] not in OFFICIAL_MODEL_IDS
        or details["effective_strategy"]
        not in {"continuation", "chunked_lcs", "targeted_short_window"}
        or details["reason"] not in _TIMESTAMP_DIAGNOSTIC_REASONS
    ):
        raise CrisperWhisperProtocolError("Worker timestamp diagnostic identity is invalid.")
    for key in ("previous_word_index", "current_word_index", "repeated_sequence_length"):
        current = details[key]
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise CrisperWhisperProtocolError("Worker timestamp diagnostic indexes are invalid.")
    if details["current_word_index"] <= details["previous_word_index"]:
        raise CrisperWhisperProtocolError("Worker timestamp diagnostic word order is invalid.")
    for key in (
        "previous_start",
        "previous_end",
        "current_start",
        "current_end",
        "overlap_duration",
        "tolerance_seconds",
    ):
        current = details[key]
        if current is None and key in {"previous_start", "previous_end", "current_end"}:
            continue
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(float(current))
            or float(current) < 0
        ):
            raise CrisperWhisperProtocolError("Worker timestamp diagnostic times are invalid.")
    if float(details["overlap_duration"]) <= 0 or float(details["tolerance_seconds"]) != 0.25:
        raise CrisperWhisperProtocolError("Worker timestamp diagnostic overlap is invalid.")
    for key in (
        "crosses_native_chunk_boundary",
        "normalized_tokens_identical",
        "repeated_token_sequence",
        "timestamp_reset_relative_to_chunk_start",
        "previous_boundary_was_repaired",
        "current_boundary_was_repaired",
    ):
        if not isinstance(details[key], bool):
            raise CrisperWhisperProtocolError("Worker timestamp diagnostic flags are invalid.")
    if details["repeated_token_sequence"] != bool(details["repeated_sequence_length"]):
        raise CrisperWhisperProtocolError("Worker repeated-sequence diagnostic is inconsistent.")
    actions = details["repair_actions_attempted"]
    if (
        not isinstance(actions, list)
        or len(actions) != len(set(actions))
        or any(action not in _TIMESTAMP_REPAIR_ACTIONS for action in actions)
    ):
        raise CrisperWhisperProtocolError("Worker timestamp diagnostic repairs are invalid.")
    previous_window = _validate_diagnostic_chunk_window(
        details["previous_chunk_window"],
        "previous timestamp chunk",
    )
    current_window = _validate_diagnostic_chunk_window(
        details["current_chunk_window"],
        "current timestamp chunk",
    )
    previous_index = details["previous_native_chunk_index"]
    current_index = details["current_native_chunk_index"]
    for index in (previous_index, current_index):
        if index is not None and (
            isinstance(index, bool) or not isinstance(index, int) or index < 0
        ):
            raise CrisperWhisperProtocolError("Worker timestamp diagnostic chunk indexes are invalid.")
    if (
        (previous_window is None) != (previous_index is None)
        or (current_window is None) != (current_index is None)
        or previous_window is not None
        and previous_window["chunk_index"] != previous_index
        or current_window is not None
        and current_window["chunk_index"] != current_index
        or details["crosses_native_chunk_boundary"]
        != (
            previous_index is not None
            and current_index is not None
            and previous_index != current_index
        )
    ):
        raise CrisperWhisperProtocolError("Worker timestamp diagnostic chunks are inconsistent.")
    return copy.deepcopy(details)


def _validate_coverage_failure_diagnostic(value: Any) -> dict[str, Any]:
    details = _expect_object(value, "coverage-failure diagnostic")
    required = {
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
    allowed = required | {"fallback_diagnostic", "targeted_recovery"}
    if not required.issubset(details) or not set(details).issubset(allowed):
        raise CrisperWhisperProtocolError("Worker coverage diagnostic fields are invalid.")
    if (
        details["diagnostic_type"] != "coverage_failure"
        or details["model_family"] not in OFFICIAL_MODEL_IDS
        or details["requested_strategy"] != "continuation"
        or details["attempted_strategies"] != ["continuation", "chunked_lcs"]
        or details["fallback_rejection_reason"] not in _COVERAGE_REJECTION_REASONS
    ):
        raise CrisperWhisperProtocolError("Worker coverage diagnostic identity is invalid.")
    for key in ("recovered_gap_count", "remaining_speech_active_gap_count"):
        current = details[key]
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise CrisperWhisperProtocolError("Worker coverage diagnostic counts are invalid.")
    for key in ("recovered_duration", "remaining_speech_active_gap_duration"):
        current = details[key]
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(float(current))
            or float(current) < 0
        ):
            raise CrisperWhisperProtocolError("Worker coverage diagnostic durations are invalid.")
    candidate_audits = _expect_object(details["candidate_audits"], "coverage candidates")
    if "continuation" not in candidate_audits or not set(candidate_audits).issubset(
        {"continuation", "chunked_lcs"}
    ):
        raise CrisperWhisperProtocolError("Worker coverage diagnostic candidates are invalid.")
    required_audit_fields = {
        "substantial_gap_count",
        "substantial_gap_duration",
        "speech_active_gap_count",
        "speech_active_gap_duration",
        "silence_gap_count",
        "silence_gap_duration",
        "speech_active_gap_ranges",
        "active_frame_seconds",
        "known_diagnostic_interval",
        "configuration",
    }
    for strategy, audit in candidate_audits.items():
        if not isinstance(audit, dict) or set(audit) != required_audit_fields:
            raise CrisperWhisperProtocolError("Worker coverage diagnostic audit fields are invalid.")
        validated_audit = _validate_coverage_audit(audit, f"error coverage audit {strategy}")
        if (
            "silence_gap_count" not in validated_audit
            or "speech_active_gap_ranges" not in validated_audit
            or "known_diagnostic_interval" not in validated_audit
        ):
            raise CrisperWhisperProtocolError("Worker coverage diagnostic audit is incomplete.")
    fallback_audit = candidate_audits.get("chunked_lcs")
    targeted_value = details.get("targeted_recovery")
    targeted = None
    if targeted_value is not None:
        targeted = _validate_targeted_recovery_metadata(
            targeted_value,
            require_complete=False,
        )
    if details["fallback_rejection_reason"] == "targeted_recovery_incomplete":
        if targeted is None or targeted["unresolved_gap_count"] <= 0:
            raise CrisperWhisperProtocolError(
                "Worker coverage diagnostic targeted recovery is missing."
            )
        base_audit = candidate_audits.get(targeted["base_strategy"])
        if (
            base_audit is None
            or targeted["target_gap_count"]
            != base_audit["speech_active_gap_count"]
            or not math.isclose(
                float(targeted["target_gap_duration"]),
                float(base_audit["speech_active_gap_duration"]),
                abs_tol=0.002,
            )
            or details["remaining_speech_active_gap_count"]
            != targeted["unresolved_gap_count"]
            or not math.isclose(
                float(details["remaining_speech_active_gap_duration"]),
                float(targeted["unresolved_gap_duration"]),
                abs_tol=0.002,
            )
        ):
            raise CrisperWhisperProtocolError(
                "Worker coverage diagnostic targeted recovery is inconsistent."
            )
    elif targeted is not None:
        raise CrisperWhisperProtocolError(
            "Worker coverage diagnostic contains unexpected targeted recovery."
        )
    elif fallback_audit is not None:
        if (
            details["remaining_speech_active_gap_count"]
            != fallback_audit["speech_active_gap_count"]
            or not math.isclose(
                float(details["remaining_speech_active_gap_duration"]),
                float(fallback_audit["speech_active_gap_duration"]),
                abs_tol=0.002,
            )
        ):
            raise CrisperWhisperProtocolError("Worker coverage diagnostic remainder is inconsistent.")
    fallback_diagnostic = details.get("fallback_diagnostic")
    if fallback_diagnostic is not None:
        _validate_timestamp_overlap_diagnostic(fallback_diagnostic)
    return copy.deepcopy(details)


def validate_worker_error_details(value: Any) -> dict[str, Any]:
    details = _expect_object(value, "worker error details")
    diagnostic_type = details.get("diagnostic_type")
    if diagnostic_type == "timestamp_overlap":
        return _validate_timestamp_overlap_diagnostic(details)
    if diagnostic_type == "coverage_failure":
        return _validate_coverage_failure_diagnostic(details)
    raise CrisperWhisperProtocolError("Worker returned an unsupported diagnostic type.")


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
    duration = _finite_number(transcription.get("duration"), "duration")
    _finite_number(transcription.get("processing_time"), "processing time")
    _validate_word_timestamp_repairs(
        transcription.get("word_timestamp_repairs")
    )
    _validate_longform_coverage(
        transcription.get("longform_coverage"),
        effective,
    )

    words = transcription.get("words")
    if not isinstance(words, list):
        raise CrisperWhisperProtocolError("Worker result words must be an array.")
    previous_end = 0.0
    for word in words:
        item = _expect_object(word, "word")
        if not isinstance(item.get("word"), str) or not item["word"].strip():
            raise CrisperWhisperProtocolError("Worker result contains a word without speech text.")
        start = _finite_number(item.get("start"), "word start time")
        end = _finite_number(item.get("end"), "word end time")
        if end <= start or start < previous_end or end > duration:
            raise CrisperWhisperProtocolError("Worker result word timestamps are invalid or out of order.")
        previous_end = end
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
        "word_timestamp_repairs": copy.deepcopy(
            transcription.get("word_timestamp_repairs")
            or {
                "repair_count": 0,
                "categories": {},
                "words_remained_untimed": False,
                "untimed_word_count": 0,
                "warnings": [],
            }
        ),
        **(
            {
                "longform_coverage": copy.deepcopy(
                    transcription["longform_coverage"]
                )
            }
            if "longform_coverage" in transcription
            else {}
        ),
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
        diagnostic_details = None
        if isinstance(event, dict) and "details" in event:
            diagnostic_details = validate_worker_error_details(event["details"])
        if isinstance(event, dict) and event.get("code") == "cancelled":
            raise KeyboardInterrupt
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, str) or not message:
            message = f"CrisperWhisper worker {operation} failed with exit code {return_code}."
        raise CrisperWhisperRuntimeError(
            message,
            diagnostic_details=diagnostic_details,
        )

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
