"""Source-grounded classification for repetition checks; no model calls."""
import re

# The closed provenance catalog. Adding a category belongs here, never in the
# repetition loop, and requires a source-grounding regression test.
LOCKED_SOURCE_FIELDS = {
    "identity": ("character_id", "locked_vault_description"),
    "style": ("rendering", "palette", "texture_grain"),
    "technical": ("lens", "hardware_reference"),
}

def _vault_fact_tokens(text):
    # Ignore grammatical list glue, never synonyms or reordered content words.
    return tuple(word for word in re.findall(r"\w+", text.casefold())
                 if word not in {"a", "an", "the", "her", "his", "their", "its", "and"})


def _vault_fact_ids(phrase, source):
    needle = _vault_fact_tokens(" ".join(phrase))
    # Short exact wardrobe/accessory facts ("casual shirt", "tool kit")
    # are still Vault facts. Require two content words and contiguous source
    # provenance; never exempt isolated common words or inferred synonyms.
    if len(needle) < 2:
        return set()
    ids = set()
    for ref in source.get("character_references", []):
        locked = ref.get("locked_vault_description")
        if not ref.get("character_id") or not isinstance(locked, str):
            continue
        words = _vault_fact_tokens(str(ref.get("name") or "") + " " + locked)
        if any(words[i:i + len(needle)] == needle for i in range(len(words) - len(needle) + 1)):
            ids.add(ref["character_id"])
    return ids


def _job_style_fact(phrase, bible):
    """Ground in exact locked spans, never the output's style label.

    Lighting motifs may contain scene-specific source positions; those remain
    subject to the existing physical-lighting continuity check instead.
    """
    if not isinstance(bible, dict):
        return False
    needle = _vault_fact_tokens(" ".join(phrase))
    if len(needle) < 6:
        return False
    for field in LOCKED_SOURCE_FIELDS["style"]:
        value = bible.get(field)
        if not isinstance(value, str):
            continue
        words = _vault_fact_tokens(value)
        if any(words[i:i + len(needle)] == needle for i in range(len(words) - len(needle) + 1)):
            return True
    # A combined clause can join exact excerpts from different locked fields.
    # No bag-of-words/synonym matching, arbitrary omissions, or lighting fields.
    # The optional suffix in the literal rendering compound "live-action-style"
    # is grammatical glue ONLY for this multi-field path; the single-field exact
    # matcher above is unchanged.
    def combined_tokens(text):
        text = re.sub(r"\blive[- ]action[- ]style\b", "live action", text, flags=re.I)
        return _vault_fact_tokens(text)
    combined = combined_tokens(" ".join(phrase))
    fields = {key: combined_tokens(value) for key, value in bible.items()
              if key in LOCKED_SOURCE_FIELDS["style"] and isinstance(value, str)}
    joins = {"with", "plus", "alongside"}  # Articles and "and" already normalize away.

    def matches(offset, used):
        if offset == len(combined):
            return len(used) >= 2
        if used and combined[offset] in joins:
            offset += 1
        for field, words in fields.items():
            if field in used:
                continue
            # At least two content tokens establish each contributing field.
            for end in range(offset + 2, len(combined) + 1):
                span = combined[offset:end]
                if any(words[i:i + len(span)] == span for i in range(len(words) - len(span) + 1)):
                    if matches(end, used | {field}):
                        return True
        return False

    return matches(0, set())


