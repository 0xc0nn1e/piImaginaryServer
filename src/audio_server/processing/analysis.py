"""Optional transcript analysis providers."""

from __future__ import annotations

from collections.abc import Sequence

from audio_server.processing.contracts import (
    AnalysisResult,
    AnalysisStatus,
    MergedTranscriptSegment,
)


class DisabledAnalysisProvider:
    """Records an explicit skip without making transcription depend on an LLM."""

    @property
    def name(self) -> str:
        return "disabled"

    def analyze(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
    ) -> AnalysisResult:
        del recording_id, segments
        return AnalysisResult(
            status=AnalysisStatus.SKIPPED,
            provider=self.name,
        )
