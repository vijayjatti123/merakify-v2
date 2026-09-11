import re

from fastapi import APIRouter, HTTPException

from app.agents import prompts
from app.agents.llm_client import call_agent
from app.schemas import ScriptExtract, ScriptExtractionOut

router = APIRouter(prefix="/api/scripts", tags=["scripts"])


def _source_spelling(name: str, script_text: str) -> str:
    """Return the source's exact spelling when the model changes case only."""
    if name in script_text:
        return name
    match = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", script_text, flags=re.IGNORECASE)
    if match and match.group(0).casefold() == name.casefold():
        return match.group(0)
    raise ValueError("extracted entity was not copied verbatim from the script")


@router.post("/extract", response_model=ScriptExtractionOut)
def extract_script(payload: ScriptExtract):
    try:
        extracted = call_agent(
            prompts.SCRIPT_EXTRACTOR,
            payload.script_text,
            fast=True,
            max_tokens=512,
        )
        result = ScriptExtractionOut.model_validate(extracted)
        return ScriptExtractionOut(
            characters=[_source_spelling(name, payload.script_text) for name in result.characters],
            locations=[_source_spelling(name, payload.script_text) for name in result.locations],
        )
    except Exception as error:
        raise HTTPException(status_code=502, detail="script extraction failed") from error
