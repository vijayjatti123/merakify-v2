import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.agents import llm_client, prompts


CLAUDE_PROMPTS = (
    "FORMAT_CLASSIFIER", "SCRIPT_EXTRACTOR", "SCRIPT_ARCHITECT",
    "SCRIPT_ARCHITECT_FROM_SCRIPT", "CONTINUITY_AGENT", "CINEMATOGRAPHY_AGENT",
    "QA_AGENT", "CINEMATOGRAPHY_FIX", "CINEMATOGRAPHY_TRIM", "SHOT_ASSEMBLER",
    "SHOT_PROMPT_COMPILER",
)


def check_static_prompt_is_identical_and_variable_input_uncached(name):
    system = getattr(prompts, name)
    if name == "SCRIPT_ARCHITECT":
        system = system % "Hindi"
    fast = name in {"FORMAT_CLASSIFIER", "SCRIPT_EXTRACTOR"}
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"ok":true}')]
    )
    with patch.object(llm_client, "_client", client):
        assert llm_client.call_agent(system, "Variable job content", fast=fast) == {"ok": True}
    request = client.messages.create.call_args.kwargs
    assert request == {
        "model": "claude-haiku-4-5-20251001" if fast else "claude-sonnet-5",
        "max_tokens": 2048,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "Variable job content"}],
    }


def check_only_classifier_and_extractor_opt_into_haiku():
    fast_sites = []
    app = Path(__file__).resolve().parents[1] / "app"
    for path in app.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "call_agent":
                for keyword in node.keywords:
                    if keyword.arg == "fast":
                        assert isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                        fast_sites.append(ast.unparse(node.args[0]))
    assert sorted(fast_sites) == ["prompts.FORMAT_CLASSIFIER", "prompts.SCRIPT_EXTRACTOR"]


def check_compiler_deadline_and_truncation_handling_unchanged(test):
    client = Mock()
    scoped = client.with_options.return_value
    scoped.messages.create.return_value = SimpleNamespace(
        stop_reason="max_tokens", content=[SimpleNamespace(type="text", text='{"shots":[')]
    )
    with patch.object(llm_client, "_client", client), test.assertRaisesRegex(ValueError, "cut off"):
        llm_client.call_agent(prompts.SHOT_PROMPT_COMPILER, "shots", max_tokens=16000, request_timeout=12.5)
    client.with_options.assert_called_once_with(timeout=12.5, max_retries=0)
    assert scoped.messages.create.call_args.kwargs["max_tokens"] == 16000


class ClaudeCachingTests(unittest.TestCase):
    def test_all_static_prompts_and_routing(self):
        for name in CLAUDE_PROMPTS:
            with self.subTest(agent=name):
                check_static_prompt_is_identical_and_variable_input_uncached(name)

    def test_only_two_fast_callers(self):
        check_only_classifier_and_extractor_opt_into_haiku()

    def test_compiler_transport_contract(self):
        check_compiler_deadline_and_truncation_handling_unchanged(self)
