"""Pure, read-only planning for conservative dual-engine transcript fusion.

This module transforms one exact :class:`result_comparison.ResultComparison`
snapshot into immutable planning objects.  It never writes result data, runs a
model, creates media resources, or claims that additional wording is correct.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Optional, Sequence

from result_comparison import (
    AGREEMENT,
    CRISPERWHISPER_ONLY,
    POSSIBLE_DUPLICATE,
    TEXT_CONFLICT,
    TIMING_CONFLICT,
    UNRESOLVED,
    WHISPERX_ONLY,
    ComparisonRegion,
    ResultComparison,
    RevisionMetadata,
    WordReference,
)


USE_WHISPERX = "use_whisperx"
USE_CRISPERWHISPER = "use_crisperwhisper"
USE_WHISPERX_TEXT = USE_WHISPERX
USE_CRISPERWHISPER_TEXT = USE_CRISPERWHISPER
USE_WHISPERX_TIMING = "use_whisperx_timing"
USE_CRISPERWHISPER_TIMING = "use_crisperwhisper_timing"
INCLUDE_ENGINE_ONLY = "include_engine_only_passage"
OMIT_ENGINE_ONLY = "omit_engine_only_passage"
KEEP_BOTH = "keep_both"
OMIT_BOTH = "omit_both"
RESET_TO_UNRESOLVED = "reset_to_unresolved"

AUTOMATIC_AGREEMENT = "automatic_agreement"
EXPLICIT_USER_DECISION = "explicit_user_decision"
UNRESOLVED_CANDIDATE = "unresolved_candidate"

_TIME_EPSILON_SECONDS = 0.001
_DERIVED_TIME_EPSILON_SECONDS = 1e-9
MINIMUM_DERIVED_WORD_DURATION_SECONDS = 0.020
AUTHORITATIVE_DURATION_TOLERANCE_SECONDS = 0.25


class FusionError(ValueError):
    """Base error for an invalid or unsafe fusion operation."""


class FusionIdentityError(FusionError):
    """Raised when a comparison does not retain a strict exact-pair identity."""


class FusionDecisionError(FusionError):
    """Raised when a decision is not valid for its comparison region."""


class FusionValidationError(FusionError):
    """Raised when selected evidence cannot form a safe ordered preview."""


class FusionNotReadyError(FusionValidationError):
    """Raised when a final preview is requested with unresolved decisions."""


@dataclass(frozen=True)
class FusionRevisionIdentity:
    engine: str
    result_id: str
    project_id: Optional[str]
    speakers_sha256: str
    segments_sha256: str
    comparison_job_id: Optional[str]
    status: str


@dataclass(frozen=True)
class FusionPairIdentity:
    comparison_pair_id: str
    project_id: Optional[str]
    source_identity: tuple[tuple[str, object], ...]
    whisperx_revision: FusionRevisionIdentity
    crisperwhisper_revision: FusionRevisionIdentity
    comparison_job_id: Optional[str]


@dataclass(frozen=True)
class FusionProvenance:
    source_engine: str
    revision_id: str
    speakers_sha256: str
    segments_sha256: str
    segment_index: int
    word_index: Optional[int]
    ordinal: int
    original_text: str
    original_start: Optional[float]
    original_end: Optional[float]
    original_speaker_id: Optional[str]
    original_speaker_name: Optional[str]
    comparison_classification: str
    decision_source: str
    source_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class FusionWord:
    text: str
    normalized: str
    start: Optional[float]
    end: Optional[float]
    speaker_id: Optional[str]
    speaker_name: Optional[str]
    provenance: tuple[FusionProvenance, ...]
    text_source_engine: Optional[str] = None
    timing_source_engine: Optional[str] = None
    speaker_source_engine: Optional[str] = None
    speaker_decision_source: Optional[str] = None
    timing_is_derived: bool = False
    timing_transformation: Optional[str] = None


@dataclass(frozen=True)
class FusionCandidate:
    candidate_id: str
    region_id: str
    role: str
    source_engine: str
    text: str
    start: Optional[float]
    end: Optional[float]
    words: tuple[FusionWord, ...]
    supporting_engines: tuple[str, ...]
    decision_source: str
    text_source_engine: Optional[str] = None
    timing_source_engine: Optional[str] = None
    speaker_source_engine: Optional[str] = None
    timing_is_derived: bool = False
    timing_transformation: Optional[str] = None


@dataclass(frozen=True)
class FusionDecision:
    region_id: str
    action: str
    selected_candidate_ids: tuple[str, ...]
    decision_source: str
    sequence: int
    text_source_engine: Optional[str] = None
    timing_source_engine: Optional[str] = None
    speaker_source_engine: Optional[str] = None
    text_decision_source: Optional[str] = None
    timing_decision_source: Optional[str] = None


@dataclass(frozen=True)
class FusionRegion:
    region_id: str
    classification: str
    candidates: tuple[FusionCandidate, ...]
    automatically_resolved: bool
    decision: Optional[FusionDecision]
    warning: bool

    @property
    def resolved(self) -> bool:
        if self.automatically_resolved:
            return True
        if self.decision is None:
            return False
        if self.classification == TEXT_CONFLICT:
            if self.decision.action in {KEEP_BOTH, OMIT_BOTH}:
                return True
            return bool(
                self.decision.text_source_engine
                and self.decision.timing_source_engine
            )
        if self.classification == TIMING_CONFLICT:
            return bool(self.decision.timing_source_engine)
        return True


@dataclass(frozen=True)
class FusionSummary:
    region_count: int
    automatically_resolved_region_count: int
    explicitly_resolved_region_count: int
    unresolved_region_count: int
    warning_region_count: int
    ready_for_final_generation: bool


@dataclass(frozen=True)
class FusionPlan:
    pair_identity: FusionPairIdentity
    regions: tuple[FusionRegion, ...]
    summary: FusionSummary
    decision_history: tuple[FusionDecision, ...]
    warnings: tuple[str, ...]
    source_duration_bound: Optional[float]
    authoritative_media_duration: Optional[float]
    authoritative_duration_source: Optional[str]
    known_transcript_bound: Optional[float]

    @property
    def ready_for_final_generation(self) -> bool:
        return self.summary.ready_for_final_generation


@dataclass(frozen=True)
class FusionPreview:
    pair_identity: FusionPairIdentity
    provisional: bool
    final_ready: bool
    candidates: tuple[FusionCandidate, ...]
    words: tuple[FusionWord, ...]
    text: str
    unresolved_region_ids: tuple[str, ...]
    warnings: tuple[str, ...]


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _pair_id(whisper: RevisionMetadata, crisper: RevisionMetadata) -> str:
    material = json.dumps(
        {
            "whisperx": [
                whisper.result_id,
                whisper.speakers_sha256,
                whisper.segments_sha256,
            ],
            "crisperwhisper": [
                crisper.result_id,
                crisper.speakers_sha256,
                crisper.segments_sha256,
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _revision_identity(revision: RevisionMetadata) -> FusionRevisionIdentity:
    return FusionRevisionIdentity(
        engine=revision.engine,
        result_id=revision.result_id,
        project_id=revision.project_id,
        speakers_sha256=revision.speakers_sha256,
        segments_sha256=revision.segments_sha256,
        comparison_job_id=revision.comparison_job_id,
        status=revision.status,
    )


def _validate_pair(comparison: ResultComparison) -> FusionPairIdentity:
    if not isinstance(comparison, ResultComparison):
        raise FusionIdentityError("Fusion requires an exact ResultComparison snapshot.")
    whisper = comparison.whisperx.revision
    crisper = comparison.crisperwhisper.revision
    if whisper.engine != "whisperx" or crisper.engine != "crisperwhisper":
        raise FusionIdentityError(
            "Fusion requires exact WhisperX and CrisperWhisper snapshots."
        )
    if not whisper.source_identity or whisper.source_identity != crisper.source_identity:
        raise FusionIdentityError("Fusion source identities do not match strictly.")
    if (
        whisper.project_id is not None
        and crisper.project_id is not None
        and whisper.project_id != crisper.project_id
    ):
        raise FusionIdentityError("Fusion revisions belong to different projects.")
    for revision in (whisper, crisper):
        if not revision.result_id:
            raise FusionIdentityError("Fusion revision IDs must not be empty.")
        if not _is_sha256(revision.speakers_sha256) or not _is_sha256(
            revision.segments_sha256
        ):
            raise FusionIdentityError("Fusion revision fingerprints are malformed.")
    expected_pair_id = _pair_id(whisper, crisper)
    if comparison.pair_id != expected_pair_id:
        raise FusionIdentityError("The comparison pair fingerprint is inconsistent.")
    region_ids = [region.region_id for region in comparison.regions]
    if len(region_ids) != len(set(region_ids)) or any(not value for value in region_ids):
        raise FusionIdentityError("Comparison region identities are not unique.")
    for region in comparison.regions:
        if any(word.engine != "whisperx" for word in region.whisperx_words):
            raise FusionIdentityError("A WhisperX region contains foreign provenance.")
        if any(
            word.engine != "crisperwhisper"
            for word in region.crisperwhisper_words
        ):
            raise FusionIdentityError(
                "A CrisperWhisper region contains foreign provenance."
            )
    shared_job_id = (
        whisper.comparison_job_id
        if whisper.comparison_job_id
        and whisper.comparison_job_id == crisper.comparison_job_id
        else None
    )
    project_id = (
        whisper.project_id
        if whisper.project_id == crisper.project_id
        else whisper.project_id or crisper.project_id
    )
    return FusionPairIdentity(
        comparison_pair_id=comparison.pair_id,
        project_id=project_id,
        source_identity=whisper.source_identity,
        whisperx_revision=_revision_identity(whisper),
        crisperwhisper_revision=_revision_identity(crisper),
        comparison_job_id=shared_job_id,
    )


def _provenance(
    word: WordReference,
    revision: RevisionMetadata,
    classification: str,
    decision_source: str,
    *,
    source_roles: Sequence[str] = (),
) -> FusionProvenance:
    return FusionProvenance(
        source_engine=word.engine,
        revision_id=revision.result_id,
        speakers_sha256=revision.speakers_sha256,
        segments_sha256=revision.segments_sha256,
        segment_index=word.segment_index,
        word_index=word.word_index,
        ordinal=word.ordinal,
        original_text=word.text,
        original_start=word.start,
        original_end=word.end,
        original_speaker_id=word.speaker_id,
        original_speaker_name=word.speaker_name,
        comparison_classification=classification,
        decision_source=decision_source,
        source_roles=tuple(source_roles),
    )


def _single_engine_word(
    word: WordReference,
    revision: RevisionMetadata,
    classification: str,
    decision_source: str,
) -> FusionWord:
    return FusionWord(
        text=word.text,
        normalized=word.normalized,
        start=word.start,
        end=word.end,
        speaker_id=word.speaker_id,
        speaker_name=word.speaker_name,
        provenance=(
            _provenance(
                word,
                revision,
                classification,
                decision_source,
                source_roles=("text", "timing", "speaker"),
            ),
        ),
        text_source_engine=word.engine,
        timing_source_engine=word.engine,
        speaker_source_engine=word.engine,
        speaker_decision_source=decision_source,
    )


def _candidate_from_engine(
    region: ComparisonRegion,
    engine: str,
    whisper: RevisionMetadata,
    crisper: RevisionMetadata,
) -> Optional[FusionCandidate]:
    if engine == "whisperx":
        references = region.whisperx_words
        revision = whisper
        text = region.whisperx_text
        start, end = region.whisperx_start, region.whisperx_end
    else:
        references = region.crisperwhisper_words
        revision = crisper
        text = region.crisperwhisper_text
        start, end = region.crisperwhisper_start, region.crisperwhisper_end
    if not references:
        return None
    words = tuple(
        _single_engine_word(
            word,
            revision,
            region.classification,
            UNRESOLVED_CANDIDATE,
        )
        for word in references
    )
    return FusionCandidate(
        candidate_id=f"{region.region_id}:{engine}",
        region_id=region.region_id,
        role="passage",
        source_engine=engine,
        text=text,
        start=start,
        end=end,
        words=words,
        supporting_engines=(engine,),
        decision_source=UNRESOLVED_CANDIDATE,
        text_source_engine=engine,
        timing_source_engine=engine,
        speaker_source_engine=engine,
    )


def _agreement_candidate(
    region: ComparisonRegion,
    whisper: RevisionMetadata,
    crisper: RevisionMetadata,
) -> FusionCandidate:
    if len(region.whisperx_words) != len(region.crisperwhisper_words):
        raise FusionIdentityError(
            f"Agreement region {region.region_id} has unequal word evidence."
        )
    words = []
    for whisper_word, crisper_word in zip(
        region.whisperx_words,
        region.crisperwhisper_words,
    ):
        if whisper_word.normalized != crisper_word.normalized:
            raise FusionIdentityError(
                f"Agreement region {region.region_id} has conflicting normalized text."
            )
        words.append(
            FusionWord(
                text=whisper_word.text,
                normalized=whisper_word.normalized,
                start=whisper_word.start,
                end=whisper_word.end,
                speaker_id=whisper_word.speaker_id,
                speaker_name=whisper_word.speaker_name,
                provenance=(
                    _provenance(
                        whisper_word,
                        whisper,
                        AGREEMENT,
                        AUTOMATIC_AGREEMENT,
                        source_roles=("text", "timing", "speaker"),
                    ),
                    _provenance(
                        crisper_word,
                        crisper,
                        AGREEMENT,
                        AUTOMATIC_AGREEMENT,
                        source_roles=("supporting",),
                    ),
                ),
                text_source_engine="whisperx",
                timing_source_engine="whisperx",
                speaker_source_engine="whisperx",
                speaker_decision_source=AUTOMATIC_AGREEMENT,
            )
        )
    return FusionCandidate(
        candidate_id=f"{region.region_id}:agreement",
        region_id=region.region_id,
        role="canonical_agreement",
        source_engine="whisperx",
        text=region.whisperx_text,
        start=region.whisperx_start,
        end=region.whisperx_end,
        words=tuple(words),
        supporting_engines=("whisperx", "crisperwhisper"),
        decision_source=AUTOMATIC_AGREEMENT,
        text_source_engine="whisperx",
        timing_source_engine="whisperx",
        speaker_source_engine="whisperx",
    )


def _timing_candidate(
    region: ComparisonRegion,
    timing_engine: str,
    whisper: RevisionMetadata,
    crisper: RevisionMetadata,
) -> FusionCandidate:
    if len(region.whisperx_words) != len(region.crisperwhisper_words):
        raise FusionIdentityError(
            f"Timing-conflict region {region.region_id} has unequal word evidence."
        )
    timing_words = (
        region.whisperx_words
        if timing_engine == "whisperx"
        else region.crisperwhisper_words
    )
    words = []
    for whisper_word, crisper_word, timing_word in zip(
        region.whisperx_words,
        region.crisperwhisper_words,
        timing_words,
    ):
        if whisper_word.normalized != crisper_word.normalized:
            raise FusionIdentityError(
                f"Timing-conflict region {region.region_id} has conflicting wording."
            )
        words.append(
            FusionWord(
                text=whisper_word.text,
                normalized=whisper_word.normalized,
                start=timing_word.start,
                end=timing_word.end,
                speaker_id=whisper_word.speaker_id,
                speaker_name=whisper_word.speaker_name,
                provenance=(
                    _provenance(
                        whisper_word,
                        whisper,
                        TIMING_CONFLICT,
                        UNRESOLVED_CANDIDATE,
                        source_roles=(
                            ("text", "timing", "speaker")
                            if timing_engine == "whisperx"
                            else ("text", "speaker")
                        ),
                    ),
                    _provenance(
                        crisper_word,
                        crisper,
                        TIMING_CONFLICT,
                        UNRESOLVED_CANDIDATE,
                        source_roles=(
                            ("timing",)
                            if timing_engine == "crisperwhisper"
                            else ("supporting",)
                        ),
                    ),
                ),
                text_source_engine="whisperx",
                timing_source_engine=timing_engine,
                speaker_source_engine="whisperx",
                speaker_decision_source=UNRESOLVED_CANDIDATE,
            )
        )
    start, end = (
        (region.whisperx_start, region.whisperx_end)
        if timing_engine == "whisperx"
        else (region.crisperwhisper_start, region.crisperwhisper_end)
    )
    return FusionCandidate(
        candidate_id=f"{region.region_id}:{timing_engine}-timing",
        region_id=region.region_id,
        role="timing",
        source_engine=timing_engine,
        text=region.whisperx_text,
        start=start,
        end=end,
        words=tuple(words),
        supporting_engines=("whisperx", "crisperwhisper"),
        decision_source=UNRESOLVED_CANDIDATE,
        text_source_engine="whisperx",
        timing_source_engine=timing_engine,
        speaker_source_engine="whisperx",
    )


def _fusion_region(
    region: ComparisonRegion,
    whisper: RevisionMetadata,
    crisper: RevisionMetadata,
) -> FusionRegion:
    if region.classification == AGREEMENT:
        candidates = (_agreement_candidate(region, whisper, crisper),)
        automatic = True
    elif region.classification == TIMING_CONFLICT:
        candidates = (
            _timing_candidate(region, "whisperx", whisper, crisper),
            _timing_candidate(region, "crisperwhisper", whisper, crisper),
        )
        automatic = False
    else:
        candidates = tuple(
            candidate
            for candidate in (
                _candidate_from_engine(region, "whisperx", whisper, crisper),
                _candidate_from_engine(
                    region,
                    "crisperwhisper",
                    whisper,
                    crisper,
                ),
            )
            if candidate is not None
        )
        automatic = False
    if not candidates and region.classification != UNRESOLVED:
        raise FusionIdentityError(
            f"Comparison region {region.region_id} has no candidate evidence."
        )
    return FusionRegion(
        region_id=region.region_id,
        classification=region.classification,
        candidates=candidates,
        automatically_resolved=automatic,
        decision=None,
        warning=region.classification in {POSSIBLE_DUPLICATE, UNRESOLVED},
    )


def _summary(regions: Sequence[FusionRegion]) -> FusionSummary:
    automatic = sum(region.automatically_resolved for region in regions)
    explicit = sum(
        region.resolved
        and region.decision is not None
        and not region.automatically_resolved
        for region in regions
    )
    unresolved = sum(not region.resolved for region in regions)
    return FusionSummary(
        region_count=len(regions),
        automatically_resolved_region_count=automatic,
        explicitly_resolved_region_count=explicit,
        unresolved_region_count=unresolved,
        warning_region_count=sum(region.warning for region in regions),
        ready_for_final_generation=unresolved == 0,
    )


def _source_duration_bound(comparison: ResultComparison) -> Optional[float]:
    values = []
    for snapshot in (comparison.whisperx, comparison.crisperwhisper):
        for word in snapshot.words:
            value = word.end if word.end is not None else word.segment_end
            if value is not None and math.isfinite(value):
                values.append(value)
    return max(values) if values else None


def _authoritative_media_duration(
    comparison: ResultComparison,
) -> tuple[Optional[float], Optional[str]]:
    reported = tuple(
        (
            snapshot.revision.engine,
            snapshot.revision.authoritative_duration,
            snapshot.revision.duration_source,
        )
        for snapshot in (comparison.whisperx, comparison.crisperwhisper)
        if snapshot.revision.authoritative_duration is not None
    )
    if len(reported) == 2:
        whisper = float(reported[0][1])
        crisper = float(reported[1][1])
        if abs(whisper - crisper) > AUTHORITATIVE_DURATION_TOLERANCE_SECONDS:
            raise FusionIdentityError(
                "The exact revisions report inconsistent authoritative media durations."
            )
        return min(whisper, crisper), "WhisperX and CrisperWhisper metadata"
    if len(reported) == 1:
        engine, duration, source = reported[0]
        return float(duration), f"{engine} {source or 'metadata'}"
    return None, None


def create_fusion_plan(comparison: ResultComparison) -> FusionPlan:
    """Create an immutable conservative plan from one exact comparison."""

    identity = _validate_pair(comparison)
    regions = tuple(
        _fusion_region(
            region,
            comparison.whisperx.revision,
            comparison.crisperwhisper.revision,
        )
        for region in comparison.regions
    )
    warnings = []
    crisper_revision = comparison.crisperwhisper.revision
    if crisper_revision.status != "complete":
        warnings.append(
            f"CrisperWhisper revision status is {crisper_revision.status.title()}."
        )
    if (
        crisper_revision.coverage is not None
        and not crisper_revision.coverage.coverage_complete
    ):
        warnings.append(
            "CrisperWhisper reports incomplete speech-active timeline coverage."
        )
    known_transcript_bound = _source_duration_bound(comparison)
    media_duration, duration_source = _authoritative_media_duration(comparison)
    plan = FusionPlan(
        pair_identity=identity,
        regions=regions,
        summary=_summary(regions),
        decision_history=(),
        warnings=tuple(warnings),
        source_duration_bound=media_duration or known_transcript_bound,
        authoritative_media_duration=media_duration,
        authoritative_duration_source=duration_source,
        known_transcript_bound=known_transcript_bound,
    )
    _validate_resolved_sequences(plan)
    return plan


def _candidate_by_engine(
    region: FusionRegion,
    engine: str,
    *,
    role: Optional[str] = None,
) -> Optional[FusionCandidate]:
    return next(
        (
            candidate
            for candidate in region.candidates
            if candidate.source_engine == engine
            and (role is None or candidate.role == role)
        ),
        None,
    )


def _retag_provenance(
    provenance: Sequence[FusionProvenance],
    roles: Sequence[str],
    decision_source: str,
) -> tuple[FusionProvenance, ...]:
    return tuple(
        replace(
            item,
            decision_source=decision_source,
            source_roles=tuple(roles),
        )
        for item in provenance
    )


def _derived_word_intervals(
    text_words: Sequence[FusionWord],
    start: Optional[float],
    end: Optional[float],
) -> tuple[tuple[float, float], ...]:
    if (
        start is None
        or end is None
        or not math.isfinite(float(start))
        or not math.isfinite(float(end))
        or float(end) <= float(start)
    ):
        raise FusionValidationError(
            "The selected timing source has no finite positive region to fit the wording."
        )
    if not text_words:
        raise FusionValidationError("The selected wording contains no source words.")
    region_start = float(start)
    region_end = float(end)
    span = region_end - region_start
    minimum_span = len(text_words) * MINIMUM_DERIVED_WORD_DURATION_SECONDS
    if span + _DERIVED_TIME_EPSILON_SECONDS < minimum_span:
        raise FusionValidationError(
            "The selected timing region is too short to give every selected word "
            f"at least {MINIMUM_DERIVED_WORD_DURATION_SECONDS * 1000:.0f} ms."
        )

    native_durations = []
    for word in text_words:
        if (
            word.start is None
            or word.end is None
            or not math.isfinite(float(word.start))
            or not math.isfinite(float(word.end))
            or float(word.end) <= float(word.start)
        ):
            native_durations = []
            break
        native_durations.append(float(word.end) - float(word.start))
    weights = native_durations or [1.0] * len(text_words)
    durations = [None] * len(weights)
    remaining_indexes = list(range(len(weights)))
    remaining_span = span
    while remaining_indexes:
        remaining_weight = sum(weights[index] for index in remaining_indexes)
        proportional = {
            index: remaining_span * weights[index] / remaining_weight
            for index in remaining_indexes
        }
        below_minimum = [
            index
            for index in remaining_indexes
            if proportional[index] + _DERIVED_TIME_EPSILON_SECONDS
            < MINIMUM_DERIVED_WORD_DURATION_SECONDS
        ]
        if not below_minimum:
            for index in remaining_indexes:
                durations[index] = proportional[index]
            break
        for index in below_minimum:
            durations[index] = MINIMUM_DERIVED_WORD_DURATION_SECONDS
        remaining_span -= (
            len(below_minimum) * MINIMUM_DERIVED_WORD_DURATION_SECONDS
        )
        remaining_indexes = [
            index for index in remaining_indexes if index not in below_minimum
        ]
    intervals = []
    cursor = region_start
    for index, duration in enumerate(durations):
        word_end = region_end if index == len(durations) - 1 else cursor + duration
        if word_end - cursor + _DERIVED_TIME_EPSILON_SECONDS < (
            MINIMUM_DERIVED_WORD_DURATION_SECONDS
        ):
            raise FusionValidationError(
                "The selected wording cannot be fitted into the timing region "
                "without a sub-20 ms word."
            )
        intervals.append((cursor, word_end))
        cursor = word_end
    return tuple(intervals)


def _mixed_text_timing_candidate(
    region: FusionRegion,
    text_engine: str,
    timing_engine: str,
    decision_source: str,
) -> FusionCandidate:
    text_candidate = _candidate_by_engine(region, text_engine, role="passage")
    timing_candidate = _candidate_by_engine(region, timing_engine, role="passage")
    if text_candidate is None:
        raise FusionDecisionError(
            f"The {text_engine} wording is absent from {region.region_id}."
        )
    if timing_candidate is None:
        raise FusionDecisionError(
            f"The {timing_engine} timing is absent from {region.region_id}."
        )
    if text_engine == timing_engine:
        return replace(
            _with_decision_source(text_candidate, decision_source),
            text_source_engine=text_engine,
            timing_source_engine=timing_engine,
            speaker_source_engine=text_engine,
        )

    timing_words = timing_candidate.words
    use_native_timing = bool(
        len(text_candidate.words) == len(timing_words)
        and timing_words
        and all(
            word.start is not None
            and word.end is not None
            and math.isfinite(float(word.start))
            and math.isfinite(float(word.end))
            and float(word.end) > float(word.start)
            for word in timing_words
        )
    )
    if use_native_timing:
        intervals = tuple((float(word.start), float(word.end)) for word in timing_words)
        transformation = None
    else:
        intervals = _derived_word_intervals(
            text_candidate.words,
            timing_candidate.start,
            timing_candidate.end,
        )
        transformation = "proportional_region_fit"

    timing_references = tuple(
        provenance
        for word in timing_words
        for provenance in word.provenance
        if provenance.source_engine == timing_engine
    )
    words = []
    for index, (text_word, interval) in enumerate(
        zip(text_candidate.words, intervals)
    ):
        text_provenance = tuple(
            provenance
            for provenance in text_word.provenance
            if provenance.source_engine == text_engine
        ) or text_word.provenance
        if use_native_timing:
            timing_provenance = tuple(
                provenance
                for provenance in timing_words[index].provenance
                if provenance.source_engine == timing_engine
            ) or timing_words[index].provenance
            timing_roles = ("timing",)
        else:
            timing_provenance = timing_references
            timing_roles = ("timing_region_reference",)
        words.append(
            replace(
                text_word,
                start=interval[0],
                end=interval[1],
                provenance=(
                    _retag_provenance(
                        text_provenance,
                        ("text", "speaker"),
                        decision_source,
                    )
                    + _retag_provenance(
                        timing_provenance,
                        timing_roles,
                        decision_source,
                    )
                ),
                text_source_engine=text_engine,
                timing_source_engine=timing_engine,
                speaker_source_engine=text_engine,
                speaker_decision_source=decision_source,
                timing_is_derived=not use_native_timing,
                timing_transformation=transformation,
            )
        )
    return FusionCandidate(
        candidate_id=(
            f"{region.region_id}:text-{text_engine}:timing-{timing_engine}"
        ),
        region_id=region.region_id,
        role="mixed_passage",
        source_engine=text_engine,
        text=text_candidate.text,
        start=timing_candidate.start,
        end=timing_candidate.end,
        words=tuple(words),
        supporting_engines=tuple(dict.fromkeys((text_engine, timing_engine))),
        decision_source=decision_source,
        text_source_engine=text_engine,
        timing_source_engine=timing_engine,
        speaker_source_engine=text_engine,
        timing_is_derived=not use_native_timing,
        timing_transformation=transformation,
    )


def _decision_candidates(region: FusionRegion, action: str) -> tuple[str, ...]:
    if region.classification == AGREEMENT:
        raise FusionDecisionError("Agreement regions are resolved automatically.")
    whisper = _candidate_by_engine(region, "whisperx")
    crisper = _candidate_by_engine(region, "crisperwhisper")

    if region.classification == TIMING_CONFLICT:
        mapping = {
            USE_WHISPERX_TIMING: (whisper,),
            USE_CRISPERWHISPER_TIMING: (crisper,),
        }
    elif region.classification == TEXT_CONFLICT:
        mapping = {
            USE_WHISPERX: (whisper,),
            USE_CRISPERWHISPER: (crisper,),
            USE_WHISPERX_TIMING: (whisper,),
            USE_CRISPERWHISPER_TIMING: (crisper,),
            KEEP_BOTH: (whisper, crisper),
            OMIT_BOTH: (),
        }
    elif region.classification in {WHISPERX_ONLY, CRISPERWHISPER_ONLY}:
        available = whisper or crisper
        mapping = {
            INCLUDE_ENGINE_ONLY: (available,),
            OMIT_ENGINE_ONLY: (),
        }
    elif region.classification in {POSSIBLE_DUPLICATE, UNRESOLVED}:
        mapping = {
            USE_WHISPERX: (whisper,),
            USE_CRISPERWHISPER: (crisper,),
            KEEP_BOTH: (whisper, crisper),
            OMIT_BOTH: (),
        }
    else:
        raise FusionDecisionError(
            f"Unsupported fusion classification: {region.classification}"
        )
    if action not in mapping:
        raise FusionDecisionError(
            f"Decision {action!r} is invalid for {region.classification}."
        )
    candidates = mapping[action]
    if any(candidate is None for candidate in candidates):
        raise FusionDecisionError(
            f"Decision {action!r} requests evidence absent from {region.region_id}."
        )
    return tuple(candidate.candidate_id for candidate in candidates if candidate)


def _decision_for_action(
    region: FusionRegion,
    action: str,
    sequence: int,
) -> FusionDecision:
    prior = region.decision
    if region.classification == TEXT_CONFLICT and action in {
        USE_WHISPERX,
        USE_CRISPERWHISPER,
        USE_WHISPERX_TIMING,
        USE_CRISPERWHISPER_TIMING,
    }:
        text_engine = prior.text_source_engine if prior else None
        timing_engine = prior.timing_source_engine if prior else None
        text_decision_source = prior.text_decision_source if prior else None
        timing_decision_source = prior.timing_decision_source if prior else None
        if action in {USE_WHISPERX, USE_CRISPERWHISPER}:
            text_engine = (
                "whisperx" if action == USE_WHISPERX else "crisperwhisper"
            )
            text_decision_source = EXPLICIT_USER_DECISION
            if timing_engine is None:
                timing_engine = "whisperx"
                timing_decision_source = "default_whisperx_timing"
        else:
            timing_engine = (
                "whisperx"
                if action == USE_WHISPERX_TIMING
                else "crisperwhisper"
            )
            timing_decision_source = EXPLICIT_USER_DECISION
        selected_ids = ()
        if text_engine and timing_engine:
            selected_ids = (
                f"{region.region_id}:text-{text_engine}:timing-{timing_engine}",
            )
        return FusionDecision(
            region_id=region.region_id,
            action=action,
            selected_candidate_ids=selected_ids,
            decision_source=EXPLICIT_USER_DECISION,
            sequence=sequence,
            text_source_engine=text_engine,
            timing_source_engine=timing_engine,
            speaker_source_engine=text_engine,
            text_decision_source=text_decision_source,
            timing_decision_source=timing_decision_source,
        )

    selected_ids = _decision_candidates(region, action)
    text_engine = None
    timing_engine = None
    speaker_engine = None
    if region.classification == TIMING_CONFLICT:
        text_engine = "whisperx"
        timing_engine = (
            "whisperx"
            if action == USE_WHISPERX_TIMING
            else "crisperwhisper"
        )
        speaker_engine = "whisperx"
    return FusionDecision(
        region_id=region.region_id,
        action=action,
        selected_candidate_ids=selected_ids,
        decision_source=EXPLICIT_USER_DECISION,
        sequence=sequence,
        text_source_engine=text_engine,
        timing_source_engine=timing_engine,
        speaker_source_engine=speaker_engine,
        text_decision_source=(
            EXPLICIT_USER_DECISION if text_engine is not None else None
        ),
        timing_decision_source=(
            EXPLICIT_USER_DECISION if timing_engine is not None else None
        ),
    )


def apply_decision(plan: FusionPlan, region_id: str, action: str) -> FusionPlan:
    """Return a new plan with one explicit decision applied or reset."""

    if not isinstance(plan, FusionPlan):
        raise FusionDecisionError("A FusionPlan is required.")
    region_index = next(
        (index for index, region in enumerate(plan.regions) if region.region_id == region_id),
        None,
    )
    if region_index is None:
        raise FusionDecisionError(f"Unknown fusion region: {region_id}")
    region = plan.regions[region_index]
    if region.classification == AGREEMENT:
        raise FusionDecisionError("Agreement regions cannot be changed explicitly.")
    sequence = len(plan.decision_history) + 1
    if action == RESET_TO_UNRESOLVED:
        selected_ids = ()
        updated_region = replace(region, decision=None)
        applied_decision = None
    else:
        applied_decision = _decision_for_action(region, action, sequence)
        selected_ids = applied_decision.selected_candidate_ids
        updated_region = replace(region, decision=applied_decision)
    history_entry = FusionDecision(
        region_id=region_id,
        action=action,
        selected_candidate_ids=selected_ids,
        decision_source=EXPLICIT_USER_DECISION,
        sequence=sequence,
        text_source_engine=(
            applied_decision.text_source_engine if applied_decision else None
        ),
        timing_source_engine=(
            applied_decision.timing_source_engine if applied_decision else None
        ),
        speaker_source_engine=(
            applied_decision.speaker_source_engine if applied_decision else None
        ),
        text_decision_source=(
            applied_decision.text_decision_source if applied_decision else None
        ),
        timing_decision_source=(
            applied_decision.timing_decision_source if applied_decision else None
        ),
    )
    regions = list(plan.regions)
    regions[region_index] = updated_region
    updated = replace(
        plan,
        regions=tuple(regions),
        summary=_summary(regions),
        decision_history=plan.decision_history + (history_entry,),
    )
    _validate_resolved_sequences(updated)
    if region.classification == TEXT_CONFLICT and action != RESET_TO_UNRESOLVED:
        build_combined_preview(updated, provisional=True)
    return updated


def reset_decision(plan: FusionPlan, region_id: str) -> FusionPlan:
    return apply_decision(plan, region_id, RESET_TO_UNRESOLVED)


def valid_decision_actions(
    plan: FusionPlan,
    region_id: str,
) -> tuple[str, ...]:
    """Return only actions that validate against the plan's current chronology."""

    region = next(
        (item for item in plan.regions if item.region_id == region_id),
        None,
    )
    if region is None:
        raise FusionDecisionError(f"Unknown fusion region: {region_id}")
    if region.classification == AGREEMENT:
        return ()
    if region.classification == TIMING_CONFLICT:
        candidates = (USE_WHISPERX_TIMING, USE_CRISPERWHISPER_TIMING)
    elif region.classification == TEXT_CONFLICT:
        candidates = (
            USE_WHISPERX,
            USE_CRISPERWHISPER,
            USE_WHISPERX_TIMING,
            USE_CRISPERWHISPER_TIMING,
            KEEP_BOTH,
            OMIT_BOTH,
        )
    elif region.classification in {WHISPERX_ONLY, CRISPERWHISPER_ONLY}:
        candidates = (INCLUDE_ENGINE_ONLY, OMIT_ENGINE_ONLY)
    else:
        candidates = (USE_WHISPERX, USE_CRISPERWHISPER, KEEP_BOTH, OMIT_BOTH)
    valid = []
    for action in candidates:
        try:
            apply_decision(plan, region_id, action)
        except FusionError:
            continue
        valid.append(action)
    return tuple(valid)


