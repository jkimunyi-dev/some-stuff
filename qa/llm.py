"""Single generative model via Ollama, with a per-question budget ledger (rules: <=15 calls, <=12,000 generated tokens
including thinking, retries and self-checks). Every model call in the pipeline MUST go through LLM.chat.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field

MAX_CALLS = 15
MAX_TOKENS = 12_000

# Sampling profiles. Reproducibility comes from a fixed seed. "extract" is near-greedy for schema-constrained choices.
PROFILES = {
    "thinking": dict(temperature=1.0, top_p=0.95, top_k=20, presence_penalty=1.5),
    "precise": dict(temperature=0.6, top_p=0.95, top_k=20, presence_penalty=0.0),
    "extract": dict(temperature=0.0, top_p=1.0, top_k=1),
    "write": dict(temperature=0.2, top_p=0.9, top_k=40),
}


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Ledger:
    """Per-question call log. Shared preprocessing that uses the model must be charged to each question that uses it."""
    calls: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    deadline_s: float = 240.0  # wall-clock cap per question: a run can never hang on one question

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    @property
    def tokens(self) -> int:
        return sum(c["eval_tokens"] for c in self.calls)

    def remaining_tokens(self) -> int:
        return MAX_TOKENS - self.tokens

    def summary(self) -> dict:
        return {"calls": self.n_calls, "tokens": self.tokens, "seconds": round(sum(c["seconds"] for c in self.calls), 1)}


class LLM:
    def __init__(self, model: str = "qwen2.5:3b-instruct", url: str = "http://127.0.0.1:11434", seed: int = 1234, timeout: int = 120):
        self.model, self.url, self.seed, self.timeout = model, url.rstrip("/"), seed, timeout
        self._caps = None
        # backend: 'ollama' (native /api/chat) or 'vllm' (OpenAI-compatible /v1/chat/completions); same interface and ledger
        self.backend = "vllm" if self.url.endswith("/v1") else "ollama"

    def _chat_vllm(self, messages, ledger, think, opts, max_tokens, json_mode, tag):
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "seed": self.seed,
                "temperature": opts.get("temperature", 0.6), "top_p": opts.get("top_p", 1.0),
                "chat_template_kwargs": {"enable_thinking": bool(think)}}
        if opts.get("top_k"):
            body["top_k"] = opts["top_k"]
        if opts.get("presence_penalty"):
            body["presence_penalty"] = opts["presence_penalty"]
        if isinstance(json_mode, dict):
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": json_mode}}
        elif json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.time()
        req = urllib.request.Request(self.url + "/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            out = json.loads(r.read())
        ch = out["choices"][0]
        msg = ch.get("message", {})
        u = out.get("usage", {})
        rec = {"tag": tag, "think": think, "eval_tokens": int(u.get("completion_tokens", 0)), "prompt_tokens": int(u.get("prompt_tokens", 0)),
               "seconds": time.time() - t0, "done_reason": ch.get("finish_reason")}
        ledger.calls.append(rec)  # completion_tokens include any reasoning tokens -> budget stays honest
        return {"text": msg.get("content", "") or "", "thinking": msg.get("reasoning_content", "") or "", **rec}

    def can_think(self) -> bool:
        if self._caps is None:
            try:
                req = urllib.request.Request(self.url + "/api/show", data=json.dumps({"model": self.model}).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    self._caps = set(json.loads(r.read()).get("capabilities") or [])
            except Exception:
                self._caps = set()
        return "thinking" in self._caps

    def chat(self, messages: list[dict], ledger: Ledger, think: bool = False, profile: str | None = None,
             max_tokens: int = 2000, json_mode: bool | dict = False, tag: str = "") -> dict:
        if ledger.n_calls >= MAX_CALLS:
            raise BudgetExceeded(f"{MAX_CALLS} calls used")
        if time.time() - ledger.started > ledger.deadline_s:
            raise BudgetExceeded(f"question exceeded {ledger.deadline_s:.0f} s")
        left = ledger.remaining_tokens()
        if left <= 0:
            raise BudgetExceeded(f"{MAX_TOKENS} tokens used")
        opts = dict(PROFILES[profile or ("thinking" if think else "precise")])
        if self.backend == "vllm":
            return self._chat_vllm(messages, ledger, think, opts, min(max_tokens, left), json_mode, tag)
        opts.update(seed=self.seed, num_predict=min(max_tokens, left), num_ctx=16384)
        body = {"model": self.model, "messages": messages, "stream": False, "options": opts}
        if self.can_think():  # thinking-capable models get an explicit flag (they may think by default); others must not receive it
            body["think"] = bool(think)
        if json_mode:
            body["format"] = json_mode if isinstance(json_mode, dict) else "json"  # JSON schema -> grammar-constrained output; callers still validate
        t0 = time.time()
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            out = json.loads(r.read())
        msg = out.get("message", {})
        rec = {"tag": tag, "think": think, "eval_tokens": int(out.get("eval_count", 0)), "prompt_tokens": int(out.get("prompt_eval_count", 0)),
               "seconds": time.time() - t0, "done_reason": out.get("done_reason")}
        ledger.calls.append(rec)
        return {"text": msg.get("content", "") or "", "thinking": msg.get("thinking", "") or "", **rec}
