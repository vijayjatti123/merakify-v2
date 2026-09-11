"""Re-runnable, measured Bulbul v3 calibration; no estimated rates enter the table."""
import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

import httpx

from app.db import Base, SessionLocal, engine
from app.models import VoiceRateProfile
from app.services.character_service import SARVAM_VOICE_IDS
from app.services.voice_generation_service import _sarvam_tts, decoded_audio_duration

CALIBRATION_TEXT = {
    "English": "The morning light falls through the window as we sit together and talk about our plans for the day.",
    "Hindi": "सुबह की रोशनी खिड़की से अंदर आती है और हम साथ बैठकर आज के काम और अपनी योजनाओं के बारे में बात करते हैं।",
    "Bengali": "সকালের আলো জানালা দিয়ে ঘরে আসে আর আমরা একসঙ্গে বসে আজকের কাজ এবং আমাদের পরিকল্পনা নিয়ে কথা বলি।",
    "Tamil": "காலை வெளிச்சம் ஜன்னல் வழியாக உள்ளே வருகிறது. நாம் ஒன்றாக அமர்ந்து இன்றைய வேலைகளையும் நமது திட்டங்களையும் பற்றி பேசுகிறோம்.",
    "Telugu": "ఉదయం వెలుతురు కిటికీ నుంచి లోపలికి వస్తుంది. మనం కలిసి కూర్చొని ఈ రోజు పనులు మరియు మన ప్రణాళికల గురించి మాట్లాడుకుంటాం.",
    "Kannada": "ಬೆಳಗಿನ ಬೆಳಕು ಕಿಟಕಿಯಿಂದ ಒಳಗೆ ಬರುತ್ತದೆ. ನಾವು ಒಟ್ಟಿಗೆ ಕುಳಿತು ಇಂದಿನ ಕೆಲಸಗಳು ಮತ್ತು ನಮ್ಮ ಯೋಜನೆಗಳ ಬಗ್ಗೆ ಮಾತನಾಡುತ್ತೇವೆ.",
    "Malayalam": "പ്രഭാത വെളിച്ചം ജനലിലൂടെ അകത്തേക്ക് വരുന്നു. നമ്മൾ ഒരുമിച്ചിരുന്ന് ഇന്നത്തെ ജോലികളെക്കുറിച്ചും നമ്മുടെ പദ്ധതികളെക്കുറിച്ചും സംസാരിക്കുന്നു.",
    "Marathi": "सकाळचा प्रकाश खिडकीतून आत येतो आणि आपण एकत्र बसून आजच्या कामांबद्दल आणि आपल्या योजनांबद्दल बोलतो.",
    "Gujarati": "સવારનો પ્રકાશ બારીમાંથી અંદર આવે છે અને આપણે સાથે બેસીને આજના કામ અને આપણી યોજનાઓ વિશે વાત કરીએ છીએ.",
    "Punjabi": "ਸਵੇਰ ਦੀ ਰੌਸ਼ਨੀ ਖਿੜਕੀ ਰਾਹੀਂ ਅੰਦਰ ਆਉਂਦੀ ਹੈ ਅਤੇ ਅਸੀਂ ਇਕੱਠੇ ਬੈਠ ਕੇ ਅੱਜ ਦੇ ਕੰਮ ਅਤੇ ਆਪਣੀਆਂ ਯੋਜਨਾਵਾਂ ਬਾਰੇ ਗੱਲ ਕਰਦੇ ਹਾਂ।",
    "Odia": "ସକାଳର ଆଲୋକ ଝରକା ଦେଇ ଭିତରକୁ ଆସେ ଏବଂ ଆମେ ଏକାଠି ବସି ଆଜିର କାମ ଓ ଆମର ଯୋଜନା ବିଷୟରେ କଥା ହେଉ।",
}


async def calibrate(output: Path, languages=None, force=False):
    Base.metadata.create_all(engine)
    output.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(3)
    failures = []
    async def measure(voice, language):
        with SessionLocal() as db:
            if not force and db.get(VoiceRateProfile, (voice, language)):
                return
        async with semaphore:
            text = CALIBRATION_TEXT[language]
            prefix = f"{language}-{voice}"
            record = {"voice_id": voice, "language": language, "text": text, "character_count": len(text), "pace": 1.0}
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    # Capture actual body/response, excluding credential headers.
                    async def capture(response):
                        await response.aread()
                        (output / f"{prefix}-request.json").write_bytes(response.request.content)
                        (output / f"{prefix}-response.json").write_bytes(response.content)
                    client.event_hooks["response"] = [capture]
                    audio = await _sarvam_tts(client, text, voice, language, pace=1.0)
                duration = decoded_audio_duration(audio.data)
                record.update(duration_sec=duration, chars_per_second=len(text)/duration,
                              measured_at=datetime.utcnow().isoformat(), audio_sha256=hashlib.sha256(audio.data).hexdigest())
                (output / f"{prefix}.wav").write_bytes(audio.data)
                with SessionLocal() as db:
                    profile = db.get(VoiceRateProfile, (voice, language)) or VoiceRateProfile(voice_id=voice, language=language)
                    profile.chars_per_second = record["chars_per_second"]
                    profile.measured_at = datetime.fromisoformat(record["measured_at"])
                    db.add(profile); db.commit()
            except Exception as error:
                record["error"] = str(error)
                failures.append(prefix)
            (output / f"{prefix}-measurement.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({k:v for k,v in record.items() if k not in {"text"}},ensure_ascii=True),flush=True)
    await asyncio.gather(*(measure(voice, language) for language in (languages or CALIBRATION_TEXT) for voice in SARVAM_VOICE_IDS))
    with SessionLocal() as db:
        rows = [{"voice_id":p.voice_id,"language":p.language,"chars_per_second":p.chars_per_second,"measured_at":p.measured_at.isoformat()} for p in db.query(VoiceRateProfile).all()]
    (output / "profiles.json").write_text(json.dumps(rows,indent=2),encoding="utf-8")
    if failures:
        raise RuntimeError(f"Calibration incomplete: {len(failures)} failed pairs; rerun to fill missing pairs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--languages", nargs="+", choices=list(CALIBRATION_TEXT))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(calibrate(args.output, args.languages, args.force))
