"""Deterministic still-render adapter for the authoritative approved shot plan."""
import json
from urllib.parse import urlsplit, urlunsplit

STILL_CONTRACT_VERSION = "shot-opening-v2"
IMAGE_POLISH_VERSION = "vague-to-polished-image-v1"
STILL_CHECKS = ("identity_wardrobe", "placement_support", "props_contact",
                "opening_state", "framing", "lighting_style")

IMAGE_PRESETS = {
    "product": "Premium product-commercial opening frame: product geometry, packaging, label and material finish are hero details.",
    "cgi": "Designed CGI-commercial opening frame: establish real scale, surfaces and lighting before any transformation or spectacle.",
    "ugc": "Credible creator-style opening frame: natural lived-in setting, believable available light and feed-native composition.",
    "character": "Cinematic character-commercial opening frame: readable identity, physical performance, environment and motivated light.",
}


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
    from app.commercial import OPENING_REQUIREMENTS
    ad_type = result.get("ad_type", result.get("script", {}).get("production_context", {}).get("commercial", {}).get("ad_type", "character"))
    facts["ad_type"] = ad_type
    if ad_type in OPENING_REQUIREMENTS:
        facts["commercial_opening_requirements"] = OPENING_REQUIREMENTS[ad_type]
    facts["aspect_ratio"] = result.get("aspect_ratio", "16:9")
    if shot.get("direction_version") == 1:
        from app.services.ad_direction import visual_direction
        facts['direction_version'] = 1
        direction = visual_direction(result)
        facts['ad_visual_direction'] = {'visual_approach': direction['visual_approach']} if direction else None
        facts['characters_in_shot'] = opening_cast(shot)
        # state_at_shot_start is deliberately the sole physical staging source
        # for the still. shot_direction describes the complete performance;
        # importing its later support/contact or outcome into the opening image
        # created impossible contracts. Performance and ending stay in video.
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
    spatial = ["Use state_at_shot_start as the complete physical truth for this opening image. Follow every stated inside/outside position, support surface, relative position and hand/object contact. Show enough surrounding geometry to prove those relationships. A camera looking out through a doorway must not relocate an inside person into the exterior. An explicitly airborne or outside person must remain outside."]
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


def _requirement(value, fallback):
    value = str(value or "").strip()
    return value if value else fallback


def visual_contract(facts, visual=None):
    """Deterministic contract created before pixels and shared with their checker."""
    opening = _requirement(
        facts.get("state_at_shot_start"),
        "Freeze the beginning of this approved description: " + _requirement(facts.get("description"), "No opening action specified."),
    )
    characters = facts.get("characters") or []
    identities = "; ".join(
        f"{c.get('name')}: {_requirement(c.get('description'), 'match the supplied locked reference')}"
        for c in characters if c.get("name")
    )
    framing = "; ".join(str(value).strip() for value in (
        facts.get("camera_angle"), facts.get("lens"),
        None if facts.get("state_at_shot_start") else facts.get("composition_note"),
    ) if value)
    style = json.dumps({
        "lighting": facts.get("lighting"),
        "visual_style": facts.get("visual_style"),
        "ad_visual_direction": facts.get("ad_visual_direction"),
    }, ensure_ascii=False, sort_keys=True)
    checks = {
        "identity_wardrobe": ("Opening-frame visible characters: " + identities if identities else
                              "No named character identity or wardrobe requirement in this opening frame."),
        "placement_support": (opening + " Treat only this opening state as physical truth. Preserve every "
                              "explicit inside/outside relationship, support surface, relative position and "
                              "physical contact stated there; show enough environment to prove containment."),
        "props_contact": (opening + " Preserve every explicitly stated prop owner, hand/object contact and product "
                          "presence. Do not add a prop, product or contact that belongs only to later action."),
        "opening_state": ("Render exactly this first physical instant, before any later action or reveal: " + opening),
        "framing": _requirement(framing, "Use the approved framing in the source visual without inventing a new camera angle."),
        "lighting_style": ("Match this approved lighting and rendering direction: " + style),
    }
    contract = {
        "contract_version": STILL_CONTRACT_VERSION,
        "source_visual": visual if visual is not None else preview_visual(facts),
        "checks": checks,
        "global_rules": [
            "Generate one opening-frame image, never a montage, collage, end state or later completed action.",
            "Show only opening-frame characters, products and props; add no unrequested subject, caption, sign or logo.",
            "Each labeled image reference locks only that named character, product or entity; never copy its old composition.",
            "Approved product references lock geometry, packaging, color, logo and existing printed text; invent no claims or markings.",
        ],
    }
    contract["generation_brief"] = polished_generation_brief(facts, contract)
    return contract


