"""Explicit, resumable maintenance command. Never imported by request handlers.

python -m app.services.generate_voice_previews --output <audit-directory>
Add one catalog voice: --voices <id>. Add language text in PREVIEW_TEXT first.
"""
import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from botocore.exceptions import ClientError
from app.services import storage_service as storage
from app.services.character_service import SARVAM_VOICE_IDS
from app.services.voice_generation_service import _sarvam_tts, decoded_audio_duration
from app.services.voice_preview_service import PREVIEW_TEXT, preview_key, manifest_key, read_manifest


async def generate(output, languages, voices):
    output.mkdir(parents=True, exist_ok=True)
    failures = []
    semaphore = asyncio.Semaphore(3)
    for language in languages:
        # Missing private keys can report 403 when ListBucket is intentionally
        # denied. Create only if absent; never overwrite an existing catalog.
        try:
            storage._s3_client().put_object(Bucket=storage._bucket(), Key=manifest_key(language),
                Body=b'{}', ContentType='application/json', IfNoneMatch='*')
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') not in {'PreconditionFailed', '412'}:
                raise
        manifest = read_manifest(language)
        async def one(voice):
            key = preview_key(language, voice)
            if manifest.get(voice, {}).get('key') == key:
                return
            async with semaphore:
                record = {'voice_id': voice, 'language': language, 'key': key,
                          'started_at': datetime.now(timezone.utc).isoformat()}
                try:
                    async with httpx.AsyncClient(timeout=90) as client:
                        async def capture(response):
                            await response.aread()
                            record.setdefault('requests', []).append({'payload': json.loads(response.request.content),
                                'status': response.status_code, 'request_id': response.headers.get('x-request-id')})
                        client.event_hooks['response'] = [capture]
                        started = time.monotonic()
                        # Sarvam only. A fallback provider would misrepresent the selected voice.
                        for attempt in range(2):
                            try:
                                audio = await _sarvam_tts(client, PREVIEW_TEXT[language], voice, language, pace=1.0)
                                break
                            except Exception:
                                if attempt: raise
                                await asyncio.sleep(5)
                        record['provider_seconds'] = round(time.monotonic()-started, 3)
                    duration = decoded_audio_duration(audio.data)
                    if duration <= 0:
                        raise ValueError('Decoded preview is empty')
                    stored = storage.upload_bytes(key, audio.data, content_type='audio/wav', cache_control='private, max-age=86400')
                    entry = {'key': key, 'duration_sec': duration, 'sha256': hashlib.sha256(audio.data).hexdigest(),
                             'text': PREVIEW_TEXT[language], 'generated_at': datetime.now(timezone.utc).isoformat()}
                    manifest[voice] = entry
                    storage.upload_bytes(manifest_key(language), json.dumps(manifest, ensure_ascii=False).encode(), content_type='application/json')
                    record.update(entry, url=stored['url'], status='stored')
                except Exception as exc:
                    record.update(status='failed', error=str(exc)); failures.append((language, voice))
                (output/f'{language}-{voice}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
                print(json.dumps({'language':language,'voice':voice,'status':record['status']}), flush=True)
        await asyncio.gather(*(one(v) for v in voices))
    if failures:
        raise RuntimeError(f'Preview batch incomplete: {failures}; rerun to fill missing entries')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--languages', nargs='+', choices=list(PREVIEW_TEXT), default=list(PREVIEW_TEXT))
    parser.add_argument('--voices', nargs='+', choices=list(SARVAM_VOICE_IDS), default=list(SARVAM_VOICE_IDS))
    args = parser.parse_args()
    asyncio.run(generate(args.output, args.languages, args.voices))
