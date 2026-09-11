"""Measured timing policies; voice identity is immutable for vault references."""
import math
import json
from datetime import datetime
from pathlib import Path
from sqlalchemy.exc import IntegrityError

from app.models import VoiceRateProfile
from app.services.character_service import SARVAM_VOICE_IDS


def ensure_measured_profiles(db):
    """Install the audited measurements only where absent; preserve recalibration."""
    existing = {(p.voice_id, p.language) for p in db.query(VoiceRateProfile).all()}
    rows = json.loads(Path(__file__).with_name("voice_rate_profiles.json").read_text())
    for row in rows:
        if (row["voice_id"], row["language"]) not in existing:
            try:
                with db.begin_nested():
                    db.add(VoiceRateProfile(**{**row, "measured_at": datetime.fromisoformat(row["measured_at"])}))
            except IntegrityError:
                # Another job installed the same immutable measurement first.
                if db.get(VoiceRateProfile, (row["voice_id"], row["language"])) is None:
                    raise
    db.commit()


def measured_budget(db, language):
    ensure_measured_profiles(db)
    rates = [p.chars_per_second for p in db.query(VoiceRateProfile).filter_by(language=language).all()]
    if not rates:
        raise ValueError(f"Missing voice calibration for {language}")
    average = sum(rates) / len(rates)
    return {"language": language, "measured_mean_chars_per_second": average,
            "instruction": "Estimate each line's soft character budget as this rate times its shot duration. This replaces the provisional 150-200 character target. Preserve user-scripted words; decoded audio is authoritative."}


def mood_pace(mood):
    text = (mood or "").casefold()
    if any(word in text for word in ("urgent", "frantic", "excited", "exciting", "energetic",
            "उत्साह", "उत्साहित", "तातडी", "जल्दबाजी", "উত্তেজিত", "জরুরি", "உற்சாக", "அவசர",
            "ఉత్సాహ", "అత్యవసర", "ಉತ್ಸಾಹ", "ತುರ್ತು", "ആവേശ", "അടിയന്തിര", "ઉત્સાહ", "તાકીદ", "ਉਤਸ਼ਾਹ", "ਜਲਦੀ", "ଉତ୍ସାହ", "ଜରୁରୀ")):
        return 1.12
    if any(word in text for word in ("solemn", "tender", "gentle", "sad", "grief", "mourn",
            "गंभीर", "कोमल", "उदास", "शोक", "বিষণ্ণ", "কোমল", "சோகம்", "மென்மை", "విషాద", "మృదువైన",
            "ದುಃಖ", "ಮೃದು", "ദുഃഖ", "സൗമ്യ", "ઉદાસ", "કોમળ", "ਉਦਾਸ", "ਕੋਮਲ", "ଦୁଃଖ", "କୋମଳ")):
        return 0.88
    return 1.0


def native_script_guard(text, language):
    from app.services.voice_generation_service import VoiceGenerationError
    if not text.strip():
        raise VoiceGenerationError("Dialogue text is empty")
    if len(text) > 2500:
        raise VoiceGenerationError(f"Dialogue has {len(text)} characters; maximum is 2,500. Shorten the line before generating audio.")
    ranges = {
        "hindi": (0x0900,0x097f), "marathi": (0x0900,0x097f), "bengali": (0x0980,0x09ff),
        "tamil": (0x0b80,0x0bff), "telugu": (0x0c00,0x0c7f), "kannada": (0x0c80,0x0cff),
        "malayalam": (0x0d00,0x0d7f), "gujarati": (0x0a80,0x0aff), "punjabi": (0x0a00,0x0a7f), "odia": (0x0b00,0x0b7f),
    }
    limits = ranges.get(language.casefold())
    if limits and not any(limits[0] <= ord(char) <= limits[1] for char in text):
        raise VoiceGenerationError("Indic dialogue must contain the language's native script; Romanized input degrades Sarvam quality. Correct the text before generating audio.")


def casting_fields(character):
    """Invalid classification disables demographic narrowing, never duration fit."""
    gender, age = character.get("gender"), character.get("age_bracket")
    if (not isinstance(gender, str) or gender not in {"female", "male", "nonbinary", "unspecified"}
            or not isinstance(age, str) or age not in {"child", "teen", "adult", "older_adult", "unspecified"}):
        return None, None
    return gender, age


def casting_warning(character):
    if casting_fields(character) == (None, None):
        return (f"CASTING WARNING — {character.get('name', 'Unnamed character')}: Continuity gender/age_bracket classification is missing or invalid. "
                "Casting is proceeding WITHOUT demographic narrowing, using all 37 voices for duration-fit selection only. Voice gender/age suitability is not assured.")
    return None


def eligible_voices(character):
    from app.services.voice_generation_service import FEMALE_VOICE_IDS
    gender, age = casting_fields(character)
    female, male = gender == "female", gender == "male"
    # Preserve existing catalog casting rules; these are not measured speaker ages.
    if age == "older_adult": return ["roopa"] if female else ["ratan"] if male else ["roopa", "ratan"]
    if age in {"child", "teen"}: return ["kavya"] if female else ["kabir"] if male else ["kavya", "kabir"]
    return [v for v in SARVAM_VOICE_IDS if (v in FEMALE_VOICE_IDS if female else v not in FEMALE_VOICE_IDS if male else True)]


def select_calibrated_voices(db, result, language, *, emit=None):
    """Select once before first synthesis; lack of measured data is an explicit error."""
    from app.services.voice_generation_service import VoiceGenerationError
    ensure_measured_profiles(db)
    records = []
    for character in result.get("continuity", {}).get("characters", []):
        if character.get("voice_assignment") != "heuristic" or character.get("character_id"):
            continue
        shots = [s for s in result.get("shots", []) if s.get("has_dialogue") and character["name"] in s.get("characters_in_shot", [])]
        if not shots: continue
        warning = casting_warning(character)
        if warning:
            character["casting_warning"] = warning
            if emit:
                emit("casting_warning", warning)
        candidates = []
        for voice in eligible_voices(character):
            profile = db.get(VoiceRateProfile, (voice, language))
            if not profile or not math.isfinite(profile.chars_per_second) or profile.chars_per_second <= 0:
                raise VoiceGenerationError(f"Voice calibration required for {voice}/{language}; run Step 0 before duration-aware selection.")
            required = [len(s.get("dialogue_text", "")) / profile.chars_per_second / max(float(s["duration_sec"]), .01) for s in shots]
            candidates.append({"voice_id":voice,"chars_per_second":profile.chars_per_second,"required_paces":required,
                               "mean_pace_deviation":sum(abs(p-1) for p in required)/len(required)})
        winner = min(candidates, key=lambda c:(c["mean_pace_deviation"],c["voice_id"]))
        character.update(voice_id=winner["voice_id"],voice_sample_ref=winner["voice_id"],voice_assignment="calibrated")
        record = {"character":character["name"],"language":language,"gender":character.get("gender"),"age_bracket":character.get("age_bracket"),
                  "classification_valid": warning is None, "casting_warning": warning,
                  "casting_note": "Structured Continuity fields only; nonbinary/unspecified gender imposes no gender filter, unspecified age imposes no age filter.",
                  "candidates":candidates,"selected_voice_id":winner["voice_id"],
                  "reason":"Smallest mean absolute pace deviation from 1.0 across this character's dialogue shots; mood applied separately."}
        character["voice_selection"] = record
        records.append(record)
    return records
