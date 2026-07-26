"""Job types, their stages, and progress weights.

Weights sum to 1.0 per job type. When a stage is skipped its weight is
redistributed across the remaining stages, so the bar still reaches 100%.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    weight: float


# Diarization dominates: a 22-minute recording spends minutes there and
# milliseconds everywhere else.
INGEST_STAGES: tuple[Stage, ...] = (
    Stage("received", "Received", 0.02),
    Stage("probing", "Inspecting audio", 0.03),
    Stage("converting", "Converting audio", 0.10),
    Stage("diarizing", "Diarizing and transcribing", 0.65),
    Stage("persisting", "Saving transcript", 0.05),
    Stage("summarizing", "Summarizing", 0.14),
    Stage("done", "Done", 0.01),
)

DIARIZE_STAGES: tuple[Stage, ...] = (
    Stage("diarizing", "Diarizing and transcribing", 0.90),
    Stage("persisting", "Saving transcript", 0.09),
    Stage("done", "Done", 0.01),
)

SUMMARIZE_STAGES: tuple[Stage, ...] = (
    Stage("summarizing", "Summarizing", 0.98),
    Stage("done", "Done", 0.02),
)

MATCH_STAGES: tuple[Stage, ...] = (
    Stage("gathering", "Searching calendar and email", 0.55),
    Stage("ranking", "Ranking candidates", 0.43),
    Stage("done", "Done", 0.02),
)

STAGES_BY_TYPE: dict[str, tuple[Stage, ...]] = {
    "ingest": INGEST_STAGES,
    "diarize": DIARIZE_STAGES,
    "summarize": SUMMARIZE_STAGES,
    "match": MATCH_STAGES,
}

JOB_TYPES = tuple(STAGES_BY_TYPE)

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
RESUMABLE_STATUSES = {"queued", "interrupted"}


def stages_for(job_type: str) -> tuple[Stage, ...]:
    return STAGES_BY_TYPE.get(job_type, ())


def stage_labels(job_type: str) -> list[dict]:
    """Shape the SPA renders its stepper from."""
    return [{"key": s.key, "label": s.label, "weight": s.weight} for s in stages_for(job_type)]
