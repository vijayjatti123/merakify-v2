"""One text-only compiler; consumes assembled data without revising upstream shots."""
import copy
import json
import re
import unicodedata
import queue
import threading
import time
from datetime import datetime, timezone
from contextvars import copy_context

from app.agents import prompts

TEXT_GUARD = "No on-screen text, logos or readable signage; composite text in post."
AUDIO_GUARD = "Visual performance only; use the existing dialogue audio file in post."
# Real eight-shot output took 102s; reserve room for one comparable correction.
# Still shorter than three 90s SDK attempts for even one old logical call.
COMPILER_DEADLINE_SEC = 240.0
COMPILER_BATCH_SIZE = 4


def dialogue_insert(source, family):
    """Only application code owns dialogue serialization; never ask an LLM to copy it."""
    if not source.get("has_dialogue"):
        return ""
    dialogue = source.get("dialogue_text")
    if not isinstance(dialogue, str) or not dialogue.strip():
        raise ValueError("Dialogue shot has no source dialogue_text")
    if family == "sora":
        return f'\nDialogue:\n{source["speaker_label"]}: "{dialogue}"'
    return f'Performance reference — {source["speaker_label"]}: "{dialogue}"'


def reference_insert(source):
    """Opaque reference URLs are copied by code, never retyped by the model.

    This preserves the supplied URL exactly; it neither fetches nor renews a
    presigned URL. Refreshing references when media is consumed is not this
    text-only compiler's job.
    """
    refs = [f'{ref["name"]}: {ref["image_url"]}' for ref in source.get("character_references", []) if ref.get("image_url")]
    return "Maintain visual consistency with these reference images — " + "; ".join(refs) + "." if refs else ""


def insert_dialogue(response, payload):
    result = copy.deepcopy(response)
    if not isinstance(result, dict) or not isinstance(result.get("shots"), list):
        return result
    sources = {s["shot_number"]: s for s in payload["shots"]}
    for output in result["shots"]:
        if not isinstance(output, dict):
            continue
        source = sources.get(output.get("shot_number"))
        value = output.get("compiled_prompt")
        if not source or not isinstance(value, str):
            continue
        # Fixed boundary after visual prose. Source bytes/codepoints are untouched.
        visual = value.removesuffix(TEXT_GUARD).rstrip()
        image_reference = reference_insert(source)
        if image_reference:
            visual += " " + image_reference
        reference = dialogue_insert(source, payload["model_family"])
        if not source["has_dialogue"]:
            output["compiled_prompt"] = f"{visual} {TEXT_GUARD}"
        elif payload["model_family"] == "sora":
            output["compiled_prompt"] = f"{visual} {AUDIO_GUARD} {TEXT_GUARD}{reference}"
        else:
            output["compiled_prompt"] = f"{visual} {reference} {AUDIO_GUARD} {TEXT_GUARD}"
    return result


def _start_provider_call(call):
    """Bound orchestration even if the synchronous SDK is still retrying.

    The worker owns only the provider call, never job state or trace persistence.
    Python cannot cancel an in-flight synchronous HTTP call: after expiry its late
    result is discarded. SDK transport limits still govern that daemon worker.
    """
    completed = queue.Queue(maxsize=1)
    def work():
        try:
            completed.put((True, call()))
        except BaseException as error:
            completed.put((False, error))
    context = copy_context()
    threading.Thread(target=lambda: context.run(work), daemon=True, name="shot-compiler-provider").start()
    return completed