def _with_decision_source(
    candidate: FusionCandidate,
    decision_source: str,
) -> FusionCandidate:
    words = tuple(
        replace(
            word,
            speaker_decision_source=decision_source,
            provenance=tuple(
                replace(item, decision_source=decision_source)
                for item in word.provenance
            ),
        )
        for word in candidate.words
    )
    return replace(candidate, words=words, decision_source=decision_source)


def _selected_candidates(
    region: FusionRegion,
    *,
    provisional: bool,
) -> tuple[FusionCandidate, ...]:
    if region.automatically_resolved:
        return region.candidates
    if region.decision is not None:
        if (
            region.classification == TEXT_CONFLICT
            and region.decision.action not in {KEEP_BOTH, OMIT_BOTH}
        ):
            text_engine = region.decision.text_source_engine
            timing_engine = region.decision.timing_source_engine
            if not text_engine or not timing_engine:
                if not provisional:
                    raise FusionNotReadyError(
                        f"Fusion region {region.region_id} still requires a wording choice."
                    )
                text_engine = text_engine or "whisperx"
                timing_engine = timing_engine or "whisperx"
            return (
                _mixed_text_timing_candidate(
                    region,
                    text_engine,
                    timing_engine,
                    EXPLICIT_USER_DECISION,
                ),
            )
        by_id = {candidate.candidate_id: candidate for candidate in region.candidates}
        return tuple(
            _with_decision_source(
                by_id[candidate_id],
                EXPLICIT_USER_DECISION,
            )
            for candidate_id in region.decision.selected_candidate_ids
        )
    if provisional:
        whisper = _candidate_by_engine(region, "whisperx")
        return (whisper,) if whisper is not None else ()
    raise FusionNotReadyError(
        f"Fusion region {region.region_id} still requires an explicit decision."
    )


