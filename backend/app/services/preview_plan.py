"""Deterministic still-render adapter for the authoritative approved shot plan."""
import json
from urllib.parse import urlsplit, urlunsplit


def preview_input(result, shot):
    from app.services.ad_direction import opening_cast
    if not shot.get("description"):
        return None  # Legacy compiled-only records retain their existing adapter.
    if shot.get('direction_version') == 1:
        from app.services.ad_direction import problems
        errors = problems(shot)
        if errors:
            raise ValueError(f"Shot {shot['shot_number']}: direction needs review before previews: {'; '.join(errors)}")
    characters = [c for c in result.get("continuity", {}).get("characters", [])
                  if c.get("name", "").casefold() in {n.casefold() for n in opening_cast(shot)}]
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
    if shot.get("direction_version") == 1:
        from app.services.ad_direction import visual_direction
        facts['direction_version'] = 1
        direction = visual_direction(result)
        facts['ad_visual_direction'] = {'visual_approach': direction['visual_approach']} if direction else None
        facts['characters_in_shot'] = opening_cast(shot)
        # Performance, story purpose and ending belong to video. Their named
        # later arrivals must not leak into this opening-frame request.
    # Deliberately excludes duration, dialogue, signed audio URLs and video prose.
    # Source image URLs remain in the fingerprint but are omitted from prose.
    return facts


def preview_visual(facts):
    # Video motion/end-state belongs to compilation, not the still request.
    visible = {k: v for k, v in facts.items() if k not in {"state_at_shot_end", "camera_movement"}}
    if facts.get("state_at_shot_start"):
        visible.pop("description", None)
        # Whole-shot composition can describe a later reveal (e.g. an open
        # parachute). The opening state and camera angle own still staging.
        visible.pop("composition_note", None)
    visible = {**visible, "characters": [{k: v for k, v in c.items() if k != "image_url"}
                                      for c in facts["characters"]]}
    spatial = ["Follow each person's inside/outside position, support surface and contact relationships in the opening state. Show enough surrounding geometry to establish those relationships. A camera looking out through a doorway must not relocate an inside person into the exterior. An explicitly airborne or outside person must remain outside. Do not infer containment merely from a mentioned vehicle or room."]
    if spatial:
        visible["spatial_requirements"] = spatial
    if facts.get('direction_version') == 1:
        for key in ('description', 'camera_movement', 'state_at_shot_end'):
            visible.pop(key, None)
        return ("Render ONE opening frame, exactly the state_at_shot_start. It is not the whole action, "
                "a montage, or the ending. Follow the supplied framing, lighting, staging and locked references. "
                "characters_in_shot lists ONLY characters visible in this opening; do not add later arrivals. "
                "The ad takeaway and shot purpose explain attention, not extra objects to insert. "
                "Show only products/props present in the opening state. Preserve the real markings on any "
                "approved product reference; invent no captions, logos, claims or additional subjects.\n"
                + ("\nSpatial requirements are hard acceptance criteria:\n" + "\n".join(spatial) if spatial else "")
                + "\n" + json.dumps(visible, ensure_ascii=False))
    return ("Depict the opening physical instant of this approved shot. Preserve its subject, framing, "
            "style and locked identity facts. state_at_shot_start is the authoritative instant; "
            "do not advance the action. If no start state is supplied, freeze the beginning of the description. "
            "No collage, invented captions, invented logos or additional subjects. Preserve existing printed "
            "text and logos ONLY on products supplied as approved product image references.\n"
            + ("\nSpatial requirements are hard acceptance criteria:\n" + "\n".join(spatial) if spatial else "")
            + "\n" + json.dumps(visible, ensure_ascii=False))