def _await_provider(completed, *, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Shot Prompt Compiler overall {COMPILER_DEADLINE_SEC:g}-second deadline exceeded")
    try:
        ok, value = completed.get(timeout=remaining)
    except queue.Empty:
        raise TimeoutError(f"Shot Prompt Compiler overall {COMPILER_DEADLINE_SEC:g}-second deadline exceeded; late provider result discarded") from None
    if not ok:
        raise value
    return value


def _attempt_before_deadline(call, *, deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError(f"Shot Prompt Compiler overall {COMPILER_DEADLINE_SEC:g}-second deadline exceeded")
    return _await_provider(_start_provider_call(call), deadline=deadline)


def _timing(emit, started, attempt_started, deadline, batch_index, numbers, attempt, phase, **extra):
    emit("shot_prompt_compiler", "Compiler attempt timing: " + json.dumps({
        "batch": batch_index, "shot_numbers": numbers, "attempt": attempt,
        "phase": phase, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "attempt_elapsed_sec": round(time.monotonic() - attempt_started, 3),
        "orchestration_elapsed_sec": round(time.monotonic() - started, 3),
        "deadline_sec": COMPILER_DEADLINE_SEC,
        "remaining_budget_sec": round(max(0, deadline - time.monotonic()), 3),
        "sdk_retries": 0, **extra}))
# Optical comparisons, not assertions that a particular camera shot the scene.
HARDWARE_LANGUAGE = {
    "product": "Arri Alexa tonal latitude",
    "character_dialogue": "35mm f/1.4 intimate optical separation",
    "environment": "70mm anamorphic spatial breadth",
    "action": "Arri Alexa tonal latitude",
    "fashion": "85mm portrait optical separation",
    "quiet": "35mm f/1.4 restrained optical separation",
    "specified_lens": "Arri Alexa tonal latitude",
}
MOVES = re.compile(r"\b(?:dolly(?:in|out)?|push(?:[- ]?in)?|pull(?:[- ]?out)?|slide|pan(?:ning)?|tilt(?:ing)?|"
                   r"track(?:ing)?|orbit|crane(?:up|down)?|zoom|hand[- ]?held|static|locked(?:-off)?|fixed)\b", re.I)


def _name(value):
    return unicodedata.normalize("NFC", value).strip().casefold()


def model_family(ai_model):
    for prefix in ("Veo", "Sora", "Kling"):
        if ai_model.startswith(prefix):
            return prefix.lower()
    if ai_model.startswith(("Seedance", "Wan")):
        # No verified special syntax for Seedance/Wan: plain prose is deliberately
        # unverified, pending real-output testing. Module M never calls these models.
        return "generic"
    raise ValueError(f"Shot compiler: unsupported ai_model {ai_model!r}")


def first_movement(value):
    """Reduce compound instructions in compiler input only; never edit the shot."""
    value = str(value or "static").strip()
    parts = re.split(r"\s+(?:then|and then|followed by|while|and|with)\s+|[,;/+&]", value, maxsplit=1, flags=re.I)
    first = parts[0].strip()
    moves = list(MOVES.finditer(first))
    if len(moves) > 1:
        first = first[:moves[1].start()].rstrip(" -")
    if re.search(r"\b(?:static|locked(?:-off)?|fixed)\b", first, re.I):
        first = "static"  # Speed and settling cannot modify a motionless camera.
    return first or "static", first != value


def _category(shot, content_type, visual_style):
    # Only positive rendering directives select anime. A natural bible may say
    # "no stylization or cel-shading"; negative exclusions are not style requests.
    style = str(visual_style.get("rendering", "") if isinstance(visual_style, dict) else visual_style).casefold()
    style = re.sub(r"\b(?:no|not|without|avoid|exclude)\b[^.;]*", "", style)
    if any(s in style for s in ("anime", "cartoon", "cel-shad", "cel shad", "2d animation")):
        return "anime"
    if content_type in {"documentary", "ugc"}:
        return content_type
    text = shot.get("description", "").casefold()
    if re.search(r"\b(fashion|garment|outfit|runway|on-model)\b", text):
        return "fashion"
    if shot.get("has_dialogue"):
        return "character_dialogue"
    if re.search(r"\b(run(?:ner|s|ning)?|jump|leap|impact|land(?:s|ing)|chase|strike)\b", text):
        return "action"
    if content_type == "product hero" or re.search(r"\b(product|bottle|cup|bean|cherry|organizer|jewel|ring|shoe|hero)\b", text):
        return "product"
    return "environment"


def similar_framing(left, right):
    def signature(value):
        value = value.casefold().replace("-", " ")
        scale = next((s for s in ("extreme close", "close", "medium", "wide") if s in value), None)
        angle = next((s for s in ("low", "high", "overhead", "eye") if s in value), None)
        return scale, angle
    a, b = signature(left), signature(right)
    return _name(left) == _name(right) or (a[0] is not None and a[0] == b[0] and (a[1] == b[1] or None in (a[1], b[1])))


def movement_present(movement, prose):
    """Accept grammatical inflection, not only a pasted movement field."""
    if movement == "static":
        return bool(re.search(r"\b(?:static|motionless|unmoving|fixed|locked[- ]off)\b|\b(?:holds?|held|keeps?|kept|stays?|remains?)\s+(?:(?:a|completely|entirely|perfectly)\s+)?still\b", prose, re.I))
    def normalize(text):
        text = text.casefold().replace("-", " ")
        for pattern, replacement in [(r"\bpush(?:es|ing)?\b", "push"), (r"\bpull(?:s|ing)?\b", "pull"),
                                     (r"\bslowly\b", "slow"), (r"\bgently\b", "gentle"),
                                     (r"\bpans?\b|\bpanning\b", "pan"), (r"\btilt(?:s|ing)?\b", "tilt"),
                                     (r"\bdrift(?:s|ing)?\b", "drift"), (r"\bslightly\b", "slight")]:
            text = re.sub(pattern, replacement, text)
        for direction in ("down", "up", "in", "out"):
            text = re.sub(r"\b" + direction + r"wards?\b", direction, text)
        return text
    expected = normalize(movement).split()
    actual = normalize(prose).split()
    # Keep all supplied direction/speed words together in a short local phrase.
    return any(all(word in " ".join(actual[i:i+len(expected)+5]).split() for word in expected)
               for i in range(len(actual)))


def hardware_reference(language):
    """Preserve the named hardware, not a compulsory repeated descriptive tag."""
    for reference in ("Arri Alexa", "70mm anamorphic", "35mm f/1.4", "85mm portrait"):
        if language and reference in language:
            return reference
    return language


def compiler_input(result, *, brief="", emit):
    assembly = result.get("assembly", {})
    if assembly.get("provisional"):
        raise ValueError("Shot compiler requires real, completed Assembly transitions")
    shots = result["shots"]
    numbers = [s["shot_number"] for s in shots]
    if len(set(numbers)) != len(numbers):
        raise ValueError("Shot compiler requires unique shot numbers")
    continuity = result.get("continuity", {})
    bible = continuity.get("visual_style")
    if not isinstance(bible, dict) or not bible.get("rendering"):
        raise ValueError("Shot compiler requires continuity.visual_style; replan this historical job")
    family = model_family(result["ai_model"])
    selected = re.findall(r"Content type:\s*([^\n.]+)\.", brief, re.I)
    content_type = (selected[-1] if selected else result.get("format", {}).get("format", "")).strip().casefold()
    characters = {_name(c["name"]): c for c in continuity.get("characters", [])}
    scenes = {s["scene_number"]: s for s in result.get("script", {}).get("scenes", [])}
    boundaries = []
    by_boundary = {t["between"]: t for t in assembly.get("transitions", [])}
    for left, right in zip(shots, shots[1:]):
        key = f'{left["shot_number"]}-{right["shot_number"]}'
        if key not in by_boundary:
            raise ValueError(f"Shot compiler missing Assembler boundary {key}; will not invent a cut")
        boundary = copy.deepcopy(by_boundary[key])
        if boundary["type"] == "cut" and similar_framing(left.get("camera_angle", ""), right.get("camera_angle", "")):
            boundary["similar_framing_risk"] = True
            emit("shot_prompt_compiler", f"Hard-cut similarity risk at {key}: preserving Cinematography; avoid duplicated incidental details.")
        boundaries.append(boundary)
    inputs = []
    continuing_speaker = None
    for shot in shots:
        item = {k: shot.get(k) for k in ("shot_number", "scene_number", "camera_angle", "lens", "lighting",
                                        "composition_note", "description", "dialogue_text", "has_dialogue", "characters_in_shot")}
        move, reduced = first_movement(shot.get("camera_movement"))
        item["camera_movement"] = move
        if not str(shot.get("camera_movement") or "").strip():
            emit("shot_prompt_compiler", f"Shot {shot['shot_number']}: source camera_movement missing; using static for compiler input only.")
        if reduced:
            emit("shot_prompt_compiler", f"Warning: shot {shot['shot_number']} compound movement reduced for compiled text only: {shot.get('camera_movement')!r} -> {move!r}.")
        refs = []
        for name in shot.get("characters_in_shot", []):
            if _name(name) not in characters:
                raise ValueError(f"Shot compiler cannot resolve character {name!r}")
            refs.append({k: characters[_name(name)].get(k) for k in ("name", "description", "gender", "character_id", "image_url", "voice_id")})
        item["character_references"] = refs
        item["subject_anchor"] = refs[0]["name"] if refs else shot.get("description", "").rstrip(".!?। ")
        # An upstream cast tag is not necessarily the visible foreground subject:
        # "Close on cup resting on table" must open on the cup, not its owner.
        focus = re.match(r"(?:close(?:[- ]up)?|detail|insert|macro)(?:\s+(?:on|of))?\s+(.+)",
                         shot.get("description", ""), re.I)
        if focus:
            focused_noun = re.sub(r"^(?:(?:reveals?|shows?|frames?|isolates?|insert|detail|view|shot|of|on)\s+)+", "", focus[1], flags=re.I)
            focused_noun = re.split(r",|\s+(?:resting|sitting|standing|on|in|at|with|as|while)\b", focused_noun, maxsplit=1, flags=re.I)[0].strip().rstrip(".!?।")
            if focused_noun:
                item["subject_anchor"] = focused_noun
                item["foreground_insert"] = True
        if not item["subject_anchor"]:
            raise ValueError("Shot compiler needs an existing subject description")
        # Establish speech from a sole dialogue performer, not a silent bystander.
        # Carry it across insert shots and batch boundaries without adding an
        # off-camera person to characters_in_shot or changing upstream audio.
        if shot.get("has_dialogue") and refs:
            continuing_speaker = refs[0] if len(refs) == 1 else {"name": "Dialogue performer", "gender": None}
        speaker = refs[0] if len(refs) == 1 else continuing_speaker if not refs else None
        item["speaker_label"] = speaker["name"] if speaker else "Dialogue performer" if refs else "Narrator"
        item["speaker_reference"] = copy.deepcopy(speaker)
        identity_refs = refs + ([speaker] if speaker and not refs else [])
        # Unknown classification never licenses a gender guess. In mixed scenes
        # use neutral prose throughout rather than guess a pronoun's antecedent.
        item["neutral_pronouns_required"] = not identity_refs or any(
            ref.get("gender") not in ("female", "male") for ref in identity_refs)
        scene = scenes.get(shot.get("scene_number"), {})
        item["scene_context"] = {k: scene.get(k) for k in ("heading", "description", "mood")}
        category = _category(shot, content_type, bible)
        item["category"] = category
        if category == "anime" and re.search(r"hand[- ]?held|shake", item["camera_movement"], re.I):
            item["camera_movement"] = "static"
            emit("shot_prompt_compiler", f"Shot {shot['shot_number']}: anime rendering uses a static drawn viewpoint instead of live-action handheld shake; upstream camera data preserved.")
        lens = str(shot.get("lens") or "")
        generic_lens = not lens or bool(re.fullmatch(r"(?:wide|normal|long|telephoto|macro)(?: lens)?", lens, re.I))
        mood = scene.get("mood")
        item["hardware_language"] = None
        if category not in {"anime", "ugc", "documentary"}:
            # A named rendering comparison also works with a specific supplied
            # lens, without changing its focal length or depth of field.
            item["hardware_language"] = HARDWARE_LANGUAGE.get(category, HARDWARE_LANGUAGE["quiet"]) if generic_lens else HARDWARE_LANGUAGE["specified_lens"]
            emit("shot_prompt_compiler", f"Shot {shot['shot_number']} hardware lookup selected {item['hardware_language']!r} for {category}, mood={mood!r}; required in compiled text.")
        item["hardware_reference"] = hardware_reference(item["hardware_language"])
        # Only already-tagged shot data can establish a product reference. A generic
        # brief image URL or unrelated vault asset is not an invented product linkage.
        item["tagged_product_references"] = [copy.deepcopy(a) for a in shot.get("reference_assets", []) if a.get("role") == "product"]
        inputs.append(item)
    return {"ai_model": result["ai_model"], "model_family": family, "content_type": content_type,
            "format": result.get("format"), "style_bible": bible, "multi_shot_context": len(shots) > 1,
            "locations": continuity.get("locations", []), "props": continuity.get("props", []),
            "boundaries": boundaries, "shots": inputs}


_LIGHT_SOURCE = re.compile(
    r"\b(?:(?:transparent|translucent|opaque)[, ]+)?(?:(?:the|a|an|same)\s+){0,2}"
    r"(?:(?:soft|hard|diffused|gentle|warm|cool|white|amber|blue|red)\s+)*"
    r"(?P<role>key|fill|rim|backlight)(?:\s+light)?\b", re.I)
_LIGHT_POSITION = re.compile(r"\b(?:behind|below|above|upper|lower|left|right|front|rear|overhead|underneath|side)\b", re.I)
# A deliberately limited physical vocabulary, not a blanket exemption for any
# sentence mentioning light. Hardware explanations and creative prose still pass
# through the ordinary repetition checks. Unsupported wording stays conservative.
_LIGHT_PHYSICAL_WORDS = set("""a an the same its and or of to from at in on by with
as so that while it is are stays remains sits placed positioned comes glows shines
falls travels passes sends keeps keeping lets letting through across into outward
outwards within off behind below above upper lower left right front rear overhead
underneath side key fill rim backlight light lighting source soft hard diffused
gentle warm cool white amber blue red pale neutral transparent translucent opaque
glass liquid soda tea water milk cream cup bottle pitcher surface face near far
edge base rim visible readable shadow shadows reflection reflections reflected
reflects transmission transmits transmitted glow color colour highlights highlight
illuminates illuminated bright dim low high
""".split())


def _lighting_setup_phrases(prose):
    """Recognize literal physical setup spans; do not infer general prose semantics.

    A repeated eight-word window must lie wholly inside a positioned light-source
    clause and contain only physical setup words. Role, position and supplied color/
    softness modifiers must match too. If a window also occurs outside a recognized
    setup, it remains subject to the ordinary repetition check.
    """
    tokens = list(re.finditer(r"\w+", prose.casefold()))
    sources = list(_LIGHT_SOURCE.finditer(prose))
    spans = []
    for index, source in enumerate(sources):
        end = sources[index + 1].start() if index + 1 < len(sources) else len(prose)
        punctuation = re.search(r"[.!?;]", prose[source.end():end])
        if punctuation:
            end = source.end() + punctuation.start()
        clause = prose[source.start():end].casefold()
        setup = re.split(r"\b(?:so|while|with|keeping|letting)\b", clause, maxsplit=1)[0]
        positions = tuple(sorted(set(_LIGHT_POSITION.findall(setup))))
        modifiers = tuple(sorted(set(re.findall(
            r"\b(?:soft|hard|diffused|gentle|warm|cool|white|amber|blue|red)\b", setup))))
        if positions and not re.search(r"\b(?:not|no|instead|moves|moving|switches|changes)\b", setup):
            spans.append((source.start(), end, (source['role'].casefold(), positions, modifiers)))
    contexts = {}
    for index in range(len(tokens) - 7):
        window = tokens[index:index + 8]
        phrase = tuple(token.group() for token in window)
        signatures = {signature for start, end, signature in spans
                      if start <= window[0].start() and window[-1].end() <= end
                      and set(phrase) <= _LIGHT_PHYSICAL_WORDS}
        contexts[phrase] = contexts[phrase] & signatures if phrase in contexts else signatures
    return contexts


def validate_compiled(response, payload):
    """Deterministic checks plus a bounded compiler retry; no upstream QA changes.

    These checks cannot prove arbitrary prose is factually entailed. The compiler's
    grounding rules and real-output review remain necessary, not a claimed oracle.
    """
    errors = []
    generated = response.get("shots") if isinstance(response, dict) else None
    if not isinstance(generated, list) or [s.get("shot_number") for s in generated if isinstance(s, dict)] != [s["shot_number"] for s in payload["shots"]]:
        return ["Return every input shot exactly once in its original order"]
    for source, output in zip(payload["shots"], generated):
        number = source["shot_number"]
        value = output.get("compiled_prompt")
        if set(output) != {"shot_number", "compiled_prompt"} or not isinstance(value, str):
            errors.append(f"Shot {number}: return only shot_number and compiled_prompt string")
            continue
        problems = []
        words = value.split()
        if not 100 <= len(words) <= 150:
            problems.append(f"word count {len(words)}; requires 100-150")
        opening_tokens = set(re.findall(r"\w+", " ".join(words[:30]).casefold()))
        anchor_tokens = set(re.findall(r"\w+", source["subject_anchor"].casefold())) - {"a", "an", "the"}
        # Subject facts may be separated by articles or grounded modifiers:
        # "clear bottle" -> "clear water bottle", without requiring pasted prose.
        if (source["character_references"] or source.get("foreground_insert")) and not anchor_tokens <= opening_tokens:
            problems.append("subject_anchor missing within first 30 words")
        if "Render style:" not in value or value.index("Render style:") > len(" ".join(words[:60])):
            problems.append("early Render style sentence missing")
        if TEXT_GUARD not in value:
            problems.append("exact no-text guard missing")
        for ref in source["character_references"]:
            for side in ("left", "right"):
                if re.search(re.escape(ref["name"]) + r"\s+(?:screen[- ])?" + side + r"\b", source.get("composition_note") or "", re.I) and not re.search(r"\b" + side + r"\b", value, re.I):
                    problems.append(f"preserve {ref['name']}'s supplied {side} composition placement")
            description = ref.get("description") or ""
            if len(description.split()) >= 6 and description.rstrip(".!? ").casefold() in value.casefold():
                problems.append("stored description pasted verbatim; integrate identical facts in fresh grammar")
            if ref.get("character_id") or ref.get("image_url"):
                for field in ("name", "image_url"):
                    if ref.get(field) and ref[field] not in value:
                        problems.append(f"locked {field} missing or altered for {ref['name']}")
                if ref.get("image_url") and not re.search(r"consisten\w*.*reference|reference.*consisten\w*", value, re.I):
                    problems.append("explicit visual reference consistency missing")
        if not movement_present(source["camera_movement"], re.sub(r"[.,;:]", " ", value)):
            problems.append("supplied single camera movement missing")
        if source.get("hardware_language") and hardware_reference(source["hardware_language"]) not in value:
            problems.append("mandatory selected hardware_language reference missing")
        dialogue = source.get("dialogue_text") or ""
        if payload["model_family"] != "sora" and "\nDialogue:" in value:
            problems.append("Dialogue block belongs only to Sora")
        if not source["has_dialogue"] and "\nDialogue:" in value:
            problems.append("silent shot cannot acquire dialogue")
        if payload["model_family"] == "kling" and payload["multi_shot_context"]:
            if not value.startswith(f"[Shot {number}:"):
                problems.append("Kling shot label missing")
        elif re.search(r"\[Shot \d", value):
            problems.append("shot brackets belong only to Kling multi-shot context")
        # Inspect instructions outside verbatim quoted source and reference URLs.
        prose = value.replace(TEXT_GUARD, "").replace(AUDIO_GUARD, "")
        prose = prose.replace(reference_insert(source), "") if reference_insert(source) else prose
        if source["has_dialogue"]:
            prose = prose.replace(dialogue_insert(source, payload["model_family"]), "")
        prose = re.sub(r"https?://\S+", "", prose)
        if source.get("neutral_pronouns_required") and re.search(
                r"\b(?:he|him|his|himself|she|her|hers|herself)\b", prose, re.I):
            problems.append("unsupported gendered pronoun; use the character name, role, or they/them because gender is not concrete")
        if re.search(r"\b(?:slow(?:ly)?|fast|gentle)\s+static\b|\bstatic\s+(?:push|settle|pan)\b", prose, re.I):
            problems.append("contradictory static camera pacing")
        if re.search(r"push[- ]?in|pushes in", source["camera_movement"], re.I) and re.search(r"\b(?:frame|view|framing)\s+(?:widens|expands|opens out)\b", prose, re.I):
            problems.append("push-in cannot widen the frame; describe tighter coverage")
        if source["category"] == "ugc":
            if re.search(r"\beye[- ]level\b|\b(?:medium[- ]wide|wide|medium)\s+(?:shot|frame|framing)\b", prose, re.I):
                problems.append("UGC conventional framing prohibited; substitute phone-native vocabulary")
            if not re.search(r"arm['’]s[-\s]length|selfie[-\s]angle|handheld[-\s]phone[-\s]framing", prose, re.I):
                problems.append("UGC requires arm's-length, selfie angle or handheld phone framing")
        # Quoted speech and URLs are not camera instructions or visual sentences.
        sentences = re.findall(r"[^.!?।]+[.!?।](?:\s|$)", re.sub(r'"[^"\n]*"', "reference", prose))
        # The two mandatory guard sentences were removed above; add them back.
        sentence_count = len(sentences) + 1 + int(bool(source["has_dialogue"])) + int(bool(reference_insert(source)))
        if not 3 <= sentence_count <= 6:
            problems.append(f"sentence count {sentence_count}; requires roughly 3-6")
        if re.search(r"\bcamera\b[^.!?;]{0,35}\b(?:then|followed by|while)\b[^.!?;]{0,25}" + MOVES.pattern, prose, re.I):
            problems.append("compound camera instruction prohibited")
        if re.search(r"\b(?:generate|synthesize|lip[- ]?sync)\b.{0,35}\b(?:audio|speech|dialogue|voice)\b", prose, re.I):
            problems.append("audio generation/lip-sync instruction prohibited")
        if re.search(r"\b(?:render|display|write|overlay|add)\b.{0,25}\b(?:logo|signage|subtitle|on-screen text)\b", prose, re.I):
            problems.append("positive on-screen text instruction prohibited")
        if source["category"] == "anime":
            if "no photorealism" not in value.casefold() or re.search(r"film grain|lens flare|camera shake|Arri Alexa|70mm anamorphic", prose, re.I):
                problems.append("anime rendering/negative constraints violated")
        if problems:
            errors.append(f"Shot {number}: " + "; ".join(problems))
    by_number = {s["shot_number"]: s.get("compiled_prompt", "") for s in generated}
    # Catch copied descriptive clauses across shots, excluding immutable strings.
    # Short factual anchors can repeat; semantic equivalence still needs review.
    seen = {}
    hardware_seen = {}
    lighting_group = 0
    previous_lighting = None
    for source, output in zip(payload["shots"], generated):
        # Only an uninterrupted run of the same scene AND identical source lighting
        # can reuse setup phrasing. No exemption for missing metadata, scene changes,
        # intervening lighting changes, or an explicitly time-shifting transition.
        lighting = tuple(re.findall(r"\w+", (source.get("lighting") or "").casefold()))
        lighting_key = (source.get("scene_number"), lighting,
                        (source.get("scene_context") or {}).get("heading"))
        time_jump = any(b['between'].split('-')[-1] == str(source['shot_number']) and
                        re.search(r"later|time[- ]?(?:jump|passage)|flashback|next day", b.get('reason', ''), re.I)
                        for b in payload['boundaries'])
        if lighting_key != previous_lighting or not lighting or lighting_key[0] is None or time_jump:
            lighting_group += 1
        previous_lighting = lighting_key
        prose = output.get("compiled_prompt", "")
        if reference_insert(source):
            prose = prose.replace(reference_insert(source), "")
        if source["has_dialogue"]:
            prose = prose.replace(dialogue_insert(source, payload["model_family"]), "")
        reference = hardware_reference(source.get("hardware_language"))
        if reference:
            # Proper hardware identifiers may recur. The explanation around them
            # gets its own shorter check: the old blanket exemption hid copied tags.
            technical = prose.replace(TEXT_GUARD, "").replace(AUDIO_GUARD, "")
            technical = re.sub(re.escape(reference), " hardwareanchor ", technical, flags=re.I)
            technical = re.sub(r"['’]s\b", "", technical)
            tokens = re.findall(r"\w+", technical.casefold())
            stop = {"the", "a", "an", "of", "to", "with", "as", "and", "s", "like", "its", "in", "on"}
            phrases = {tuple(tokens[i:i+3]) for i in range(len(tokens)-2)
                       if "hardwareanchor" in tokens[i:i+3]
                       and not any(t in stop for t in tokens[i:i+3])}
            repeats = phrases & hardware_seen.keys()
            if repeats:
                phrase = " ".join(sorted(repeats)[0]).replace("hardwareanchor", reference)
                errors.append(f"Shot {source['shot_number']}: repeated hardware phrasing: {phrase}; keep the hardware identity, vary its grounded optical explanation")
            for phrase in phrases:
                hardware_seen[phrase] = source["shot_number"]
        for literal in (TEXT_GUARD, AUDIO_GUARD, source.get("dialogue_text") or "", reference or ""):
            if literal:
                prose = prose.replace(literal, " ")
        prose = re.sub(r"https?://\S+|\[Shot[^\]]*\]", " ", prose)
        # Reference-consistency instructions are technical guards, not descriptive
        # boilerplate; exclude their fixed introductory wording, just like URLs.
        prose = re.sub(r"(?:maintain(?:ing)?|preserv(?:e|ing)) visual consistency with (?:the supplied |the |this )?reference(?: image)?(?: at)?", " ", prose, flags=re.I)
        words = re.findall(r"\w+", prose.casefold())
        phrases = {tuple(words[i:i+8]) for i in range(len(words)-7)}
        setups = _lighting_setup_phrases(prose)
        repeats = [phrase for phrase in phrases if phrase in seen and not all(
            group == lighting_group and signatures & setups.get(phrase, set())
            for group, signatures in seen[phrase])]
        if repeats:
            errors.append(f"Shot {source['shot_number']}: repeated descriptive clause; vary phrasing: {' '.join(sorted(repeats)[0])}")
        for phrase in phrases:
            seen.setdefault(phrase, []).append((lighting_group, setups.get(phrase, set())))
    for boundary in payload["boundaries"]:
        if boundary["type"] == "match cut":
            left, right = (int(n) for n in boundary["between"].split("-"))
            left_visual = by_number[left].split("\nDialogue:")[0].replace(TEXT_GUARD, "").replace(AUDIO_GUARD, "")
            left_source = next(s for s in payload["shots"] if s["shot_number"] == left)
            if reference_insert(left_source):
                left_visual = left_visual.replace(reference_insert(left_source), "")
            left_visual = re.sub(r"https?://\S+", "", left_visual)
            tail = " ".join(re.split(r"[.!?]\s+", left_visual.strip())[-2:])
            opening = re.split(r"[.!?]\s+", by_number[right])[0]
            stop = set("the a an and or in on at to of with as its it her his their this that same shot frame camera from into is stays remains holds".split())
            shared = (set(re.findall(r"\w+", tail.casefold())) & set(re.findall(r"\w+", opening.casefold()))) - stop
            # Natural final action can establish the ending without literally
            # saying "ending". Still require an explicit opening and shared detail.
            ending_present = bool(re.search(r"\b(?:end(?:s|ing|ed)?|finish(?:es|ing|ed)?|clos(?:e[sd]?|ing))\b", by_number[left], re.I)) or len(shared) >= 2
            if not ending_present or not re.search(r"\b(?:open(?:s|ing|ed)?|begin(?:s|ning)?|start(?:s|ing|ed)?)\b", by_number[right], re.I):
                errors.append(f"Match cut {boundary['between']}: explicitly coordinate left ending and right opening")
    return errors


def shot_batches(payload):
    """At most four targets per call; keep a boundary pair together when possible."""
    shots = payload["shots"]
    matches = {b["between"] for b in payload["boundaries"] if b["type"] == "match cut"}
    start = 0
    while start < len(shots):
        end = min(start + COMPILER_BATCH_SIZE, len(shots))
        if end < len(shots) and end - start == 4 and f'{shots[end-1]["shot_number"]}-{shots[end]["shot_number"]}' in matches:
            end -= 1
        yield shots[start:end]
        start = end


def compile_shot_prompts(result, *, brief="", emit, call_agent):
    started = time.monotonic()
    deadline = started + COMPILER_DEADLINE_SEC
    payload = compiler_input(result, brief=brief, emit=emit)
    groups = list(shot_batches(payload))
    emit("shot_prompt_compiler", f"Compiling {len(payload['shots'])} shots in {len(groups)} batches of at most {COMPILER_BATCH_SIZE} after real Assembly; text only, {payload['model_family']} syntax.")
    model_input = copy.deepcopy(payload)
    for source, item in zip(payload["shots"], model_input["shots"]):
        reference = dialogue_insert(source, payload["model_family"])
        reserved = (len((reference + " " + AUDIO_GUARD).split()) if reference else 0) + len(reference_insert(source).split())
        for ref in item["character_references"]:
            ref["has_image_reference"] = bool(ref.pop("image_url", None))
        if item.get("speaker_reference"):
            item["speaker_reference"].pop("image_url", None)
        item.pop("dialogue_text", None)
        item["visual_word_target"] = [120 - reserved, 130 - reserved]
        item["visual_word_range"] = [max(1, 100 - reserved), 150 - reserved]
        item["programmatic_reserved_words"] = reserved
        item["visual_sentence_max"] = 6 - int(bool(reference)) - int(bool(reference_insert(source)))
    raw_done, rendered_done, pending = [], [], {}
    def input_for(index):
        group = groups[index-1]
        numbers = {s["shot_number"] for s in group}
        touching = [b for b in payload["boundaries"] if set(map(int, b["between"].split("-"))) & numbers]
        neighbor_numbers = {n for b in touching for n in map(int, b["between"].split("-"))} - numbers
        return {**model_input, "shots": [s for s in model_input["shots"] if s["shot_number"] in numbers],
                "batch_index": index, "batch_count": len(groups), "boundaries": touching,
                "readonly_neighbors": [s for s in model_input["shots"] if s["shot_number"] in neighbor_numbers],
                "prior_compiled_shots": list(raw_done)}
    try:
        for batch_index, group in enumerate(groups, 1):
            # One-batch lookahead overlaps provider latency only. Validation stays
            # ordered, with full accepted-prefix context on any corrective call.
            for index in (batch_index, batch_index + 1):
                if index > len(groups) or index in pending:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Shot Prompt Compiler overall {COMPILER_DEADLINE_SEC:g}-second deadline exceeded")
                attempt_started = time.monotonic()
                numbers = [s["shot_number"] for s in groups[index-1]]
                _timing(emit, started, attempt_started, deadline, index, numbers, 1, "start")
                body = json.dumps(input_for(index), ensure_ascii=False)
                budget = max(0.001, deadline - time.monotonic())
                pending[index] = {"started": attempt_started, "claimed": False,
                                  "completed": _start_provider_call(lambda body=body, budget=budget: call_agent(
                                      prompts.SHOT_PROMPT_COMPILER, body, max_tokens=16000, request_timeout=budget))}
            numbers = {s["shot_number"] for s in group}
            prefix_numbers = {s["shot_number"] for s in rendered_done} | numbers
            targets = {**payload, "shots": group}
            validation_payload = {**payload, "shots": [s for s in payload["shots"] if s["shot_number"] in prefix_numbers],
                                  "boundaries": [b for b in payload["boundaries"] if set(map(int, b["between"].split("-"))) <= prefix_numbers]}
            pending[batch_index]["claimed"] = True
            raw, rendered = _compile_batch(targets, input_for(batch_index), validation_payload=validation_payload,
                                          rendered_done=rendered_done, started=started, deadline=deadline,
                                          batch_index=batch_index, emit=emit, call_agent=call_agent,
                                          initial=pending[batch_index])
            raw_done.extend(raw)
            rendered_done.extend(rendered)
    except Exception:
        for index, initial in pending.items():
            if not initial["claimed"]:
                _timing(emit, started, initial["started"], deadline, index,
                        [s["shot_number"] for s in groups[index-1]], 1, "end",
                        outcome="discarded", reason="Another batch failed; late response cannot persist")
        raise
    # Atomic return: the caller never receives or persists a partial batch result.
    emit("shot_prompt_compiler", "All compiler batches validated together; original shots, dialogue, audio and transitions preserved.")
    return [{**shot, "compiled_prompt": text["compiled_prompt"]} for shot, text in zip(result["shots"], rendered_done)]


def _compile_batch(payload, model_input, *, validation_payload, rendered_done,
                   started, deadline, batch_index, emit, call_agent, initial):
    content = json.dumps(model_input, ensure_ascii=False)
    errors = []
    # Strong reasoning model is deliberate: immutable identities, model-specific
    # syntax and coordinated boundary reasoning. One call normally; one retry only
    # if deterministic compiler checks fail. No video/image/audio generation API.
    for attempt in range(2):
        # Include reasoning headroom as well as 100-150 words per shot. Real audits
        # exhausted a 4096-token budget entirely in thinking before emitting JSON.
        attempt_started = initial["started"] if attempt == 0 else time.monotonic()
        def timing(phase, **extra):
            _timing(emit, started, attempt_started, deadline, batch_index,
                    [s["shot_number"] for s in payload["shots"]], attempt + 1, phase, **extra)
        if attempt:
            timing("start")
        try:
            # Capture immutable arguments; a late worker must not see a later retry.
            request_content = content
            request_timeout = max(0.001, deadline - time.monotonic())
            response = _await_provider(initial["completed"], deadline=deadline) if attempt == 0 else _attempt_before_deadline(
                lambda body=request_content, budget=request_timeout: call_agent(
                    prompts.SHOT_PROMPT_COMPILER, body,
                    max_tokens=max(16000, len(payload["shots"]) * 1800), request_timeout=budget),
                deadline=deadline)
            errors = []
            if isinstance(response, dict) and isinstance(response.get("shots"), list):
                for output in response["shots"]:
                    if not isinstance(output, dict) or not isinstance(output.get("compiled_prompt"), str):
                        continue
                    visual = output["compiled_prompt"]
                    if not visual.endswith(TEXT_GUARD):
                        errors.append(f"Shot {output.get('shot_number')}: visual prose must end with the no-text guard")
                    if re.search(r'["“”]|\bDialogue:|\bPerformance reference', re.sub(r'https?://\S+', '', visual)) or AUDIO_GUARD in visual:
                        errors.append(f"Shot {output.get('shot_number')}: return visual prose only; code inserts dialogue and audio guard")
            rendered = insert_dialogue(response, payload)
            if isinstance(rendered, dict) and isinstance(rendered.get("shots"), list):
                errors += validate_compiled({"shots": rendered_done + rendered["shots"]}, validation_payload)
                by_number = {s.get("shot_number"): s.get("compiled_prompt", "") for s in rendered["shots"] if isinstance(s, dict)}
                for boundary in model_input["boundaries"]:
                    left, right = map(int, boundary["between"].split("-"))
                    if boundary["type"] == "match cut" and left in by_number and right not in by_number:
                        if not re.search(r"\b(?:end(?:s|ing)?|finish(?:es|ing)?)\b", by_number[left], re.I):
                            errors.append(f"Shot {left}: establish the match-cut ending for readonly neighbor {right}")
            else:
                errors += ["Return every input shot exactly once in its original order"]
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Shot Prompt Compiler overall {COMPILER_DEADLINE_SEC:g}-second deadline exceeded")
        except Exception as error:
            malformed = isinstance(error, ValueError) and str(error).startswith((
                "Model response was not valid JSON", "No JSON object found", "Model response was cut off"))
            if malformed and attempt == 0 and time.monotonic() < deadline:
                timing("end", outcome="format_rejected", error_type=type(error).__name__,
                       reason="Provider returned malformed or incomplete JSON")
                emit("shot_prompt_compiler", f"Compiler batch {batch_index}: malformed/incomplete JSON; using its one corrective retry within the original deadline.")
                content = json.dumps({"input": model_input, "required_corrections": [
                    "Previous response was malformed or incomplete JSON. Return complete valid JSON for every target shot, and only those targets."]}, ensure_ascii=False)
                continue
            timing("end", outcome="error", error_type=type(error).__name__, reason=str(error))
            raise
        timing("end", outcome="validation_rejected" if errors else "accepted", validation_errors=errors)
        if not errors:
            generated = rendered["shots"]
            emit("shot_prompt_compiler", f"Compiler batch {batch_index} validated, including previous-batch repetition and transition checks; awaiting whole-job completion before persistence.")
            return response["shots"], generated
        emit("shot_prompt_compiler", "Compiler validation failed; " + ("retrying once: " if attempt == 0 else "stopping: ") + " | ".join(errors))
        content = json.dumps({"input": model_input, "rejected_output": response, "required_corrections": errors}, ensure_ascii=False)
    raise ValueError("Shot Prompt Compiler failed validation: " + " | ".join(errors))
