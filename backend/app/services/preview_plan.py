"""Deterministic still-render adapter for the authoritative approved shot plan."""
import json
from urllib.parse import urlsplit, urlunsplit


def preview_input(result, shot):
    if not shot.get("description"):
        return None  # Legacy compiled-only records retain their existing adapter.
    characters = [c for c in result.get("continuity", {}).get("characters", [])
                  if c.get("name", "").casefold() in {n.casefold() for n in shot.get("characters_in_shot", [])}]
    facts = {key: shot.get(key) for key in ("description", "camera_angle", "camera_movement", "lens",
             "lighting", "composition_note", "state_at_shot_start", "state_at_shot_end", "characters_in_shot")}
    facts["characters"] = [{k: c.get(k) for k in ("name", "description", "character_id", "image_url")}
                           for c in characters]
    for character in facts["characters"]:
        url = character.get("image_url")
        if url and "x-amz-signature=" in url.lower():
            parsed = urlsplit(url)
            character["image_url"] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    facts["visual_style"] = result.get("continuity", {}).get("visual_style", {})
    facts["aspect_ratio"] = result.get("aspect_ratio", "16:9")
    # Deliberately excludes duration, dialogue, signed audio URLs and video prose.
    # Source image URLs remain in the fingerprint but are omitted from prose.
    return facts


def preview_visual(facts):
    visible = {**facts, "characters": [{k: v for k, v in c.items() if k != "image_url"}
                                      for c in facts["characters"]]}
    return ("Depict the opening physical instant of this approved shot. Preserve its subject, framing, "
            "style and locked identity facts. The ending state and camera movement are context only; "
            "do not depict a later moment. No readable text, logos, collage or additional subjects.\n"
            + json.dumps(visible, ensure_ascii=False))
