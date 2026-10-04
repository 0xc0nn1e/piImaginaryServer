"""The administrator's system-wide switches for automatic LLM work."""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from audio_server.db.models import PROCESSING_SETTINGS_ID, ProcessingSettings
from audio_server.jobs.queue import AutomaticFollowUps, read_automatic_follow_ups

logger = logging.getLogger(__name__)


class ProcessingSettingsService:
    """Reads and changes which LLM steps follow a transcript on their own.

    The worker reads the same row when it hands a finished transcript on, so a
    change takes effect at the next hand-off without restarting anything.
    """

    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self) -> AutomaticFollowUps:
        with self._session_factory() as session:
            return read_automatic_follow_ups(session)

    def update(
        self,
        *,
        auto_analysis: bool | None = None,
        auto_translation: bool | None = None,
    ) -> AutomaticFollowUps:
        """Change only the switches given and leave the others as stored.

        Two tabs that each flip a different switch therefore cannot undo one
        another, which replacing the whole set would.
        """

        try:
            switches = self._write(auto_analysis=auto_analysis, auto_translation=auto_translation)
        except IntegrityError:
            # Only reachable when the seeded row is missing and two first writes
            # raced to insert it. The loser's transaction is already rolled
            # back and the row now exists, so writing again just updates it.
            switches = self._write(auto_analysis=auto_analysis, auto_translation=auto_translation)
        logger.info(
            "processing settings updated",
            extra={
                "auto_analysis": switches.analysis,
                "auto_translation": switches.translation,
            },
        )
        return switches

    def _write(
        self, *, auto_analysis: bool | None, auto_translation: bool | None
    ) -> AutomaticFollowUps:
        with self._session_factory.begin() as session:
            settings = session.get(
                ProcessingSettings, PROCESSING_SETTINGS_ID, with_for_update=True
            )
            if settings is None:
                defaults = AutomaticFollowUps()
                settings = ProcessingSettings(
                    id=PROCESSING_SETTINGS_ID,
                    auto_analysis=defaults.analysis,
                    auto_translation=defaults.translation,
                )
                session.add(settings)
            if auto_analysis is not None:
                settings.auto_analysis = auto_analysis
            if auto_translation is not None:
                settings.auto_translation = auto_translation
            session.flush()
            return AutomaticFollowUps(
                analysis=settings.auto_analysis,
                translation=settings.auto_translation,
            )
