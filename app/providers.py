"""LLM provider layer.

The entire contract between the application and the AI world:

    LLMProvider.generate(messages) -> str        # RAW text. Nothing else.

The provider never parses, validates, or interprets model output. Mock and real
providers therefore cross the exact same trust boundary -- raw text becomes a
validated CoachPlan in exactly ONE place (service.parse_plan), and business code
depends only on this interface, never on a vendor SDK.
"""

from __future__ import annotations

import json
import os 
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

from app.errors import MalformedModelOutputError, ProviderTerminalError, ProviderTransientError

Messages = List[Dict[str, str]]


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, messages: Messages) -> str:
        """Return the raw completion text for the given conversation."""


class MockLLMProvider(LLMProvider):
    """Scriptable mock: default provider for the deployed service, test seam for CI.

    - raw_response: returned whenever `script` is empty (persistent answer).
    - script:       FIFO consumed one item per call; an Exception is raised,
                    a str is returned verbatim. Lets tests script sequences
                    like [TransientError, TransientError, valid_json] to
                    exercise bounded retries deterministically.
    - calls:        number of generate() invocations; tests assert on it to
                    prove the model was (or was not) consulted again.
    """

    DEFAULT_PLAN: str = json.dumps(
        {
            "assistant_message": "Here to help with your training.",
            "intent": "unknown",
            "action": {"type": "no_action"},
            "warnings": [],
        }
    )

    def __init__(self) -> None:
        env_plan = os.environ.get("MOCK_PLAN_JSON", "").strip()
        self.raw_response: str = env_plan or self.DEFAULT_PLAN
        self.script: List[object] = []
        self.calls: int = 0

    def generate(self, messages: Messages) -> str:
        self.calls += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return str(item)
        return self.raw_response
    
    

class OpenAICompatibleProvider(LLMProvider):
    """Reference integration for any OpenAI-compatible chat API
    (OpenAI, Azure OpenAI, vLLM, ...). Selected with LLM_PROVIDER=openai;
    never used in the default mock setup.

    How the error taxonomy maps onto a real HTTP client:

      timeout / connection error   -> ProviderTransientError (retry may help)
      429 rate limit               -> ProviderTransientError (retry-after surfaced)
      5xx                          -> ProviderTransientError
      other 4xx                    -> ProviderTerminalError (retrying cannot help)
      200                          -> raw content string, still 100% untrusted

    JSON mode (`response_format`) is requested to make valid JSON *likely*, but
    the result is still parsed defensively downstream. Prompting improves odds;
    it never replaces validation.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout_s: float,
        transport: Optional[httpx.BaseTransport] = None,  # injectable for tests
    ) -> None:
        self._model = model
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def generate(self, messages: Messages) -> str:
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        try:
            response = self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(f"provider timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(f"connection error: {exc}") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after", "unknown")
            raise ProviderTransientError(f"rate limited (retry-after={retry_after}s)")
        if response.status_code >= 500:
            raise ProviderTransientError(f"provider server error: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ProviderTerminalError(f"provider rejected request: HTTP {response.status_code}")

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise MalformedModelOutputError(f"unrecognised provider envelope: {exc}") from exc
        if not isinstance(content, str):
            raise MalformedModelOutputError("provider content is not a string")
        return content