def _candidate_time(candidate: FusionCandidate, which: str) -> Optional[float]:
    value = candidate.start if which == "start" else candidate.end
    if value is not None:
        return value
    timed = [
        getattr(word, which)
        for word in candidate.words
        if getattr(word, which) is not None
    ]
    if not timed:
        return None
    return min(timed) if which == "start" else max(timed)


def _candidate_sequences_duplicate(
    first: FusionCandidate,
    second: FusionCandidate,
) -> bool:
    first_tokens = tuple(word.normalized for word in first.words)
    second_tokens = tuple(word.normalized for word in second.words)
    if not first_tokens or first_tokens != second_tokens:
        return False
    first_start = _candidate_time(first, "start")
    first_end = _candidate_time(first, "end")
    second_start = _candidate_time(second, "start")
    second_end = _candidate_time(second, "end")
    if None in (first_start, first_end, second_start, second_end):
        return first.region_id == second.region_id
    return max(first_start, second_start) <= min(first_end, second_end) + (
        _TIME_EPSILON_SECONDS
    )


def _validate_candidate(candidate: FusionCandidate, plan: FusionPlan) -> None:
    previous_start = None
    for word in candidate.words:
        for label, value in (("start", word.start), ("end", word.end)):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise FusionValidationError(
                    f"{candidate.candidate_id} contains a nonfinite {label} timestamp."
                )
        if word.start is not None and word.end is not None and word.end <= word.start:
            raise FusionValidationError(
                f"{candidate.candidate_id} contains a non-positive word duration."
            )
        if (
            word.timing_is_derived
            and word.start is not None
            and word.end is not None
            and word.end - word.start + _DERIVED_TIME_EPSILON_SECONDS
            < MINIMUM_DERIVED_WORD_DURATION_SECONDS
        ):
            raise FusionValidationError(
                f"{candidate.candidate_id} contains a derived word shorter than "
                f"{MINIMUM_DERIVED_WORD_DURATION_SECONDS * 1000:.0f} ms."
            )
        if (
            previous_start is not None
            and word.start is not None
            and word.start + _TIME_EPSILON_SECONDS < previous_start
        ):
            raise FusionValidationError(
                f"{candidate.candidate_id} contains nonmonotonic word timestamps."
            )
        if word.start is not None:
            previous_start = word.start
        if (
            plan.source_duration_bound is not None
            and word.end is not None
            and word.end > plan.source_duration_bound + _TIME_EPSILON_SECONDS
        ):
            raise FusionValidationError(
                f"{candidate.candidate_id} exceeds the known source timeline bound."
            )


