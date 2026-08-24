"""Read-only comparison of validated Transcript Studio result revisions.

The comparison model deliberately treats differences as measurements rather
than accuracy judgements.  It never writes result files, refreshes manifests,
starts a model, or creates a media player.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from result_catalog import (
    ResultCoverage,
    ResultDescriptor,
    ResultPreflight,
    revalidate_descriptor,
    source_identities_match,
)


TIMING_CONFLICT_THRESHOLD_SECONDS = 2.0
_DUPLICATE_MIN_WORDS = 2

AGREEMENT = "Agreement"
WHISPERX_ONLY = "WhisperX-only passage"
CRISPERWHISPER_ONLY = "CrisperWhisper-only passage"
TEXT_CONFLICT = "Text conflict"
TIMING_CONFLICT = "Timing conflict"
POSSIBLE_DUPLICATE = "Possible duplicate"
UNRESOLVED = "Unresolved"

REGION_CLASSIFICATIONS = (
    AGREEMENT,
    WHISPERX_ONLY,
    CRISPERWHISPER_ONLY,
    TEXT_CONFLICT,
    TIMING_CONFLICT,
    POSSIBLE_DUPLICATE,
    UNRESOLVED,
)

CLASSIFICATION_EXPLANATIONS = {
    AGREEMENT: (
        "The normalized word sequence agrees at a compatible point in both "
        "transcripts. Punctuation, capitalization, and cue boundaries are ignored."
    ),
    WHISPERX_ONLY: (
        "This wording appears only in the WhisperX result at this aligned location. "
        "That does not establish whether the wording is correct."
    ),
    CRISPERWHISPER_ONLY: (
        "This wording appears only in the CrisperWhisper result at this aligned "
        "location. That does not establish whether the wording is correct."
    ),
    TEXT_CONFLICT: (
        "Both results contain text in this aligned region, but their normalized "
        "wording differs. The comparison does not choose a correct version."
    ),
    TIMING_CONFLICT: (
        f"The normalized wording agrees, but corresponding timestamps differ by "
        f"more than {TIMING_CONFLICT_THRESHOLD_SECONDS:.1f} seconds."
    ),
    POSSIBLE_DUPLICATE: (
        "This unmatched wording also occurs elsewhere in the other transcript. "
        "Repeated text makes the intended occurrence uncertain."
    ),
    UNRESOLVED: (
        "The available text and timing evidence is not sufficient for a conservative "
        "classification."
    ),
}


class ResultComparisonError(ValueError):
    """Raised when two result revisions cannot be compared safely."""


@dataclass(frozen=True)
class RevisionMetadata:
    engine: str
    result_id: str
    project_id: Optional[str]
    display_title: str
    model: Optional[str]
    mode: Optional[str]
    execution_backend: Optional[str]
    status: str
    created_at: Optional[datetime]
    modified_at: Optional[datetime]
    comparison_job_id: Optional[str]
    coverage: Optional[ResultCoverage]
    source_identity: tuple[tuple[str, Any], ...]
    source_state: str
    speakers_json: Path
    segments_json: Path
    speakers_sha256: str
    segments_sha256: str


@dataclass(frozen=True)
class WordReference:
    engine: str
    ordinal: int
    segment_index: int
    word_index: Optional[int]
    text: str
    normalized: str
    start: Optional[float]
    end: Optional[float]
    segment_start: Optional[float]
    segment_end: Optional[float]
    segment_text: str
    speaker_id: Optional[str]
    speaker_name: Optional[str]

    @property
    def speaker_display(self) -> Optional[str]:
        if self.speaker_id and self.speaker_name:
            return f"{self.speaker_id} ({self.speaker_name})"
        return self.speaker_name or self.speaker_id


@dataclass(frozen=True)
class TranscriptSnapshot:
    revision: RevisionMetadata
    words: tuple[WordReference, ...]


@dataclass(frozen=True)
class ComparisonRegion:
    region_id: str
    classification: str
    whisperx_words: tuple[WordReference, ...]
    crisperwhisper_words: tuple[WordReference, ...]
    whisperx_text: str
    crisperwhisper_text: str
    whisperx_start: Optional[float]
    whisperx_end: Optional[float]
    crisperwhisper_start: Optional[float]
    crisperwhisper_end: Optional[float]
    start: Optional[float]
    end: Optional[float]
    whisperx_speakers: tuple[str, ...]
    crisperwhisper_speakers: tuple[str, ...]
    speaker_disagreement: bool
    maximum_timing_delta: Optional[float]
    explanation: str


@dataclass(frozen=True)
class ComparisonSummary:
    whisperx_word_count: int
    crisperwhisper_word_count: int
    matched_normalized_words: int
    whisperx_unique_words: int
    crisperwhisper_unique_words: int
    agreement_percentage: float
    discrepancy_region_count: int
    discrepancy_duration: float
    timing_conflict_count: int
    possible_duplicate_count: int
    unresolved_count: int


@dataclass(frozen=True)
class ResultComparison:
    pair_id: str
    whisperx: TranscriptSnapshot
    crisperwhisper: TranscriptSnapshot
    summary: ComparisonSummary
    regions: tuple[ComparisonRegion, ...]


def _revision_metadata(descriptor: ResultDescriptor) -> RevisionMetadata:
    return RevisionMetadata(
        engine=descriptor.engine,
        result_id=descriptor.result_id,
        project_id=descriptor.project_id,
        display_title=descriptor.display_title,
        model=descriptor.model,
        mode=descriptor.mode,
        execution_backend=descriptor.execution_backend,
        status=descriptor.status,
        created_at=descriptor.created_at,
        modified_at=descriptor.modified_at,
        comparison_job_id=descriptor.comparison_job_id,
        coverage=descriptor.coverage,
        source_identity=tuple(sorted(dict(descriptor.source_identity or {}).items())),
        source_state=descriptor.source_state,
        speakers_json=descriptor.speakers_json,
        segments_json=descriptor.segments_json,
        speakers_sha256=descriptor.speakers_sha256,
        segments_sha256=descriptor.segments_sha256,
    )


def strict_same_source(first: ResultDescriptor, second: ResultDescriptor) -> bool:
    """Return True only for opposite engines with the same strict source identity."""

    if {first.engine, second.engine} != {"whisperx", "crisperwhisper"}:
        return False
    if not source_identities_match(first.source_identity, second.source_identity):
        return False
    if (
        first.project_id is not None
        and second.project_id is not None
        and first.project_id != second.project_id
    ):
        return False
    return True


def compatible_counterparts(
    loaded: ResultDescriptor,
    descriptors: Iterable[ResultDescriptor],
) -> tuple[ResultDescriptor, ...]:
    compatible = [
        descriptor
        for descriptor in descriptors
        if descriptor.engine != loaded.engine and strict_same_source(loaded, descriptor)
    ]
    return tuple(sorted(compatible, key=_descriptor_order_key))


def _descriptor_time(descriptor: ResultDescriptor) -> Optional[datetime]:
    return descriptor.created_at or descriptor.modified_at


def _descriptor_order_key(descriptor: ResultDescriptor):
    value = _descriptor_time(descriptor)
    timestamp = value.timestamp() if value is not None else float("-inf")
    return (-timestamp, descriptor.result_id, str(descriptor.segments_json))


def choose_default_pair(
    loaded: ResultDescriptor,
    descriptors: Iterable[ResultDescriptor],
) -> tuple[ResultDescriptor, ResultDescriptor]:
    candidates = compatible_counterparts(loaded, descriptors)
    if not candidates:
        raise ResultComparisonError(
            "No compatible opposite-engine revision has the same verified source identity."
        )

    loaded_time = _descriptor_time(loaded)

    def counterpart_key(descriptor: ResultDescriptor):
        shared_group = bool(
            loaded.comparison_job_id
            and descriptor.comparison_job_id == loaded.comparison_job_id
        )
        candidate_time = _descriptor_time(descriptor)
        if loaded_time is not None and candidate_time is not None:
            distance = abs((candidate_time - loaded_time).total_seconds())
        else:
            distance = float("inf")
        return (0 if shared_group else 1, distance, _descriptor_order_key(descriptor))

    counterpart = min(candidates, key=counterpart_key)
    return (
        (loaded, counterpart)
        if loaded.engine == "whisperx"
        else (counterpart, loaded)
    )


def _normalize_word(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    if not value:
        return ""
    kept = []
    for index, character in enumerate(value):
        if character.isalnum():
            kept.append(character)
            continue
        if character.isspace():
            kept.append(" ")
            continue
        if character in {"'", "\u2019", "-"}:
            previous_is_word = index > 0 and value[index - 1].isalnum()
            next_is_word = index + 1 < len(value) and value[index + 1].isalnum()
            if previous_is_word and next_is_word:
                kept.append("'" if character == "\u2019" else character)
                continue
        if not unicodedata.category(character).startswith(("P", "S")):
            kept.append(character)
    return " ".join("".join(kept).split())


def _optional_time(value: Any, label: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultComparisonError(f"{label} must be a finite numeric value.")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ResultComparisonError(f"{label} must be a non-negative finite value.")
    return result


def _speaker_names(speakers_data: Mapping[str, Any]) -> dict[str, str]:
    raw = speakers_data.get("names")
    if not isinstance(raw, dict):
        raw = speakers_data.get("name_map")
    if not isinstance(raw, dict):
        return {}
    return {
        str(speaker): str(name).strip()
        for speaker, name in raw.items()
        if isinstance(speaker, str)
        and speaker.strip()
        and isinstance(name, str)
        and name.strip()
    }


def _extract_words(
    descriptor: ResultDescriptor,
    preflight: ResultPreflight,
) -> tuple[WordReference, ...]:
    names = _speaker_names(preflight.speakers_data)
    references: list[WordReference] = []
    for segment_index, segment in enumerate(preflight.segments_data["segments"]):
        segment_start = _optional_time(
            segment.get("start"), f"segment {segment_index + 1} start"
        )
        segment_end = _optional_time(
            segment.get("end"), f"segment {segment_index + 1} end"
        )
        if (
            segment_start is not None
            and segment_end is not None
            and segment_end < segment_start
        ):
            raise ResultComparisonError(
                f"segment {segment_index + 1} ends before it starts."
            )
        segment_text = str(segment.get("text", "") or "")
        segment_speaker = segment.get("speaker")
        raw_words = segment.get("words")
        extracted = []
        if isinstance(raw_words, list) and raw_words:
            for word_index, word in enumerate(raw_words):
                text = str(word.get("word", word.get("text", "")) or "")
                if not text.strip():
                    continue
                extracted.append((word_index, text, word))
        else:
            extracted.extend(
                (None, match.group(0), {})
                for match in re.finditer(r"\S+", segment_text, re.UNICODE)
            )

        for word_index, text, word in extracted:
            normalized = _normalize_word(text)
            if not normalized:
                continue
            start = _optional_time(
                word.get("start"),
                f"segment {segment_index + 1} word start",
            )
            end = _optional_time(
                word.get("end"),
                f"segment {segment_index + 1} word end",
            )
            if start is not None and end is not None and end < start:
                raise ResultComparisonError(
                    f"segment {segment_index + 1} contains a word ending before it starts."
                )
            speaker = word.get("speaker", segment_speaker)
            if speaker is not None:
                speaker = str(speaker)
            references.append(
                WordReference(
                    engine=descriptor.engine,
                    ordinal=len(references),
                    segment_index=segment_index,
                    word_index=word_index,
                    text=text,
                    normalized=normalized,
                    start=start,
                    end=end,
                    segment_start=segment_start,
                    segment_end=segment_end,
                    segment_text=segment_text,
                    speaker_id=speaker,
                    speaker_name=names.get(speaker) if speaker else None,
                )
            )
    return tuple(references)


def _longest_increasing_anchor_pairs(
    first: Sequence[str],
    second: Sequence[str],
) -> tuple[tuple[int, int], ...]:
    first_counts = Counter(first)
    second_counts = Counter(second)
    second_positions = {
        token: index
        for index, token in enumerate(second)
        if second_counts[token] == 1
    }
    candidates = [
        (index, second_positions[token])
        for index, token in enumerate(first)
        if first_counts[token] == 1 and token in second_positions
    ]
    if not candidates:
        return ()

    tails: list[int] = []
    tail_indices: list[int] = []
    previous = [-1] * len(candidates)
    from bisect import bisect_left

    for candidate_index, (_first_index, second_index) in enumerate(candidates):
        position = bisect_left(tails, second_index)
        if position == len(tails):
            tails.append(second_index)
            tail_indices.append(candidate_index)
        else:
            tails[position] = second_index
            tail_indices[position] = candidate_index
        if position:
            previous[candidate_index] = tail_indices[position - 1]

    selected = []
    cursor = tail_indices[-1]
    while cursor >= 0:
        selected.append(candidates[cursor])
        cursor = previous[cursor]
    selected.reverse()
    return tuple(selected)


def _slice_opcodes(
    first: Sequence[str],
    second: Sequence[str],
    first_offset: int,
    second_offset: int,
):
    matcher = difflib.SequenceMatcher(None, first, second, autojunk=False)
    return [
        (
            tag,
            first_offset + first_start,
            first_offset + first_end,
            second_offset + second_start,
            second_offset + second_end,
        )
        for tag, first_start, first_end, second_start, second_end in matcher.get_opcodes()
        if first_start != first_end or second_start != second_end
    ]


def _coalesce_opcodes(opcodes):
    merged = []
    for opcode in opcodes:
        if (
            merged
            and opcode[0] == merged[-1][0]
            and opcode[0] != "equal"
            and opcode[1] == merged[-1][2]
            and opcode[3] == merged[-1][4]
        ):
            previous = merged[-1]
            merged[-1] = (
                previous[0], previous[1], opcode[2], previous[3], opcode[4]
            )
        else:
            merged.append(opcode)
    return tuple(merged)


def _monotonic_opcodes(first: Sequence[str], second: Sequence[str]):
    anchors = _longest_increasing_anchor_pairs(first, second)
    if not anchors:
        return tuple(_slice_opcodes(first, second, 0, 0))
    opcodes = []
    first_cursor = second_cursor = 0
    for first_anchor, second_anchor in anchors:
        opcodes.extend(
            _slice_opcodes(
                first[first_cursor:first_anchor],
                second[second_cursor:second_anchor],
                first_cursor,
                second_cursor,
            )
        )
        opcodes.append(
            ("equal", first_anchor, first_anchor + 1, second_anchor, second_anchor + 1)
        )
        first_cursor = first_anchor + 1
        second_cursor = second_anchor + 1
    opcodes.extend(
        _slice_opcodes(
            first[first_cursor:],
            second[second_cursor:],
            first_cursor,
            second_cursor,
        )
    )
    return _coalesce_opcodes(opcodes)


def _word_time(word: WordReference) -> Optional[float]:
    return word.start if word.start is not None else word.segment_start


def _word_end(word: WordReference) -> Optional[float]:
    return word.end if word.end is not None else word.segment_end


def _timing_delta(first: WordReference, second: WordReference) -> Optional[float]:
    deltas = []
    for first_value, second_value in (
        (_word_time(first), _word_time(second)),
        (_word_end(first), _word_end(second)),
    ):
        if first_value is not None and second_value is not None:
            deltas.append(abs(first_value - second_value))
    return max(deltas) if deltas else None


def _direct_local_time_correspondence(
    whisper_words: Sequence[WordReference],
    crisper_words: Sequence[WordReference],
    whisper_start: int,
    whisper_end: int,
    crisper_start: int,
    crisper_end: int,
) -> bool:
    """Return true when the selected equal run has clear local timing evidence."""

    whisper_part = whisper_words[whisper_start:whisper_end]
    crisper_part = crisper_words[crisper_start:crisper_end]
    if not whisper_part or len(whisper_part) != len(crisper_part):
        return False
    if tuple(word.normalized for word in whisper_part) != tuple(
        word.normalized for word in crisper_part
    ):
        return False

    whisper_range = _word_bounds(whisper_part)
    crisper_range = _word_bounds(crisper_part)
    whisper_range_start, whisper_range_end = whisper_range
    crisper_range_start, crisper_range_end = crisper_range
    intervals_overlap = (
        whisper_range_start is not None
        and whisper_range_end is not None
        and crisper_range_start is not None
        and crisper_range_end is not None
        and max(whisper_range_start, crisper_range_start)
        <= min(whisper_range_end, crisper_range_end) + 0.001
    )
    boundary_deltas = [
        abs(whisper_value - crisper_value)
        for whisper_value, crisper_value in zip(whisper_range, crisper_range)
        if whisper_value is not None and crisper_value is not None
    ]
    boundaries_are_close = bool(boundary_deltas) and max(boundary_deltas) <= (
        TIMING_CONFLICT_THRESHOLD_SECONDS
    )
    return intervals_overlap or boundaries_are_close


def _repeated_match_is_ambiguous(
    whisper_words: Sequence[WordReference],
    crisper_words: Sequence[WordReference],
    whisper_start: int,
    whisper_end: int,
    crisper_start: int,
    crisper_end: int,
) -> bool:
    length = whisper_end - whisper_start
    if length < _DUPLICATE_MIN_WORDS or length != crisper_end - crisper_start:
        return False
    if _direct_local_time_correspondence(
        whisper_words,
        crisper_words,
        whisper_start,
        whisper_end,
        crisper_start,
        crisper_end,
    ):
        return False
    needle = tuple(
        word.normalized for word in whisper_words[whisper_start:whisper_end]
    )
    whisper_tokens = tuple(word.normalized for word in whisper_words)
    crisper_tokens = tuple(word.normalized for word in crisper_words)
    whisper_occurrences = _find_occurrences(whisper_tokens, needle)
    crisper_occurrences = _find_occurrences(crisper_tokens, needle)
    if len(whisper_occurrences) <= 1 and len(crisper_occurrences) <= 1:
        return False

    def occurrence_time(words, start):
        for word in words[start:start + length]:
            value = _word_time(word)
            if value is not None:
                return value
        return None

    candidates = []
    selected_delta = None
    for whisper_occurrence in whisper_occurrences:
        whisper_time = occurrence_time(whisper_words, whisper_occurrence)
        for crisper_occurrence in crisper_occurrences:
            crisper_time = occurrence_time(crisper_words, crisper_occurrence)
            if whisper_time is None or crisper_time is None:
                continue
            delta = abs(whisper_time - crisper_time)
            candidates.append(delta)
            if (
                whisper_occurrence == whisper_start
                and crisper_occurrence == crisper_start
            ):
                selected_delta = delta
    if selected_delta is None or not candidates:
        return True
    best = min(candidates)
    if selected_delta > best + 0.001:
        return True
    equally_plausible = sum(
        abs(candidate - best) <= TIMING_CONFLICT_THRESHOLD_SECONDS
        for candidate in candidates
    )
    return equally_plausible > 1


def _find_occurrences(haystack: Sequence[str], needle: Sequence[str]) -> tuple[int, ...]:
    if len(needle) < _DUPLICATE_MIN_WORDS or len(needle) > len(haystack):
        return ()
    target = tuple(needle)
    return tuple(
        index
        for index in range(len(haystack) - len(target) + 1)
        if tuple(haystack[index:index + len(target)]) == target
    )


def _longest_repeated_run(
    words: Sequence[WordReference],
    start: int,
    other_tokens: Sequence[str],
) -> int:
    best = 0
    for other_start, token in enumerate(other_tokens):
        if token != words[start].normalized:
            continue
        length = 0
        while (
            start + length < len(words)
            and other_start + length < len(other_tokens)
            and words[start + length].normalized == other_tokens[other_start + length]
        ):
            length += 1
        best = max(best, length)
    return best if best >= _DUPLICATE_MIN_WORDS else 0


def _partition_engine_only(
    words: Sequence[WordReference],
    other_tokens: Sequence[str],
    engine_only_classification: str,
    *,
    whisper_side: bool,
) -> list[ComparisonRegion]:
    regions = []
    ordinary = []

    def flush_ordinary():
        if ordinary:
            regions.append(
                _make_region(
                    engine_only_classification,
                    tuple(ordinary) if whisper_side else (),
                    () if whisper_side else tuple(ordinary),
                )
            )
            ordinary.clear()

    index = 0
    while index < len(words):
        repeated_length = _longest_repeated_run(words, index, other_tokens)
        if repeated_length:
            flush_ordinary()
            repeated_words = tuple(words[index:index + repeated_length])
            regions.append(
                _make_region(
                    POSSIBLE_DUPLICATE,
                    repeated_words if whisper_side else (),
                    () if whisper_side else repeated_words,
                )
            )
            index += repeated_length
        else:
            ordinary.append(words[index])
            index += 1
    flush_ordinary()
    return regions


def _join_original(words: Sequence[WordReference]) -> str:
    result = ""
    closing_punctuation = set(",.!?;:%)]}\u2019'\"")
    for word in words:
        value = word.text
        if not value:
            continue
        if not result:
            result = value.strip()
        elif value[:1].isspace() or value[:1] in closing_punctuation:
            result += value
        else:
            result += " " + value
    return result.strip()


def _speaker_values(words: Sequence[WordReference]) -> tuple[str, ...]:
    values = []
    for word in words:
        value = word.speaker_display
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _word_bounds(
    words: Sequence[WordReference],
) -> tuple[Optional[float], Optional[float]]:
    starts = [
        value
        for word in words
        for value in (_word_time(word),)
        if value is not None
    ]
    ends = [
        value
        for word in words
        for value in (_word_end(word),)
        if value is not None
    ]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _region_bounds(
    whisper_words: Sequence[WordReference],
    crisper_words: Sequence[WordReference],
) -> tuple[Optional[float], Optional[float]]:
    whisper_start, whisper_end = _word_bounds(whisper_words)
    crisper_start, crisper_end = _word_bounds(crisper_words)
    starts = [value for value in (whisper_start, crisper_start) if value is not None]
    ends = [value for value in (whisper_end, crisper_end) if value is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _make_region(
    classification: str,
    whisper_words: Sequence[WordReference],
    crisper_words: Sequence[WordReference],
    *,
    maximum_timing_delta: Optional[float] = None,
) -> ComparisonRegion:
    whisper_tuple = tuple(whisper_words)
    crisper_tuple = tuple(crisper_words)
    whisper_speakers = _speaker_values(whisper_tuple)
    crisper_speakers = _speaker_values(crisper_tuple)
    whisper_start, whisper_end = _word_bounds(whisper_tuple)
    crisper_start, crisper_end = _word_bounds(crisper_tuple)
    start, end = _region_bounds(whisper_tuple, crisper_tuple)
    explanation = CLASSIFICATION_EXPLANATIONS[classification]
    speaker_disagreement = bool(
        whisper_speakers
        and crisper_speakers
        and whisper_speakers != crisper_speakers
    )
    if speaker_disagreement:
        explanation += " Corresponding speaker labels differ between the results."
    return ComparisonRegion(
        region_id="",
        classification=classification,
        whisperx_words=whisper_tuple,
        crisperwhisper_words=crisper_tuple,
        whisperx_text=_join_original(whisper_tuple),
        crisperwhisper_text=_join_original(crisper_tuple),
        whisperx_start=whisper_start,
        whisperx_end=whisper_end,
        crisperwhisper_start=crisper_start,
        crisperwhisper_end=crisper_end,
        start=start,
        end=end,
        whisperx_speakers=whisper_speakers,
        crisperwhisper_speakers=crisper_speakers,
        speaker_disagreement=speaker_disagreement,
        maximum_timing_delta=maximum_timing_delta,
        explanation=explanation,
    )


def _merge_adjacent_regions(regions: Sequence[ComparisonRegion]) -> tuple[ComparisonRegion, ...]:
    merged: list[ComparisonRegion] = []
    for region in regions:
        if merged and merged[-1].classification == region.classification:
            previous = merged.pop()
            deltas = [
                value
                for value in (
                    previous.maximum_timing_delta,
                    region.maximum_timing_delta,
                )
                if value is not None
            ]
            merged.append(
                _make_region(
                    region.classification,
                    previous.whisperx_words + region.whisperx_words,
                    previous.crisperwhisper_words + region.crisperwhisper_words,
                    maximum_timing_delta=max(deltas) if deltas else None,
                )
            )
        else:
            merged.append(region)
    return tuple(
        ComparisonRegion(
            region_id=f"region-{index:04d}",
            classification=region.classification,
            whisperx_words=region.whisperx_words,
            crisperwhisper_words=region.crisperwhisper_words,
            whisperx_text=region.whisperx_text,
            crisperwhisper_text=region.crisperwhisper_text,
            whisperx_start=region.whisperx_start,
            whisperx_end=region.whisperx_end,
            crisperwhisper_start=region.crisperwhisper_start,
            crisperwhisper_end=region.crisperwhisper_end,
            start=region.start,
            end=region.end,
            whisperx_speakers=region.whisperx_speakers,
            crisperwhisper_speakers=region.crisperwhisper_speakers,
            speaker_disagreement=region.speaker_disagreement,
            maximum_timing_delta=region.maximum_timing_delta,
            explanation=region.explanation,
        )
        for index, region in enumerate(merged, 1)
    )


def _comparison_regions(
    whisper_words: tuple[WordReference, ...],
    crisper_words: tuple[WordReference, ...],
) -> tuple[ComparisonRegion, ...]:
    whisper_tokens = tuple(word.normalized for word in whisper_words)
    crisper_tokens = tuple(word.normalized for word in crisper_words)
    opcodes = _monotonic_opcodes(whisper_tokens, crisper_tokens)
    regions = []
    for tag, whisper_start, whisper_end, crisper_start, crisper_end in opcodes:
        whisper_part = whisper_words[whisper_start:whisper_end]
        crisper_part = crisper_words[crisper_start:crisper_end]
        if tag == "equal":
            if _repeated_match_is_ambiguous(
                whisper_words,
                crisper_words,
                whisper_start,
                whisper_end,
                crisper_start,
                crisper_end,
            ):
                regions.append(
                    _make_region(
                        POSSIBLE_DUPLICATE,
                        whisper_part,
                        crisper_part,
                    )
                )
                continue
            run_classification = None
            run_whisper = []
            run_crisper = []
            run_deltas = []

            def flush_equal_run():
                nonlocal run_classification, run_whisper, run_crisper, run_deltas
                if run_classification is not None:
                    regions.append(
                        _make_region(
                            run_classification,
                            run_whisper,
                            run_crisper,
                            maximum_timing_delta=max(run_deltas) if run_deltas else None,
                        )
                    )
                run_classification = None
                run_whisper = []
                run_crisper = []
                run_deltas = []

            for whisper_word, crisper_word in zip(whisper_part, crisper_part):
                delta = _timing_delta(whisper_word, crisper_word)
                classification = (
                    TIMING_CONFLICT
                    if delta is not None
                    and delta > TIMING_CONFLICT_THRESHOLD_SECONDS
                    else AGREEMENT
                )
                if run_classification != classification:
                    flush_equal_run()
                    run_classification = classification
                run_whisper.append(whisper_word)
                run_crisper.append(crisper_word)
                if delta is not None:
                    run_deltas.append(delta)
            flush_equal_run()
            continue

        whisper_normalized = tuple(word.normalized for word in whisper_part)
        crisper_normalized = tuple(word.normalized for word in crisper_part)
        repeated = bool(
            _find_occurrences(crisper_tokens, whisper_normalized)
            or _find_occurrences(whisper_tokens, crisper_normalized)
        )
        if repeated:
            classification = POSSIBLE_DUPLICATE
        elif whisper_part and not crisper_part:
            regions.extend(
                _partition_engine_only(
                    whisper_part,
                    crisper_tokens,
                    WHISPERX_ONLY,
                    whisper_side=True,
                )
            )
            continue
        elif crisper_part and not whisper_part:
            regions.extend(
                _partition_engine_only(
                    crisper_part,
                    whisper_tokens,
                    CRISPERWHISPER_ONLY,
                    whisper_side=False,
                )
            )
            continue
        elif whisper_part and crisper_part:
            classification = TEXT_CONFLICT
        else:
            classification = UNRESOLVED
        regions.append(_make_region(classification, whisper_part, crisper_part))
    return _merge_adjacent_regions(regions)


def _snapshot(descriptor: ResultDescriptor, preflight: ResultPreflight) -> TranscriptSnapshot:
    return TranscriptSnapshot(
        revision=_revision_metadata(descriptor),
        words=_extract_words(descriptor, preflight),
    )


def _summary(
    whisper: TranscriptSnapshot,
    crisper: TranscriptSnapshot,
    regions: Sequence[ComparisonRegion],
) -> ComparisonSummary:
    matched = sum(
        min(len(region.whisperx_words), len(region.crisperwhisper_words))
        for region in regions
        if region.classification in {AGREEMENT, TIMING_CONFLICT}
    )
    whisper_unique = sum(
        len(region.whisperx_words)
        for region in regions
        if region.classification not in {AGREEMENT, TIMING_CONFLICT}
    )
    crisper_unique = sum(
        len(region.crisperwhisper_words)
        for region in regions
        if region.classification not in {AGREEMENT, TIMING_CONFLICT}
    )
    total = len(whisper.words) + len(crisper.words)
    agreement_percentage = (200.0 * matched / total) if total else 100.0
    discrepancies = [region for region in regions if region.classification != AGREEMENT]
    discrepancy_duration = sum(
        max(0.0, region.end - region.start)
        for region in discrepancies
        if region.start is not None and region.end is not None
    )
    return ComparisonSummary(
        whisperx_word_count=len(whisper.words),
        crisperwhisper_word_count=len(crisper.words),
        matched_normalized_words=matched,
        whisperx_unique_words=whisper_unique,
        crisperwhisper_unique_words=crisper_unique,
        agreement_percentage=agreement_percentage,
        discrepancy_region_count=len(discrepancies),
        discrepancy_duration=discrepancy_duration,
        timing_conflict_count=sum(
            region.classification == TIMING_CONFLICT for region in regions
        ),
        possible_duplicate_count=sum(
            region.classification == POSSIBLE_DUPLICATE for region in regions
        ),
        unresolved_count=sum(
            region.classification == UNRESOLVED for region in regions
        ),
    )


def compare_results(
    first: ResultDescriptor,
    second: ResultDescriptor,
) -> ResultComparison:
    """Revalidate and compare exactly one WhisperX and one CrisperWhisper result."""

    if not isinstance(first, ResultDescriptor) or not isinstance(second, ResultDescriptor):
        raise ResultComparisonError("Comparison inputs must be ResultDescriptor instances.")
    try:
        validated_first = revalidate_descriptor(first)
        validated_second = revalidate_descriptor(second)
    except Exception as exc:
        raise ResultComparisonError(f"A selected result is no longer valid: {exc}") from exc
    if validated_first.file_identity != first.file_identity:
        raise ResultComparisonError(
            "The first selected result changed after it was selected."
        )
    if validated_second.file_identity != second.file_identity:
        raise ResultComparisonError(
            "The second selected result changed after it was selected."
        )
    if not strict_same_source(validated_first, validated_second):
        raise ResultComparisonError(
            "Results must be opposite-engine revisions of the same strict source identity."
        )
    whisper_descriptor = (
        validated_first if validated_first.engine == "whisperx" else validated_second
    )
    crisper_descriptor = (
        validated_first
        if validated_first.engine == "crisperwhisper"
        else validated_second
    )

    # revalidate_descriptor already performed the strict catalog preflight and
    # fingerprint checks.  Re-read through the validated paths only for the
    # immutable content snapshot used by this comparison.
    from result_catalog import preflight_result_pair

    whisper_preflight = preflight_result_pair(*whisper_descriptor.paths)
    crisper_preflight = preflight_result_pair(*crisper_descriptor.paths)
    if whisper_preflight.identity != whisper_descriptor.file_identity:
        raise ResultComparisonError("WhisperX result changed during comparison setup.")
    if crisper_preflight.identity != crisper_descriptor.file_identity:
        raise ResultComparisonError("CrisperWhisper result changed during comparison setup.")

    whisper = _snapshot(whisper_descriptor, whisper_preflight)
    crisper = _snapshot(crisper_descriptor, crisper_preflight)
    regions = _comparison_regions(whisper.words, crisper.words)
    pair_material = json.dumps(
        {
            "whisperx": [
                whisper_descriptor.result_id,
                whisper_descriptor.speakers_sha256,
                whisper_descriptor.segments_sha256,
            ],
            "crisperwhisper": [
                crisper_descriptor.result_id,
                crisper_descriptor.speakers_sha256,
                crisper_descriptor.segments_sha256,
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ResultComparison(
        pair_id=hashlib.sha256(pair_material).hexdigest(),
        whisperx=whisper,
        crisperwhisper=crisper,
        summary=_summary(whisper, crisper, regions),
        regions=regions,
    )


__all__ = [
    "AGREEMENT",
    "CLASSIFICATION_EXPLANATIONS",
    "CRISPERWHISPER_ONLY",
    "ComparisonRegion",
    "ComparisonSummary",
    "POSSIBLE_DUPLICATE",
    "REGION_CLASSIFICATIONS",
    "ResultComparison",
    "ResultComparisonError",
    "RevisionMetadata",
    "TEXT_CONFLICT",
    "TIMING_CONFLICT",
    "TIMING_CONFLICT_THRESHOLD_SECONDS",
    "TranscriptSnapshot",
    "UNRESOLVED",
    "WHISPERX_ONLY",
    "WordReference",
    "choose_default_pair",
    "compare_results",
    "compatible_counterparts",
    "strict_same_source",
]
