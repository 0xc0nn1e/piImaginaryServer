"""Per-user bookmark API for saved analysis expressions and highlights."""

from __future__ import annotations

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response

from audio_server.api.schemas import (
    BookmarkCreateRequest,
    BookmarkListResponse,
    BookmarkResponse,
    FuriganaToken,
)
from audio_server.core.furigana import annotate_all
from audio_server.db.models import BookmarkKind
from audio_server.services.bookmark_service import BookmarkService
from audio_server.web_auth.dependencies import (
    require_web_mutation_session,
    require_web_session,
)
from audio_server.web_auth.service import AuthenticatedWebSession

# Bookmarks belong to an administrator account, so this router is browser-only:
# the machine Bearer credential identifies a device, not a user.
router = APIRouter(prefix="/api/v1/bookmarks", tags=["bookmarks"])


def get_bookmark_service(request: Request) -> BookmarkService:
    return cast(BookmarkService, request.app.state.bookmark_service)


@router.get("", response_model=BookmarkListResponse)
def list_bookmarks(
    response: Response,
    principal: Annotated[AuthenticatedWebSession, Depends(require_web_session)],
    service: Annotated[BookmarkService, Depends(get_bookmark_service)],
    kind: Annotated[BookmarkKind | None, Query()] = None,
) -> BookmarkListResponse:
    response.headers["Cache-Control"] = "no-store"
    items = service.list_for_user(user_id=principal.user.id, kind=kind)
    japanese = [text for item in items for text in (item.original_ja, item.note_ja)]
    return BookmarkListResponse(
        items=[BookmarkResponse.model_validate(item) for item in items],
        furigana={
            text: [FuriganaToken.model_validate(token) for token in tokens]
            for text, tokens in annotate_all(japanese).items()
        },
    )


@router.post("", response_model=BookmarkResponse, status_code=201)
def create_bookmark(
    payload: BookmarkCreateRequest,
    principal: Annotated[AuthenticatedWebSession, Depends(require_web_mutation_session)],
    service: Annotated[BookmarkService, Depends(get_bookmark_service)],
) -> BookmarkResponse:
    bookmark = service.create(
        user_id=principal.user.id,
        kind=payload.kind,
        recording_id=payload.recording_id,
        original_ja=payload.original_ja,
        translation_zh_hk=payload.translation_zh_hk,
        note_ja=payload.note_ja,
        note_zh_hk=payload.note_zh_hk,
        speaker_label=payload.speaker_label,
        start_time=payload.start_time,
        end_time=payload.end_time,
    )
    return BookmarkResponse.model_validate(bookmark)


@router.delete("/{bookmark_id}", status_code=204)
def delete_bookmark(
    bookmark_id: uuid.UUID,
    principal: Annotated[AuthenticatedWebSession, Depends(require_web_mutation_session)],
    service: Annotated[BookmarkService, Depends(get_bookmark_service)],
) -> Response:
    service.delete(user_id=principal.user.id, bookmark_id=bookmark_id)
    return Response(status_code=204)