def _technical_fact_pattern(source):
    """Match source lens notation; never infer an optical explanation."""
    lens = source.get("lens") or ""
    focal = re.search(r"\b(\d+(?:\.\d+)?)\s*mm\b", lens, re.I)
    if not focal:
        return None
    value = focal.group(1)
    aliases = [re.escape(value)]
    # Spelling out a focal length is notation, not a change to the locked fact.
    units = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    tens = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
    if value.isdigit() and 0 < int(value) < 100:
        n = int(value)
        words = units[n] if n < 20 else tens[n // 10] + (" " + units[n % 10] if n % 10 else "")
        aliases.append(r"[-\s]+".join(words.split()))
    measure = r"(?:" + "|".join(aliases) + r")[-\s]*(?:mm|millimet(?:er|re)s?)"
    def literal(text):
        return r"[-\s]+".join(re.escape(word) for word in re.findall(r"\w+", text))
    prefix, suffix = literal(lens[:focal.start()]), literal(lens[focal.end():])
    pattern = r"\b" + (prefix + r"[-\s]+" if prefix else "") + measure
    pattern += (r"[-\s]+" + suffix if suffix else "")
    if not re.search(r"\blens\b", lens, re.I):
        pattern += r"(?:[-\s]+lens)?"
    pattern += r"\b"
    return pattern



def classify_repetition_tokens(prose, source, style_bible):
    """Partition source-grounded facts from creative prose before any windows.

    Registry is deliberately closed: output labels and arbitrary metadata cannot
    grant an exemption. Every locked token records its provenance for auditing.
    Facts are independently grounded per shot, irrespective of scene position.
    """
    matches = list(re.finditer(r"\w+", prose))
    tokens = [m.group().casefold() for m in matches]
    evidence = [set() for _ in tokens]
    def mark(start, end, provenance):
        for i, token in enumerate(matches):
            if token.start() >= start and token.end() <= end:
                evidence[i].add(provenance)
    # Whole fixed technical spans: no blanket exemption for the sentence.
    lens = _technical_fact_pattern(source)
    if lens:
        for m in re.finditer(lens, prose, re.I):
            mark(m.start(), m.end(), "shot.lens")
    hardware = source.get("hardware_reference")
    if hardware:
        for m in re.finditer(re.escape(hardware), prose, re.I):
            mark(m.start(), m.end(), "shot.hardware_reference")
    # Complete short field values are facts too. Partial identity/style excerpts
    # still use the stricter multi-token grounding below to avoid common-word masks.
    exact = [("job.visual_style." + key, (style_bible or {}).get(key))
             for key in LOCKED_SOURCE_FIELDS["style"]]
    for ref in source.get("character_references", []):
        if ref.get("character_id"):
            exact.append(("vault." + ref["character_id"] + ".description", ref.get("locked_vault_description")))
    if source.get("lens") and not lens:
        exact.append(("shot.lens", source["lens"]))
    for provenance, value in exact:
        if not isinstance(value, str):
            continue
        words = re.findall(r"\w+", value)
        # A lone common adjective (e.g. "natural") cannot safely classify an
        # arbitrary occurrence as a style fact. Keep ambiguous fragments creative.
        if len(words) < 2:
            continue
        pattern = r"\b" + r"[\W_]+".join(map(re.escape, words)) + r"\b"
        for m in re.finditer(pattern, prose, re.I):
            mark(m.start(), m.end(), provenance)
    # Existing exact/provenance-aware matchers become category classifiers.
    # Overlapping spans cover long facts, while no neighboring action is removed.
    for start in range(len(tokens)):
        for end in range(start + 1, min(len(tokens), start + 12) + 1):
            phrase = tuple(tokens[start:end])
            ids = _vault_fact_ids(phrase, source)
            style = _job_style_fact(phrase, style_bible)
            for identifier in ids:
                mark(matches[start].start(), matches[end-1].end(), "vault." + identifier + ".description")
            if style:
                mark(matches[start].start(), matches[end-1].end(), "job.visual_style")
    return [{"text": token, "category": "LOCKED_FACT" if refs else "CREATIVE_PROSE",
             "sources": sorted(refs)} for token, refs in zip(tokens, evidence)]


def creative_windows(classified, size=8):
    """Same eight-word threshold, applied only to creative text in source order.

    Inserting a locked fact into copied creative prose cannot hide the copy.
    """
    words = [t["text"] for t in classified if t["category"] == "CREATIVE_PROSE"]
    return {tuple(words[i:i+size]) for i in range(len(words)-size+1)}
