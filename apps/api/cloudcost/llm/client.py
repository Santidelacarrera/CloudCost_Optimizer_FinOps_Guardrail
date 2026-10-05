"""Clientes HTTP mínimos para OpenAI y Gemini. Solo envían el contexto filtrado; no usan herramientas ni funciones."""
from __future__ import annotations

import requests


class LLMError(Exception):
    pass


class OpenAIClient:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com", timeout: float = 30.0):
        self.api_key, self.model, self.base_url, self.timeout = api_key, model, base_url.rstrip("/"), timeout

    def complete_json(self, system: str, user: str) -> str:
        try:
            resp = requests.post(
                f"{self.base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"OpenAI: {type(exc).__name__}") from exc


class GeminiClient:
    def __init__(self, api_key: str, model: str, timeout: float = 30.0):
        self.api_key, self.model, self.timeout = api_key, model, timeout

    def complete_json(self, system: str, user: str) -> str:
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                headers={"x-goog-api-key": self.api_key},          # en cabecera, no en la URL
                json={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": user}]}],
                      "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Gemini: {type(exc).__name__}") from exc


def build_client(provider: str, model: str, openai_key: str | None, gemini_key: str | None, timeout: float):
    if provider == "openai" and openai_key:
        return OpenAIClient(openai_key, model, timeout=timeout)
    if provider == "gemini" and gemini_key:
        return GeminiClient(gemini_key, model, timeout=timeout)
    return None
