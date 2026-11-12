"""Minimal Ollama client (local, free). Uses structured JSON output."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host: str, model: str, num_ctx: int = 8192, temperature: float = 0.4,
                 timeout: int = 300):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.host + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"Ollama HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"Cannot reach Ollama at {self.host} ({e}). Is the Ollama app running?") from e

    def check(self) -> None:
        try:
            with urllib.request.urlopen(self.host + "/api/tags", timeout=10) as resp:
                tags = json.loads(resp.read())
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Cannot reach Ollama at {self.host}: {e}") from e
        names = {m.get("name", "") for m in tags.get("models", [])}
        wanted = self.model if ":" in self.model else self.model + ":latest"
        if wanted not in names and self.model not in names:
            raise LLMError(f"Model '{self.model}' not pulled. Run: ollama pull {self.model}")

    def generate_json(self, prompt: str, schema: dict, system: str | None = None,
                      retries: int = 2) -> dict:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "keep_alive": "10m",
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        if system:
            payload["system"] = system
        last = None
        for attempt in range(retries + 1):
            out = self._post("/api/generate", payload)
            text = out.get("response", "")
            try:
                data = json.loads(text)
                missing = [k for k in schema.get("required", []) if k not in data]
                if missing:
                    raise ValueError(f"missing keys {missing}")
                return data
            except (json.JSONDecodeError, ValueError) as e:
                last = e
                log.debug("LLM returned invalid JSON (attempt %d): %s", attempt + 1, text[:200])
        raise LLMError(f"LLM did not return valid JSON: {last}")

    def unload(self) -> None:
        """Free the model's RAM immediately (important on an 8 GB Mac before launching Chrome)."""
        try:
            self._post("/api/generate", {"model": self.model, "keep_alive": 0})
        except LLMError:
            pass
