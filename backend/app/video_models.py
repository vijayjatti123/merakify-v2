"""Job-level rendering choices, independent of Compiler model-family names."""
from typing import Literal
AUTOMATIC = "automatic_omni_mini"  # Legacy saved-job ID; now Mini-only, never Omni.
VideoModel = Literal["h3_max_fal", "seedance_evolink", "seedance_fal", "seedance_fast_evolink", "seedance_mini_evolink",
                     "seedance_fast_fal", "seedance_mini_fal", "kling_voice_fal", "kling_avatar_fal", "automatic_omni_mini"]


def validate_selection(model, family, language, quality):
    if not model:
        return
    if model == AUTOMATIC and quality != "720p":
        raise ValueError("Automatic uses the verified 720p output tier; select 720p")
    expected = "MiniMax H3 Max" if model == "h3_max_fal" else ("Kling 3.0" if model.startswith("kling_") else "Seedance 2.0")
    if family != expected:
        raise ValueError("The selected video model must match its planning model family")
    if model == "kling_voice_fal" and language.strip().lower() not in {"english", "chinese", "en", "zh", "mandarin"}:
        raise ValueError("Kling voice-ID supports English/Chinese only. Choose Seedance for this language.")
    if ("_fast_" in model or "_mini_" in model) and quality not in {"480p", "720p"}:
        raise ValueError("Seedance Fast and Mini support 480p or 720p only")
