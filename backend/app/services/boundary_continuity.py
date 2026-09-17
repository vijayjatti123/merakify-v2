"""Review temporal meaning without rewriting approved shots or transition effects."""
import hashlib
import json

from app.agents import prompts


def review_input(result, brief):
    shots = result.get("shots", [])
    transitions = {t["between"]: t for t in result.get("assembly", {}).get("transitions", [])}
    candidates = []
    for left, right in zip(shots, shots[1:]):
        key = f'{left["shot_number"]}-{right["shot_number"]}'
        boundary = transitions.get(key, {})
        end, start = left.get("state_at_shot_end"), right.get("state_at_shot_start")
        if (left.get("scene_number") is not None and left.get("scene_number") == right.get("scene_number")
                and boundary.get("type") in {"cut", "match cut"} and (end or start)
                and not (isinstance(end, str) and isinstance(start, str) and end.strip() == start.strip())):
            fields = ("shot_number", "scene_number", "description", "state_at_shot_start", "state_at_shot_end", "dialogue_text")
            candidates.append({"between": key, "transition": boundary,
                "left": {k: left.get(k) for k in fields}, "right": {k: right.get(k) for k in fields}})
    return {"brief": brief, "scenes": result.get("script", {}).get("scenes", []), "boundaries": candidates}


def fingerprint(payload):
    # A policy update also invalidates an earlier semantic verdict.
    return hashlib.sha256(json.dumps([prompts.BOUNDARY_CONTINUITY_REVIEW, payload],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_review(response, payload):
    rows = response.get("boundaries") if isinstance(response, dict) else None
    expected = {b["between"] for b in payload["boundaries"]}
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("Boundary review returned no decisions")
    if len(rows) != len(expected) or {r.get("between") for r in rows} != expected:
        raise ValueError("Boundary review must cover each requested boundary exactly once")
    for row in rows:
        if type(row.get("approved")) is not bool or row.get("relation") not in {
                "same_instant", "action_progression", "narrative_transition", "conflict"}:
            raise ValueError("Boundary review returned an invalid decision")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("Boundary review must explain its source evidence")
        if row["approved"] and row["relation"] == "conflict":
            raise ValueError("A conflicting boundary cannot be approved")
        if row["approved"] and row["relation"] == "same_instant" and not (
                isinstance(row.get("shared_physical_state"), str) and row["shared_physical_state"].strip()):
            raise ValueError("Equivalent boundary snapshots need a grounded shared state")
    return rows


def reviewed_boundaries(result, brief=""):
    payload = review_input(result, brief)
    saved = result.get("boundary_continuity_review") or {}
    if saved.get("fingerprint") != fingerprint(payload):
        return {}
    try:
        rows = validate_review(saved, payload)
    except ValueError:
        return {}
    return {r["between"]: r for r in rows if r["approved"]}


def prepare_boundaries(result, *, brief="", emit, call_agent):
    """One small review, one bounded corrective retry; cache only unchanged inputs."""
    payload = review_input(result, brief)
    if not payload["boundaries"]:
        return
    if len(reviewed_boundaries(result, brief)) == len(payload["boundaries"]):
        emit("boundary_continuity", "Reusing verified transition continuity for unchanged plan.")
        return
    emit("boundary_continuity", f'Reviewing temporal continuity at {len(payload["boundaries"])} boundaries before media preparation.')
    request = payload
    for attempt in range(2):
        response = call_agent(prompts.BOUNDARY_CONTINUITY_REVIEW, json.dumps(request, ensure_ascii=False),
            fast=True, max_tokens=min(4096, 512 + 256 * len(payload["boundaries"])), request_timeout=40,
            on_response=lambda usage: emit("boundary_continuity_usage", json.dumps(usage)))
        try:
            rows = validate_review(response, payload)
            rejected = [r for r in rows if not r["approved"]]
            if rejected:
                raise ValueError("; ".join(f'Boundary {r["between"]}: {r["reason"]}' for r in rejected))
        except ValueError as error:
            if attempt == 0:
                # Ask for a targeted recheck, never for approval regardless of evidence.
                request = {"source": payload, "previous_review": response, "required_recheck": str(error)}
                continue
            raise ValueError(f"Shot transitions need review before video preparation: {error}") from error
        result["boundary_continuity_review"] = {"fingerprint": fingerprint(payload), "boundaries": rows}
        for row in rows:
            emit("boundary_continuity", f'Boundary {row["between"]}: {row["relation"]}; approved.')
        return
