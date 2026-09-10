from fastapi import APIRouter, HTTPException

from app.agents import prompts
from app.agents.llm_client import call_agent
from app.schemas import ScriptExtract, ScriptExtractionOut

router = APIRouter(prefix="/api/scripts", tags=["scripts"])


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
        if any(
            name not in payload.script_text
            for name in [*result.characters, *result.locations]
        ):
            raise ValueError("extracted entity was not copied verbatim from the script")
        return result
    except Exception as error:
        raise HTTPException(status_code=502, detail="script extraction failed") from error
