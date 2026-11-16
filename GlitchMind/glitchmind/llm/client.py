"""Клиент языковой модели: Ollama (нативный API) или любой OpenAI-совместимый сервер.

Работает на стандартной библиотеке. «С нуля» обучать большую языковую модель нереально
(нужны миллионы долларов и терабайты текста) — вместо этого используется готовая
локальная модель, а знания из книг подаются через конспект и поиск по библиотеке.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def parse_json_loose(text: str) -> dict | None:
    """Достаёт JSON-объект из ответа модели, даже если вокруг мусор или кодовые блоки."""
    t = strip_think(text)
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.I)
    candidates = [t]
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b > a:
        candidates.append(t[a:b + 1])
    for c in candidates:
        for fixed in (c, re.sub(r",(\s*[}\]])", r"\1", c)):
            try:
                d = json.loads(fixed)
                if isinstance(d, dict):
                    return d
            except ValueError:
                continue
    return None


class LLMClient:
    def __init__(self, provider: str, base_url: str, model: str, api_key: str = "",
                 temperature: float = 0.4, timeout: int = 240, num_ctx: int = 8192):
        self.provider = (provider or "ollama").lower()
        self.base = (base_url or "").strip().rstrip("/")
        self.model, self.api_key = model, api_key
        self.temperature, self.timeout, self.num_ctx = temperature, timeout, num_ctx

    # ------------------------------------------------------------------ URL
    def _ollama_base(self) -> str:
        return re.sub(r"/(api|v1)$", "", self.base)

    def _openai_base(self) -> str:
        b = self.base
        if b.endswith("/chat/completions"):
            return b[: -len("/chat/completions")]
        if re.search(r"/v\d+$", b):
            return b
        return b + "/v1"

    # ------------------------------------------------------------------ HTTP
    def _request(self, url: str, body: dict | None = None, timeout: float | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                payload = e.read().decode("utf-8", "replace")
                msg = (json.loads(payload).get("error") or payload)
                if isinstance(msg, dict):
                    msg = msg.get("message", str(msg))
            except Exception:  # noqa: BLE001
                msg = str(e)
            hint = ""
            if e.code == 404 and self.provider == "ollama" and self.model:
                hint = f" Скачай модель: ollama pull {self.model}"
            if e.code in (401, 403):
                hint = " Проверь API-ключ."
            raise LLMError(f"HTTP {e.code}: {msg}.{hint}") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise LLMError(f"Модель не ответила за {int(timeout or self.timeout)} с.") from e
            hint = " Запущена ли Ollama? (ollama serve)" if self.provider == "ollama" else ""
            raise LLMError(f"Не удаётся подключиться к {self.base}.{hint}") from e
        except (socket.timeout, TimeoutError) as e:
            raise LLMError(f"Модель не ответила за {int(timeout or self.timeout)} с.") from e
        except ValueError as e:
            raise LLMError("Сервер вернул не JSON.") from e

    # ------------------------------------------------------------------- API
    def list_models(self) -> list[str]:
        if self.provider == "ollama":
            d = self._request(self._ollama_base() + "/api/tags", timeout=8)
            return sorted(m.get("name", "") for m in d.get("models", []) if m.get("name"))
        d = self._request(self._openai_base() + "/models", timeout=8)
        return sorted(m.get("id", "") for m in d.get("data", []) if m.get("id"))

    def chat(self, messages: list[dict], json_mode: bool = False) -> str:
        if not self.model:
            raise LLMError("Модель не выбрана (Настройки → ИИ-модель).")
        if self.provider == "ollama":
            body = {
                "model": self.model, "messages": messages, "stream": False,
                "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
            }
            if json_mode:
                body["format"] = "json"
            d = self._request(self._ollama_base() + "/api/chat", body)
            return strip_think((d.get("message") or {}).get("content", ""))
        body = {"model": self.model, "messages": messages, "temperature": self.temperature, "stream": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        d = self._request(self._openai_base() + "/chat/completions", body)
        try:
            return strip_think(d["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("Неожиданный формат ответа модели.") from e

    def test(self) -> dict:
        try:
            models = self.list_models()
        except LLMError as e:
            return {"ok": False, "message": str(e), "models": []}
        if self.model and models and not any(self.model == m or m.startswith(self.model + ":") for m in models):
            hint = f" Скачай: ollama pull {self.model}" if self.provider == "ollama" else ""
            return {"ok": False, "message": f"Сервер доступен, но модели «{self.model}» нет.{hint}", "models": models}
        return {"ok": True, "message": f"Подключено. Моделей на сервере: {len(models)}.", "models": models}