def polished_generation_brief(facts, contract):
    """Vague-to-polished layers, deterministically grounded in approved facts."""
    ad_type = str(facts.get("ad_type") or "character").strip().casefold()
    preset = IMAGE_PRESETS.get(ad_type, IMAGE_PRESETS["character"])
    return {
        "version": IMAGE_POLISH_VERSION,
        "role": "Commercial still photographer and production designer executing an already-approved Director plan.",
        "preset": preset,
        "format": f"One {facts.get('aspect_ratio') or '16:9'} opening frame; one coherent camera view; no sequence or montage.",
        "subject_identity": contract["checks"]["identity_wardrobe"],
        "opening_moment": contract["checks"]["opening_state"],
        "physical_staging": contract["checks"]["placement_support"],
        "props_and_product": contract["checks"]["props_contact"],
        "camera": contract["checks"]["framing"],
        "lighting_and_finish": contract["checks"]["lighting_style"],
        "reference_direction": "Use every labeled reference only for its named identity or object. Preserve those locked facts while composing this new shot.",
        "avoid": [
            "later action or end state",
            "wrong inside/outside placement",
            "unsupported or floating bodies and objects",
            "duplicated characters or products",
            "copied reference-sheet layout",
            "invented captions, logos, claims or packaging text",
            "collage, split screen or multiple moments",
        ],
    }


def generation_prompt(contract):
    """Readable provider prompt generated solely from the persisted contract."""
    brief = contract.get("generation_brief") or {
        "role": "Commercial still photographer executing an approved shot description.",
        "preset": "Cinematic advertising opening frame with coherent production design and motivated light.",
        "format": "One coherent opening frame; no sequence or montage.",
        "opening_moment": contract.get("source_visual", "Render the approved opening instant."),
        "reference_direction": "Use labeled references only for their named identities or objects; compose a new shot.",
        "avoid": ["later action or end state", "duplicated subjects", "invented text or logos", "collage or split screen"],
    }
    order = ("role", "preset", "format", "subject_identity", "opening_moment",
             "physical_staging", "props_and_product", "camera",
             "lighting_and_finish", "reference_direction")
    labels = {
        "role": "ROLE", "preset": "COMMERCIAL TREATMENT", "format": "DELIVERABLE",
        "subject_identity": "SUBJECT", "opening_moment": "EXACT OPENING MOMENT",
        "physical_staging": "PHYSICAL STAGING", "props_and_product": "PROPS AND PRODUCT",
        "camera": "CAMERA", "lighting_and_finish": "LIGHTING AND FINISH",
        "reference_direction": "REFERENCE USE",
    }
    lines = [f"{labels[key]}: {brief[key]}" for key in order if brief.get(key)]
    if brief.get("avoid"):
        lines.append("AVOID: " + "; ".join(brief["avoid"]))
    return "\n".join(lines)


def contract_text(contract):
    return json.dumps(contract, ensure_ascii=False, sort_keys=True)


def attach_visual_contracts(result):
    """Freeze testable opening-frame requirements when the user approves the plan."""
    for shot in result.get("shots", []):
        facts = preview_input(result, shot)
        if not facts:
            continue
        visual = preview_visual(facts)
        shot["preview_input"] = facts
        shot["still_frame_contract"] = visual_contract(facts, visual)
    return result
