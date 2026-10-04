"""System-wide switches for the LLM work queued behind a transcript.

Transcription always runs, so it has no switch. Analysis and the Cantonese
translation each have one; the worker reads them at the moment a finished
transcript is handed on, which is why a change needs no restart.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request

from audio_server.api.dependencies import require_mutation_principal, require_principal
from audio_server.api.schemas import ProcessingSettingsResponse, ProcessingSettingsUpdate
from audio_server.jobs.queue import AutomaticFollowUps
from audio_server.services.processing_settings_service import ProcessingSettingsService

router = APIRouter(
    prefix="/api/v1/settings",
    tags=["settings"],
    dependencies=[Depends(require_principal)],
)


def get_processing_settings_service(request: Request) -> ProcessingSettingsService:
    return cast(ProcessingSettingsService, request.app.state.processing_settings_service)


@router.get("/processing", response_model=ProcessingSettingsResponse)
def read_processing_settings(
    service: Annotated[ProcessingSettingsService, Depends(get_processing_settings_service)],
) -> ProcessingSettingsResponse:
    return _response(service.get())


@router.patch("/processing", response_model=ProcessingSettingsResponse)
def update_processing_settings(
    payload: ProcessingSettingsUpdate,
    _principal: Annotated[object, Depends(require_mutation_principal)],
    service: Annotated[ProcessingSettingsService, Depends(get_processing_settings_service)],
) -> ProcessingSettingsResponse:
    return _response(
        service.update(
            auto_analysis=payload.auto_analysis,
            auto_translation=payload.auto_translation,
        )
    )


def _response(switches: AutomaticFollowUps) -> ProcessingSettingsResponse:
    return ProcessingSettingsResponse(
        auto_analysis=switches.analysis,
        auto_translation=switches.translation,
    )
