import logging
from fastapi import APIRouter, HTTPException, Response
from app.services.voice_preview_service import catalog, PREVIEW_TEXT

router = APIRouter(prefix='/api/voice-previews', tags=['voice previews'])
logger = logging.getLogger(__name__)


@router.get('')
def get_previews(response: Response, language: str = 'English'):
    if language not in PREVIEW_TEXT:
        raise HTTPException(422, 'No cached previews for this language yet')
    response.headers['Cache-Control'] = 'no-store'
    try:
        return catalog(language)
    except Exception as exc:
        logger.warning('Voice preview catalog unavailable: %s', type(exc).__name__)
        raise HTTPException(503, 'Voice previews are temporarily unavailable. You can still choose and save a voice.') from exc