def _validate_candidate_sequence(
    candidates: Sequence[FusionCandidate],
    plan: FusionPlan,
) -> None:
    previous = None
    previous_start = None
    previous_end = None
    for candidate in candidates:
        _validate_candidate(candidate, plan)
        current_start = _candidate_time(candidate, "start")
        current_end = _candidate_time(candidate, "end")
        if previous is not None:
            if _candidate_sequences_duplicate(previous, candidate):
                raise FusionValidationError(
                    "A fusion decision would insert overlapping duplicate wording."
                )
            if (
                previous_start is not None
                and current_start is not None
                and current_start + _TIME_EPSILON_SECONDS < previous_start
            ):
                raise FusionValidationError(
                    "A fusion decision would make the output chronology nonmonotonic."
                )
            if (
                previous_end is not None
                and current_start is not None
                and current_start + _TIME_EPSILON_SECONDS < previous_end
                and (previous.timing_source_engine or previous.source_engine)
                != (candidate.timing_source_engine or candidate.source_engine)
            ):
                raise FusionValidationError(
                    "A fusion decision would introduce an unsafe cross-engine overlap."
                )
        previous = candidate
        if current_start is not None:
            previous_start = current_start
        if current_end is not None:
            previous_end = current_end


def _validate_resolved_sequences(plan: FusionPlan) -> None:
    contiguous = []
    for region in plan.regions:
        if not region.resolved:
            _validate_candidate_sequence(contiguous, plan)
            contiguous = []
            continue
        contiguous.extend(_selected_candidates(region, provisional=False))
    _validate_candidate_sequence(contiguous, plan)


