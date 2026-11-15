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

# Qwen3.5 model-card sampling profiles. Greedy is avoided (small model loops); reproducibility comes from a fixed seed.
PROFILES = {
    "thinking": dict(temperature=1.0, top_p=0.95, top_k=20, presence_penalty=1.5),
    "precise": dict(temperature=0.6, top_p=0.95, top_k=20, presence_penalty=0.0),
}


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Ledger:
    """Per-question call log. Shared preprocessing that uses the model must be charged to each question that uses it."""
    calls: list[dict] = field(default_factory=list)

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
    def __init__(self, model: str = "qwen3.5:2b", url: str = "http://127.0.0.1:11434", seed: int = 1234, timeout: int = 900):
        self.model, self.url, self.seed, self.timeout = model, url.rstrip("/"), seed, timeout

    def chat(self, messages: list[dict], ledger: Ledger, think: bool = False, profile: str | None = None,
             max_tokens: int = 2000, json_mode: bool = False, tag: str = "") -> dict:
        if ledger.n_calls >= MAX_CALLS:
            raise BudgetExceeded(f"{MAX_CALLS} calls used")
        left = ledger.remaining_tokens()
        if left <= 0:
            raise BudgetExceeded(f"{MAX_TOKENS} tokens used")
        opts = dict(PROFILES[profile or ("thinking" if think else "precise")])
        opts.update(seed=self.seed, num_predict=min(max_tokens, left), num_ctx=16384)
        body = {"model": self.model, "messages": messages, "stream": False, "think": think, "options": opts}
        if json_mode:
            body["format"] = "json"  # not relied upon (known to be ignored by some builds); callers validate
        t0 = time.time()
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            out = json.loads(r.read())
        msg = out.get("message", {})
        rec = {"tag": tag, "think": think, "eval_tokens": int(out.get("eval_count", 0)), "prompt_tokens": int(out.get("prompt_eval_count", 0)),
               "seconds": time.time() - t0, "done_reason": out.get("done_reason")}
        ledger.calls.append(rec)
        return {"text": msg.get("content", "") or "", "thinking": msg.get("thinking", "") or "", **rec}
