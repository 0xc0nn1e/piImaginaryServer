"""Translations must survive a transcript edit and never overwrite human work."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import Select, select
from sqlalchemy.orm import Session, sessionmaker

from audio_server.db.models import (
    JobKind,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
    TranscriptSegment,
    TranscriptTranslation,
    TranslationSource,
)
from audio_server.processing.contracts import (
    AnalysisResult,
    AnalysisStatus,
    AudioProbe,
    MergedTranscriptSegment,
    PipelineResult,
    SentenceTranslation,
    TranslationResult,
)
from audio_server.worker_runtime import _result_persister, _write_translations
from tests.conftest import TEST_API_TOKEN

BEARER = {"Authorization": f"Bearer {TEST_API_TOKEN}"}


def select_translations(recording_id: uuid.UUID) -> Select[tuple[TranscriptTranslation]]:
    return (
        select(TranscriptTranslation)
        .where(TranscriptTranslation.recording_id == recording_id)
        .order_by(TranscriptTranslation.created_at, TranscriptTranslation.id)
    )



def _seed(
    session_factory: sessionmaker[Session], texts: list[str]
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    recording_id = uuid.uuid4()
    job_id = uuid.uuid4()
    started = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    segment_ids = [uuid.uuid4() for _ in texts]
    with session_factory.begin() as session:
        session.add(
            Recording(
                id=recording_id,
                device_id="pi-recorder-01",
                original_filename="meeting.wav",
                storage_key=f"recordings/{recording_id}/original.wav",
                mime_type="audio/wav",
                audio_format="wav",
                file_size=1024,
                sha256=recording_id.hex * 2,
                started_at=started,
                ended_at=started + timedelta(seconds=60),
                duration_seconds=60.0,
                sample_rate=16_000,
                channels=1,
                processing_status=RecordingStatus.COMPLETED,
            )
        )
        session.add(
            ProcessingJob(
                id=job_id,
                recording_id=recording_id,
                kind=JobKind.FULL,
                status=JobStatus.COMPLETED,
                available_at=started,
            )
        )
        for index, (segment_id, text) in enumerate(zip(segment_ids, texts, strict=True)):
            session.add(
                TranscriptSegment(
                    id=segment_id,
                    recording_id=recording_id,
                    job_id=job_id,
                    sequence=index,
                    speaker_label="SPEAKER_00",
                    start_time=float(index),
                    end_time=float(index) + 0.9,
                    text=text,
                )
            )
    return recording_id, segment_ids


def _result(*spans: tuple[int, int, str, str]) -> TranslationResult:
    return TranslationResult(
        status=AnalysisStatus.COMPLETED,
        provider="lmstudio",
        translations=tuple(
            SentenceTranslation(
                start_sequence=start, end_sequence=end, source_ja=source, text_zh_hk=text
            )
            for start, end, source, text in spans
        ),
    )


def test_translations_are_stored_against_segment_ids(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["昨日は", "行きました。"])

    with session_factory.begin() as session:
        _write_translations(
            session, recording_id, _result((0, 1, "昨日は行きました。", "尋日去咗。"))
        )

    with session_factory() as session:
        rows = list(session.scalars(select_translations(recording_id)))
    assert len(rows) == 1
    assert rows[0].start_segment_id == segment_ids[0]
    assert rows[0].end_segment_id == segment_ids[1]
    assert rows[0].source is TranslationSource.LLM


def test_a_failed_run_leaves_the_previous_translation_alone(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, _ = _seed(session_factory, ["行きました。"])
    with session_factory.begin() as session:
        _write_translations(session, recording_id, _result((0, 0, "行きました。", "去咗。")))

    with session_factory.begin() as session:
        _write_translations(
            session,
            recording_id,
            TranslationResult(status=AnalysisStatus.FAILED, provider="lmstudio"),
        )

    with session_factory() as session:
        rows = list(session.scalars(select_translations(recording_id)))
    assert [row.text_zh_hk for row in rows] == ["去咗。"]


def test_a_machine_run_never_overwrites_a_hand_written_translation(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["行きました。"])
    with session_factory.begin() as session:
        session.add(
            TranscriptTranslation(
                recording_id=recording_id,
                start_segment_id=segment_ids[0],
                end_segment_id=segment_ids[0],
                source_ja="行きました。",
                text_zh_hk="人手譯文",
                source=TranslationSource.MANUAL,
            )
        )

    with session_factory.begin() as session:
        _write_translations(
            session, recording_id, _result((0, 0, "行きました。", "機器譯文"))
        )

    with session_factory() as session:
        rows = list(session.scalars(select_translations(recording_id)))
    assert [row.text_zh_hk for row in rows] == ["人手譯文"]
    assert rows[0].source is TranslationSource.MANUAL


def test_a_translation_for_a_missing_segment_is_dropped_not_misattached(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, _ = _seed(session_factory, ["行きました。"])

    with session_factory.begin() as session:
        _write_translations(
            session,
            recording_id,
            _result((0, 0, "行きました。", "去咗。"), (7, 9, "無此句。", "唔存在")),
        )

    with session_factory() as session:
        rows = list(session.scalars(select_translations(recording_id)))
    assert [row.text_zh_hk for row in rows] == ["去咗。"]


def test_editing_the_transcript_marks_translations_stale_but_keeps_them(
    app_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["昨日は", "行きました。"])
    with session_factory.begin() as session:
        _write_translations(
            session, recording_id, _result((0, 1, "昨日は行きました。", "尋日去咗。"))
        )

    transcript = app_client.get(f"/api/v1/recordings/{recording_id}/transcript", headers=BEARER)
    assert transcript.status_code == 200
    body = transcript.json()
    assert [item["text_zh_hk"] for item in body["translations"]] == ["尋日去咗。"]
    assert body["translations"][0]["stale"] is False

    saved = app_client.put(
        f"/api/v1/recordings/{recording_id}/transcript",
        headers=BEARER,
        json={
            "expected_revision": body["revision"],
            "segments": [
                {
                    "id": str(segment_ids[0]),
                    "speaker_label": "SPEAKER_00",
                    "start_time": 0.0,
                    "end_time": 0.9,
                    "text": "一昨日は",
                },
                {
                    "id": str(segment_ids[1]),
                    "speaker_label": "SPEAKER_00",
                    "start_time": 1.0,
                    "end_time": 1.9,
                    "text": "行きました。",
                },
            ],
        },
    )

    assert saved.status_code == 200
    after = saved.json()["translations"]
    # The rendering is kept so nothing is lost, but it is no longer trusted.
    assert [item["text_zh_hk"] for item in after] == ["尋日去咗。"]
    assert after[0]["stale"] is True
    assert saved.json()["translation_revision"] == 1


def _pipeline_result(
    recording_id: uuid.UUID,
    texts: list[str] | list[tuple[str, float]],
    *,
    translation: TranslationResult | None = None,
) -> PipelineResult:
    # Reprocessing re-reads the same audio, so a line keeps its real timestamp
    # even when the new pass splits the speech differently.
    timed = [(item, float(index)) if isinstance(item, str) else item
             for index, item in enumerate(texts)]
    return PipelineResult(
        recording_id=str(recording_id),
        audio=AudioProbe(
            duration_seconds=60.0,
            codec_name="pcm_s16le",
            format_name="wav",
            sample_rate=16_000,
            channels=1,
            mime_type="audio/wav",
            preferred_extension=".wav",
        ),
        transcript=tuple(
            MergedTranscriptSegment(
                sequence=index,
                speaker_label="SPEAKER_00",
                start=start,
                end=start + 0.4,
                text=text,
            )
            for index, (text, start) in enumerate(timed)
        ),
        analysis=AnalysisResult(status=AnalysisStatus.SKIPPED, provider="disabled"),
        translation=translation,
    )


def _reprocess(
    session_factory: sessionmaker[Session],
    recording_id: uuid.UUID,
    texts: list[str] | list[tuple[str, float]],
) -> None:
    with session_factory() as session:
        job_id = session.scalar(
            select(ProcessingJob.id).where(ProcessingJob.recording_id == recording_id)
        )
    claim = SimpleNamespace(recording_id=recording_id, id=job_id)
    with session_factory.begin() as session:
        _result_persister(claim, _pipeline_result(recording_id, texts))(session)


def test_reprocessing_keeps_a_hand_written_translation_whose_sentence_survived(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["行きました。"])
    with session_factory.begin() as session:
        session.add(
            TranscriptTranslation(
                recording_id=recording_id,
                start_segment_id=segment_ids[0],
                end_segment_id=segment_ids[0],
                source_ja="行きました。",
                text_zh_hk="人手譯文",
                source=TranslationSource.MANUAL,
            )
        )

    _reprocess(session_factory, recording_id, ["行きました。"])

    with session_factory() as session:
        rows = list(session.scalars(select_translations(recording_id)))
        segments = list(
            session.scalars(
                select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)
            )
        )
    # Re-transcription replaces every segment, so the row has to be re-attached
    # rather than merely left behind pointing at deleted rows.
    assert [row.text_zh_hk for row in rows] == ["人手譯文"]
    assert rows[0].source is TranslationSource.MANUAL
    assert rows[0].start_segment_id == segments[0].id


def test_reprocessing_drops_a_translation_whose_sentence_changed(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["行きました。"])
    with session_factory.begin() as session:
        session.add(
            TranscriptTranslation(
                recording_id=recording_id,
                start_segment_id=segment_ids[0],
                end_segment_id=segment_ids[0],
                source_ja="行きました。",
                text_zh_hk="人手譯文",
                source=TranslationSource.MANUAL,
            )
        )

    _reprocess(session_factory, recording_id, ["帰りました。"])

    with session_factory() as session:
        rows = list(session.scalars(select_translations(recording_id)))
    # The words it was written for are gone; reattaching it would put a
    # translation under a sentence it never described.
    assert rows == []


def test_repeated_sentences_are_restored_by_position_not_by_text(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["はい。", "はい。", "はい。"])
    with session_factory.begin() as session:
        for index, text in ((0, "係。"), (2, "冇錯。")):
            session.add(
                TranscriptTranslation(
                    recording_id=recording_id,
                    start_segment_id=segment_ids[index],
                    end_segment_id=segment_ids[index],
                    source_ja="はい。",
                    text_zh_hk=text,
                    source=TranslationSource.MANUAL,
                )
            )

    _reprocess(session_factory, recording_id, ["はい。", "はい。", "はい。"])

    with session_factory() as session:
        segments = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.recording_id == recording_id)
                .order_by(TranscriptSegment.sequence)
            )
        )
        rows = list(
            session.scalars(
                select(TranscriptTranslation)
                .join(
                    TranscriptSegment,
                    TranscriptSegment.id == TranscriptTranslation.start_segment_id,
                )
                .where(TranscriptTranslation.recording_id == recording_id)
                .order_by(TranscriptSegment.sequence)
            )
        )

    # A text-keyed lookup would send both rows to the same segment, which both
    # misplaces the words and breaks the one-translation-per-start constraint.
    assert [row.text_zh_hk for row in rows] == ["係。", "冇錯。"]
    # The second rendering belonged to the third "yes", not the second.
    assert [row.start_segment_id for row in rows] == [segments[0].id, segments[2].id]


def test_a_sparse_hand_translation_returns_to_its_own_occurrence(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["はい。", "はい。", "はい。"])
    with session_factory.begin() as session:
        session.add(
            TranscriptTranslation(
                recording_id=recording_id,
                start_segment_id=segment_ids[2],
                end_segment_id=segment_ids[2],
                source_ja="はい。",
                text_zh_hk="第三次",
                source=TranslationSource.MANUAL,
            )
        )

    _reprocess(session_factory, recording_id, ["はい。", "はい。", "はい。"])

    with session_factory() as session:
        segments = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.recording_id == recording_id)
                .order_by(TranscriptSegment.sequence)
            )
        )
        rows = list(session.scalars(select_translations(recording_id)))

    # Only one of three identical lines was translated. Matching on "the next
    # unused copy" would move it onto the first line instead of the third.
    assert len(rows) == 1
    assert rows[0].start_segment_id == segments[2].id


def test_an_extra_line_recognised_on_reprocess_does_not_shift_a_translation(
    session_factory: sessionmaker[Session],
) -> None:
    # The first pass missed an interjection; the second pass hears it. Every
    # later occurrence of that line shifts by one.
    recording_id, segment_ids = _seed(session_factory, ["はい。", "ありがとう。", "はい。"])
    with session_factory.begin() as session:
        session.add(
            TranscriptTranslation(
                recording_id=recording_id,
                start_segment_id=segment_ids[2],
                end_segment_id=segment_ids[2],
                source_ja="はい。",
                text_zh_hk="最後嗰句",
                source=TranslationSource.MANUAL,
            )
        )

    _reprocess(
        session_factory,
        recording_id,
        [("はい。", 0.0), ("はい。", 0.5), ("ありがとう。", 1.0), ("はい。", 2.0)],
    )

    with session_factory() as session:
        segments = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.recording_id == recording_id)
                .order_by(TranscriptSegment.sequence)
            )
        )
        rows = list(session.scalars(select_translations(recording_id)))

    # By ordinal the rendering was "the second yes" and would land on the new
    # second yes, a different utterance. Its moment in the audio is what makes
    # it recognisable.
    assert len(rows) == 1
    assert rows[0].start_segment_id == segments[3].id


def test_a_sentence_that_moved_far_in_time_is_not_reattached(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["はい。"])
    with session_factory.begin() as session:
        session.add(
            TranscriptTranslation(
                recording_id=recording_id,
                start_segment_id=segment_ids[0],
                end_segment_id=segment_ids[0],
                source_ja="はい。",
                text_zh_hk="係。",
                source=TranslationSource.MANUAL,
            )
        )

    _reprocess(
        session_factory, recording_id, ["おはよう。", "こんにちは。", "そうです。", "はい。"]
    )

    with session_factory() as session:
        rows = list(session.scalars(select_translations(recording_id)))

    # The same words now appear seconds away, so this is a different moment.
    assert rows == []


def test_two_identical_lines_at_the_same_moment_are_not_guessed_between(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["はい。"])
    with session_factory.begin() as session:
        session.add(
            TranscriptTranslation(
                recording_id=recording_id,
                start_segment_id=segment_ids[0],
                end_segment_id=segment_ids[0],
                source_ja="はい。",
                text_zh_hk="係。",
                source=TranslationSource.MANUAL,
            )
        )

    # The new pass splits one utterance into two identical lines, both within
    # the tolerance of the original moment.
    _reprocess(session_factory, recording_id, [("はい。", 0.0), ("はい。", 0.4)])

    with session_factory() as session:
        rows = list(session.scalars(select_translations(recording_id)))

    # Nothing here says which line the administrator wrote for. Picking the
    # nearer one would let read order decide, and the mistake would be silent.
    assert rows == []


def test_two_hand_translations_merged_into_one_line_are_both_left_out(
    session_factory: sessionmaker[Session],
) -> None:
    recording_id, segment_ids = _seed(session_factory, ["はい。", "はい。"])
    with session_factory.begin() as session:
        for index, text in ((0, "係。"), (1, "冇錯。")):
            session.add(
                TranscriptTranslation(
                    recording_id=recording_id,
                    start_segment_id=segment_ids[index],
                    end_segment_id=segment_ids[index],
                    source_ja="はい。",
                    text_zh_hk=text,
                    source=TranslationSource.MANUAL,
                )
            )

    # The new pass hears one line where the old pass heard two, and both
    # renderings sit within its tolerance.
    _reprocess(session_factory, recording_id, [("はい。", 0.5)])

    with session_factory() as session:
        rows = list(session.scalars(select_translations(recording_id)))

    # Only one can be attached, and nothing says which. Taking whichever row was
    # read first would decide it by accident.
    assert rows == []
