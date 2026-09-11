"""Pure local, voice-independent pre-flight timing; no provider or DB access."""
import json
import math
import re
import unicodedata

# Untuned defaults, not measurements or replacements for Module J's voice profiles.
SYLLABLES_PER_SECOND = 4.5
COMMA_PAUSE_SEC = 0.25
SENTENCE_PAUSE_SEC = 0.5
MISMATCH_THRESHOLD = 0.25
SCRIPT_NAMES = frozenset({"DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI",
                          "ORIYA", "TAMIL", "TELUGU", "KANNADA", "MALAYALAM"})
VOWELS = frozenset({"A", "AA", "I", "II", "U", "UU", "E", "EE", "AI", "O", "OO", "AU",
                    "VOCALIC R", "VOCALIC RR", "VOCALIC L", "VOCALIC LL",
                    "CANDRA A", "CANDRA E", "CANDRA O", "SHORT A", "SHORT E", "SHORT O",
                    "AW", "AE", "OE", "OOE"})


def estimate_dialogue_duration(text: str) -> dict:
    """Count vowel nuclei, including matras, plus punctuation pause groups.

    Unicode Indic consonants carry an inherent vowel; a matra replaces it and
    virama cancels it. NFC and one nucleus per base avoid split-matra double counts.
    https://www.unicode.org/versions/Unicode17.0.0/core-spec/chapter-12/

    This is still an estimate, not a guarantee: schwa deletion, delivery, voice,
    mood, and context vary beyond local text analysis. The goal is reducing how
    often Module J's Layer 4 needs to intervene, not eliminating it. This warning-
    only check does not itself change timing or guarantee fewer corrections.
    Latin vowel groups are a coarse fallback, not an English pronunciation model;
    digits/unsupported scripts are explicitly reported as incomplete coverage.
    """
    # Text-only estimation predicts sentence duration, not voice-specific delivery
    # variance. Module J's Step 0 calibration handles voice-specific rates; a miss
    # driven by voice delivery is not by itself evidence of an estimator bug.
    # Keep this signal experimental and low priority pending production evidence.
    text = unicodedata.normalize("NFC", text)
    independent = inherent = matras = matra_codepoints = viramas = 0
    unsupported = 0
    active = None
    latin = []
    for char in text:
        name = unicodedata.name(char, "")
        script = name.split(" ", 1)[0]
        if script in SCRIPT_NAMES:
            latin.append(" ")
            if "LETTER " in name:
                letter = name.split("LETTER ", 1)[1]
                if "CHILLU" in letter or letter == "KHANDA TA":
                    active = None  # Explicitly vowelless consonants.
                elif letter in VOWELS:
                    independent += 1
                    active = "independent"
                else:
                    inherent += 1
                    active = "inherent"
            elif "VOWEL SIGN" in name:
                matra_codepoints += 1
                if active == "inherent":
                    inherent -= 1
                    matras += 1
                elif active not in {"matra", "independent"}:
                    matras += 1
                active = "matra"
            elif "VIRAMA" in name:
                if active == "inherent":
                    inherent -= 1
                viramas += 1
                active = None
            elif char.isdigit():
                unsupported += 1
                active = None
            # Nukta, nasalization and length marks add no vowel nucleus.
        elif char in "\u200c\u200d":
            continue  # Join controls do not change the syllable count.
        else:
            active = None
            latin.append(char if char.isascii() and char.isalpha() else " ")
            if char.isalnum() and not (char.isascii() and char.isalpha()):
                unsupported += 1
    latin_units = 0
    for word in re.findall(r"[a-z]+", "".join(latin).lower()):
        groups = len(re.findall(r"[aeiouy]+", word))
        if word.endswith("e") and not word.endswith("le") and groups > 1:
            groups -= 1
        latin_units += max(1, groups)
    commas = len(re.findall(r"[,，]+", text))
    # Repeated punctuation/ellipsis/double danda denotes one pause, not N pauses.
    # Treat decimal points as part of unsupported number speech, not sentence ends.
    endings = len(re.findall(r"[.!?।॥…。！？]+", re.sub(r"(?<=\d)\.(?=\d)", "", text)))
    syllables = independent + inherent + matras + latin_units
    pause = commas * COMMA_PAUSE_SEC + endings * SENTENCE_PAUSE_SEC
    predicted = syllables / SYLLABLES_PER_SECOND + pause if syllables else None
    return {"method": "local_vowel_nuclei_v1", "syllable_units": syllables,
            "matra_codepoints": matra_codepoints, "matra_nuclei": matras,
            "independent_vowels": independent, "inherent_vowels": inherent,
            "viramas": viramas, "latin_fallback_units": latin_units,
            "unsupported_alphanumeric_count": unsupported,
            "coverage": "partial" if unsupported else "latin_heuristic" if latin_units else "brahmic_proxy",
            "comma_groups": commas, "sentence_end_groups": endings,
            "pause_sec": pause, "baseline_syllables_per_second": SYLLABLES_PER_SECOND,
            "predicted_duration_sec": round(predicted, 6) if predicted is not None else None}


def preflight_dialogue_durations(shots: list[dict], *, emit) -> list[dict]:
    """Emit trace evidence immediately after Cinematography; never alter shots."""
    records = []
    for shot in shots:
        if not shot.get("has_dialogue"):
            continue
        text = shot.get("dialogue_text")
        estimate = estimate_dialogue_duration(text) if isinstance(text, str) else estimate_dialogue_duration("")
        planned = shot.get("duration_sec")
        valid = isinstance(planned, (int, float)) and not isinstance(planned, bool) and math.isfinite(planned) and planned > 0
        predicted = estimate["predicted_duration_sec"]
        divergence = abs(predicted - planned) / planned if valid and predicted is not None else None
        warning = divergence is None or divergence > MISMATCH_THRESHOLD or estimate["coverage"] == "partial"
        record = {"shot_number": shot.get("shot_number"), "stage": "after_cinematography_before_qa_and_tts",
                  "planned_duration_sec": planned if valid else None, **estimate,
                  "relative_divergence": round(divergence, 6) if divergence is not None else None,
                  "threshold": MISMATCH_THRESHOLD, "warning": warning,
                  "confidence": "experimental", "priority": "low"}
        finding = "possible mismatch or incomplete estimate" if warning else "within estimate threshold"
        emit("duration_preflight_experimental",
             f"Experimental duration preflight (low priority): {finding} — "
             f"local estimate only; before any dialogue TTS call; "
             f"planned timing unchanged; Module J calibration/correction retained. {json.dumps(record)}")
        records.append(record)
    return records
