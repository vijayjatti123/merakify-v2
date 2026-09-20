"""Deterministic scene/identity reference contract shared by video adapters."""
import re
from urllib.parse import urlsplit, urlunsplit


def identity(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def shot_text(shot):
    # Never match entities against global style prose or the compiled appendix.
    fields = ("description", "state_at_shot_start", "state_at_shot_end", "composition_note")
    return " ".join(str(shot.get(k) or "") for k in fields) + " " + str((shot.get("shot_direction") or {}).get("product_props") or "")


def mentions(text, name):
    words = re.findall(r"\w+", name.casefold())
    haystack = " " + " ".join(re.findall(r"\w+", text.casefold())) + " "
    return bool(words and " " + " ".join(words) + " " in haystack)


def build(result, shot, *, limit, tag_style, refresh):
    if not shot.get("still_frame_url"):
        raise ValueError("An accepted scene preview is required; a portrait cannot replace it")
    required = [("scene", None, "Approved opening scene: preserve composition, environment and initial physical state", shot["still_frame_url"])]
    optional, warnings = [], []
    cast = list(dict.fromkeys(shot.get("characters_in_shot", [])))
    opening = shot.get("opening_characters", cast)
    for name in cast:
        matches = [c for c in result.get("continuity", {}).get("characters", []) if c.get("name", "").casefold() == name.casefold()]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous character binding for {name}; resolve the character ID before rendering")
        character = matches[0] if matches else {}
        cid = character.get("character_id")
        url = character.get("image_url")
        if cid and not url:
            raise ValueError(f"Missing approved identity image for {name}")
        if name not in opening and not url:
            raise ValueError(f"{name} enters after the opening but has no identity reference; establish an approved reference first")
        if not url:
            continue  # Legacy/invented cast can already be established in the preview.
        role = f"{name} identity only: face, hair, clothing; not a new subject or scene layout"
        if name not in opening:
            role += "; enters later, do not add to the opening before the directed entrance"
        required.append(("character", cid or name, role, url))
        sheet = character.get("reference_sheet_url")
        if sheet and not character.get("style_variant_id"):
            optional.append(("character_views", cid or name,
                f"Alternate views of the SAME {name}; one person, not duplicates. Identity guide only; never reproduce the sheet layout", sheet))
        elif sheet:
            warnings.append(f"{name}: base character views omitted because they are not verified for this style variant")
    text = shot_text(shot)
    for product in shot.get("approved_product_references", []):
        if not all(product.get(k) for k in ("name", "product_id", "object_key")):
            raise ValueError("Approved product reference is incomplete; refresh the job before rendering")
        if mentions(text, product["name"]):
            from app.services import storage_service
            required.append(("product", product["product_id"],
                f"{product['name']} packaging identity only: preserve geometry, colors and existing lettering/logos; use only at the directed time and position",
                storage_service.asset_url(product["object_key"])))
            from app.services.product_album_service import relevant_views
            for view in relevant_views(product, shot):
                optional.append(("product_view", view["id"],
                    f"{product['name']} approved {view['angle']} view of the SAME product. Identity/detail guide only; never copy its white background or duplicate the product",
                    storage_service.asset_url(view["object_key"])))
    # Continuity props remain optional. Match ONLY this shot's own facts.
    from app.services.still_frame_service import match_entities
    needed = match_entities(result, shot, text)
    for key, ref in result.get("entity_references", {}).items():
        if key in needed and ref.get("url"):
            optional.append(("entity", key, f"Job entity {key} only; ignore other people, background layout and action in this reference", ref["url"]))
    entries, lookup = [], {}
    for necessary, items in ((True, required), (False, optional)):
        for kind, asset_id, role, url in items:
            key = identity(url)
            if key in lookup:
                entries[lookup[key]]["roles"].append({"kind": kind, "id": asset_id, "instruction": role})
                continue
            if len(entries) >= limit:
                if necessary:
                    raise ValueError(f"This shot needs more than {limit} required reference images; split the shot or reduce its cast/products")
                warnings.append(f"Reference limit: omitted optional {kind} {asset_id}")
                continue
            lookup[key] = len(entries)
            entries.append({"source": key, "url": refresh(url), "roles": [{"kind": kind, "id": asset_id, "instruction": role}]})
    for index, entry in enumerate(entries):
        entry["tag"] = f"Image {index+1}" if tag_style == "h3" else f"<IMAGE_REF_{index}>" if tag_style == "omni" else f"@{'Image' if tag_style == 'fal' else 'image'}{index+1}"
    instructions = "Reference roles (identity views never introduce extra people):\n" + "\n".join(
        f"{e['tag']}: " + "; ".join(r["instruction"] for r in e["roles"]) + "." for e in entries)
    return {"images": [e["url"] for e in entries], "instructions": instructions,
            "manifest": [{k: v for k, v in e.items() if k != "url"} for e in entries], "warnings": warnings,
            "lookup": {e["source"]: e["tag"] for e in entries}}


def check_prompt(prompt, manifest, *, omni=False):
    tags = set(re.findall(r"<IMAGE_REF_\d+>|@(?:Image|image)\d+|\bImage \d+\b", prompt))
    if tags - {e["tag"] for e in manifest}:
        raise ValueError("Video instructions contain an unbound image reference tag")
    if omni and len(prompt) > 20000:
        raise ValueError("Omni video instructions exceed 20,000 characters; shorten the shot instructions")
