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


@dataclass(frozen=True)
class FusionWord:
    text: str
    normalized: str
    start: Optional[float]
    end: Optional[float]
    speaker_id: Optional[str]
    speaker_name: Optional[str]
    provenance: tuple[FusionProvenance, ...]


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


@dataclass(frozen=True)
class FusionDecision:
    region_id: str
    action: str
    selected_candidate_ids: tuple[str, ...]
    decision_source: str
    sequence: int


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
        return self.automatically_resolved or self.decision is not None


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
            _provenance(word, revision, classification, decision_source),
        ),
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
                    ),
                    _provenance(
                        crisper_word,
                        crisper,
                        AGREEMENT,
                        AUTOMATIC_AGREEMENT,
                    ),
                ),
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
                    ),
                    _provenance(
                        crisper_word,
                        crisper,
                        TIMING_CONFLICT,
                        UNRESOLVED_CANDIDATE,
                    ),
                ),
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
        region.decision is not None and not region.automatically_resolved
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
    plan = FusionPlan(
        pair_identity=identity,
        regions=regions,
        summary=_summary(regions),
        decision_history=(),
        warnings=tuple(warnings),
        source_duration_bound=_source_duration_bound(comparison),
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
    else:
        selected_ids = _decision_candidates(region, action)
        decision = FusionDecision(
            region_id=region_id,
            action=action,
            selected_candidate_ids=selected_ids,
            decision_source=EXPLICIT_USER_DECISION,
            sequence=sequence,
        )
        updated_region = replace(region, decision=decision)
    history_entry = FusionDecision(
        region_id=region_id,
        action=action,
        selected_candidate_ids=selected_ids,
        decision_source=EXPLICIT_USER_DECISION,
        sequence=sequence,
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
    return updated


def reset_decision(plan: FusionPlan, region_id: str) -> FusionPlan:
    return apply_decision(plan, region_id, RESET_TO_UNRESOLVED)


def _with_decision_source(
    candidate: FusionCandidate,
    decision_source: str,
) -> FusionCandidate:
    words = tuple(
        replace(
            word,
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
                and previous.source_engine != candidate.source_engine
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
    "OMIT_BOTH",
    "OMIT_ENGINE_ONLY",
    "RESET_TO_UNRESOLVED",
    "UNRESOLVED_CANDIDATE",
    "USE_CRISPERWHISPER",
    "USE_CRISPERWHISPER_TIMING",
    "USE_WHISPERX",
    "USE_WHISPERX_TIMING",
    "apply_decision",
    "build_combined_preview",
    "create_fusion_plan",
    "reset_decision",
]