def build_combined_preview(
    plan: FusionPlan,
    *,
    provisional: bool = False,
) -> FusionPreview:
    """Build an ordered preview without writing or mutating either source result."""

    if not isinstance(plan, FusionPlan):
        raise FusionValidationError("A FusionPlan is required.")
    if not plan.ready_for_final_generation and not provisional:
        raise FusionNotReadyError(
            "The fusion plan still contains unresolved regions; request an explicit "
            "provisional preview to use the WhisperX baseline."
        )
    candidates = []
    unresolved_ids = []
    for region in plan.regions:
        if not region.resolved:
            unresolved_ids.append(region.region_id)
        candidates.extend(
            _selected_candidates(
                region,
                provisional=provisional,
            )
        )
    _validate_candidate_sequence(candidates, plan)
    words = tuple(word for candidate in candidates for word in candidate.words)
    return FusionPreview(
        pair_identity=plan.pair_identity,
        provisional=bool(provisional),
        final_ready=plan.ready_for_final_generation and not provisional,
        candidates=tuple(candidates),
        words=words,
        text="\n".join(candidate.text for candidate in candidates if candidate.text),
        unresolved_region_ids=tuple(unresolved_ids),
        warnings=plan.warnings,
    )


__all__ = [
    "AUTHORITATIVE_DURATION_TOLERANCE_SECONDS",
    "AUTOMATIC_AGREEMENT",
    "EXPLICIT_USER_DECISION",
    "FusionCandidate",
    "FusionDecision",
    "FusionDecisionError",
    "FusionError",
    "FusionIdentityError",
    "FusionNotReadyError",
    "FusionPairIdentity",
    "FusionPlan",
    "FusionPreview",
    "FusionProvenance",
    "FusionRegion",
    "FusionRevisionIdentity",
    "FusionSummary",
    "FusionValidationError",
    "FusionWord",
    "INCLUDE_ENGINE_ONLY",
    "KEEP_BOTH",
    "MINIMUM_DERIVED_WORD_DURATION_SECONDS",
    "OMIT_BOTH",
    "OMIT_ENGINE_ONLY",
    "RESET_TO_UNRESOLVED",
    "UNRESOLVED_CANDIDATE",
    "USE_CRISPERWHISPER",
    "USE_CRISPERWHISPER_TEXT",
    "USE_CRISPERWHISPER_TIMING",
    "USE_WHISPERX",
    "USE_WHISPERX_TEXT",
    "USE_WHISPERX_TIMING",
    "apply_decision",
    "build_combined_preview",
    "create_fusion_plan",
    "reset_decision",
    "valid_decision_actions",
]
