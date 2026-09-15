"""Read cached preview assets only. No synthesis or character mutation here."""
import json
from botocore.exceptions import ClientError
from app.services import storage_service as storage
from app.services.character_service import SARVAM_VOICE_IDS

VERSION = 'v1'
PREVIEW_TEXT = {
    'English': "Hello! Here's a short preview of my voice.",
    'Hindi': 'नमस्ते! मेरी आवाज़ का यह छोटा सा नमूना सुनिए।',
    'Tamil': 'வணக்கம்! இது என் குரலின் ஒரு சிறிய மாதிரி.',
    'Telugu': 'నమస్కారం! ఇది నా గొంతుకు ఒక చిన్న నమూనా.',
    'Bengali': 'নমস্কার! এটি আমার কণ্ঠের একটি ছোট নমুনা।',
}


def preview_key(language, voice_id):
    if language not in PREVIEW_TEXT or voice_id not in SARVAM_VOICE_IDS:
        raise ValueError('Unknown preview language or voice')
    return f'voice-previews/{VERSION}/{language.lower()}/{voice_id}.wav'


def manifest_key(language):
    if language not in PREVIEW_TEXT:
        raise ValueError('Preview language unavailable')
    return f'voice-previews/{VERSION}/{language.lower()}/manifest.json'


def read_manifest(language):
    try:
        return json.loads(storage.download_bytes(manifest_key(language)))
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') in {'NoSuchKey', '404'}:
            return {}
        raise


def catalog(language):
    manifest = read_manifest(language)
    voices = []
    for voice in SARVAM_VOICE_IDS:
        entry = manifest.get(voice)
        available = bool(entry and entry.get('key') == preview_key(language, voice))
        voices.append({'voice_id': voice, 'available': available,
                       'url': storage.asset_url(entry['key'], expires_in=1800) if available else None,
                       'duration_sec': entry.get('duration_sec') if available else None})
    return {'language': language, 'languages': list(PREVIEW_TEXT), 'expires_in': 1800, 'voices': voices}
