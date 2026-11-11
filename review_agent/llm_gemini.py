"""Gemini adapter (google-genai SDK) with manual function calling and model fallback."""
from __future__ import annotations

import logging
import os
import time

from google import genai
from google.genai import errors, types

from .agent import Step, ToolCall

log = logging.getLogger("review_agent")

DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
RETRYABLE = {429, 500, 503, 504}


class GeminiLLM:
    def __init__(self, models: list[str] | None = None, api_key: str | None = None, temperature: float = 0.2):
        self.models = models or os.environ.get("REVIEW_MODELS", ",".join(DEFAULT_MODELS)).split(",")
        self.model = self.models[0]
        self.client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY"),
                                   http_options=types.HttpOptions(timeout=90_000))
        self.contents: list[types.Content] = []
        self.config: types.GenerateContentConfig | None = None
        self.temperature = temperature

    # -- public API used by the agent ------------------------------------
    def start(self, system: str, prompt: str, tools: list[dict]) -> Step:
        self.config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**t) for t in tools])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
        )
        self.contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
        return self._generate()

    def reply(self, results: list[tuple[str, dict]]) -> Step:
        parts = [types.Part.from_function_response(name=n, response=r) for n, r in results]
        self.contents.append(types.Content(role="user", parts=parts))
        return self._generate()

    def say(self, text: str) -> Step:
        self.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        return self._generate()

    # -- internals ---------------------------------------------------------
    def _generate(self) -> Step:
        last: Exception | None = None
        for model in self.models:
            for attempt in range(3):
                try:
                    resp = self.client.models.generate_content(model=model, contents=self.contents, config=self.config)
                    self.model = model
                    return self._to_step(resp)
                except errors.APIError as e:
                    last = e
                    if getattr(e, "code", None) not in RETRYABLE:
                        raise
                    wait = 2 ** attempt * 2
                    log.warning("%s returned %s; retrying in %ss", model, e.code, wait)
                    time.sleep(wait)
            log.warning("falling back from %s", model)
        raise RuntimeError(f"All Gemini models failed: {last}")

    def _to_step(self, resp) -> Step:
        cand = resp.candidates[0] if resp.candidates else None
        if cand is None or cand.content is None:
            return Step(text="")
        self.contents.append(cand.content)  # keeps thought signatures intact for Gemini 3
        calls = [ToolCall(name=fc.name, args=dict(fc.args or {})) for fc in (resp.function_calls or [])]
        text = "".join(p.text for p in (cand.content.parts or []) if getattr(p, "text", None))
        return Step(calls=calls, text=text)
