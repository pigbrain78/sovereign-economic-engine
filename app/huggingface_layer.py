from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_UP
from urllib import error, request


class HuggingFaceProviderUnavailable(RuntimeError):
    pass


class ModelPolicyViolation(ValueError):
    pass


@dataclass(frozen=True)
class HuggingFaceModel:
    model_id: str
    display_name: str
    capabilities: tuple[str, ...]
    context_window: int
    quality_score: float
    reliability: float
    latency_ms: int
    input_usd_per_1m: Decimal | None
    output_usd_per_1m: Decimal | None
    providers: tuple[str, ...]
    status: str = "candidate"
    pricing_source: str = "PLACEHOLDER_CONFIGURE_FROM_HF"

    @property
    def pricing_verified(self) -> bool:
        return self.input_usd_per_1m is not None and self.output_usd_per_1m is not None and self.pricing_source != "PLACEHOLDER_CONFIGURE_FROM_HF"

    def as_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "capabilities": list(self.capabilities),
            "context_window": self.context_window,
            "quality_score": self.quality_score,
            "reliability": self.reliability,
            "latency_ms": self.latency_ms,
            "input_usd_per_1m": str(self.input_usd_per_1m) if self.input_usd_per_1m is not None else None,
            "output_usd_per_1m": str(self.output_usd_per_1m) if self.output_usd_per_1m is not None else None,
            "providers": list(self.providers),
            "status": self.status,
            "pricing_source": self.pricing_source,
            "pricing_verified": self.pricing_verified,
        }


# These are routing/catalog placeholders, not live provider prices. Production execution
# requires HF_MODEL_PRICING_JSON with verified rates and is blocked otherwise.
DEFAULT_CATALOG: tuple[HuggingFaceModel, ...] = (
    HuggingFaceModel("Qwen/Qwen2.5-7B-Instruct", "Qwen 2.5 7B Instruct", ("chat", "code", "structured_output", "tool_use"), 32768, .84, .93, 1400, None, None, ("hf-inference", "together", "novita")),
    HuggingFaceModel("meta-llama/Llama-3.1-8B-Instruct", "Llama 3.1 8B Instruct", ("chat", "code", "structured_output", "tool_use"), 131072, .83, .94, 1200, None, None, ("hf-inference", "groq", "together")),
    HuggingFaceModel("mistralai/Mistral-7B-Instruct-v0.3", "Mistral 7B Instruct", ("chat", "code", "structured_output"), 32768, .79, .92, 1000, None, None, ("hf-inference", "together")),
    HuggingFaceModel("deepseek-ai/DeepSeek-R1-Distill-Qwen-7B", "DeepSeek R1 Distill Qwen 7B", ("chat", "code", "reasoning", "structured_output"), 32768, .88, .90, 2500, None, None, ("hf-inference", "novita", "together")),
)


def _load_catalog() -> tuple[HuggingFaceModel, ...]:
    raw = os.environ.get("HF_MODEL_PRICING_JSON")
    if not raw:
        return DEFAULT_CATALOG
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelPolicyViolation("HF_MODEL_PRICING_JSON is not valid JSON") from exc
    if not isinstance(overrides, dict):
        raise ModelPolicyViolation("HF_MODEL_PRICING_JSON must be an object keyed by model id")
    result: list[HuggingFaceModel] = []
    for model in DEFAULT_CATALOG:
        override = overrides.get(model.model_id)
        if not isinstance(override, dict):
            result.append(model)
            continue
        try:
            result.append(replace(model, input_usd_per_1m=Decimal(str(override["input_usd_per_1m"])), output_usd_per_1m=Decimal(str(override["output_usd_per_1m"])), pricing_source=str(override.get("source", "operator_config"))))
        except (KeyError, ValueError) as exc:
            raise ModelPolicyViolation(f"invalid pricing override for {model.model_id}") from exc
    return tuple(result)


def catalog(capability: str | None = None) -> list[HuggingFaceModel]:
    models = _load_catalog()
    if capability:
        models = tuple(model for model in models if capability in model.capabilities)
    return list(models)


def quote_model(model: HuggingFaceModel, input_tokens: int, output_tokens: int) -> Decimal:
    if input_tokens < 0 or output_tokens < 0:
        raise ModelPolicyViolation("token estimates cannot be negative")
    if not model.pricing_verified:
        raise ModelPolicyViolation(f"verified pricing is required before executing {model.model_id}")
    cost = (Decimal(input_tokens) * model.input_usd_per_1m / Decimal(1_000_000)) + (Decimal(output_tokens) * model.output_usd_per_1m / Decimal(1_000_000))
    return cost.quantize(Decimal("0.000001"), rounding=ROUND_UP)


def select_model(*, capabilities: list[str], input_tokens: int, output_tokens: int, max_cost: Decimal, minimum_quality: float = 0.0, routing_policy: str = "cheapest", preferred_providers: list[str] | None = None, model_id: str | None = None) -> tuple[HuggingFaceModel, Decimal, list[dict[str, object]]]:
    if routing_policy not in {"cheapest", "fastest", "preferred"}:
        raise ModelPolicyViolation("routing_policy must be cheapest, fastest, or preferred")
    if max_cost < 0:
        raise ModelPolicyViolation("max_cost cannot be negative")
    candidates: list[tuple[HuggingFaceModel, Decimal]] = []
    pricing_blocked = False
    for model in catalog():
        if model_id and model.model_id != model_id:
            continue
        if model.status not in {"candidate", "qualified", "approved"}:
            continue
        if not set(capabilities).issubset(set(model.capabilities)):
            continue
        if model.quality_score * model.reliability < minimum_quality:
            continue
        try:
            cost = quote_model(model, input_tokens, output_tokens)
        except ModelPolicyViolation:
            pricing_blocked = True
            continue
        if cost <= max_cost:
            candidates.append((model, cost))
    if not candidates:
        if pricing_blocked:
            raise ModelPolicyViolation("verified pricing is required before selecting a Hugging Face model")
        raise ModelPolicyViolation("NO_HUGGINGFACE_MODEL_WITHIN_VERIFIED_ECONOMIC_ENVELOPE")
    preferred = preferred_providers or []
    if routing_policy == "cheapest":
        candidates.sort(key=lambda item: (item[1], -item[0].quality_score))
    elif routing_policy == "fastest":
        candidates.sort(key=lambda item: (item[0].latency_ms, item[1]))
    else:
        candidates.sort(key=lambda item: (min((preferred.index(provider) for provider in item[0].providers if provider in preferred), default=999), item[1]))
    selected, cost = candidates[0]
    alternatives = [{"model_id": model.model_id, "estimated_cost": str(model_cost), "quality_score": model.quality_score, "latency_ms": model.latency_ms} for model, model_cost in candidates[1:]]
    return selected, cost, alternatives


class HuggingFaceChatAdapter:
    endpoint = "https://router.huggingface.co/v1/chat/completions"

    def __init__(self, token: str | None = None, timeout_s: float = 45.0):
        self.token = token or os.environ.get("HF_TOKEN")
        self.timeout_s = timeout_s

    def chat(self, model: HuggingFaceModel, messages: list[dict[str, str]], *, routing_policy: str = "cheapest", provider: str | None = None, max_tokens: int = 512) -> dict[str, object]:
        if not self.token:
            raise HuggingFaceProviderUnavailable("HF_TOKEN is not configured; provider execution is disabled")
        if not model.pricing_verified:
            raise HuggingFaceProviderUnavailable("verified model pricing is required before provider execution")
        suffix = provider or routing_policy
        routed_model = f"{model.model_id}:{suffix}"
        payload = json.dumps({"model": routed_model, "messages": messages, "max_tokens": max_tokens}).encode()
        req = request.Request(self.endpoint, data=payload, headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}, method="POST")
        started = time.perf_counter()
        try:
            with request.urlopen(req, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode())
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            raise HuggingFaceProviderUnavailable(f"Hugging Face provider request failed: {exc}") from exc
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        return {"model_id": model.model_id, "routed_model": routed_model, "message": body.get("choices", [{}])[0].get("message", {}) if isinstance(body, dict) else {}, "usage": usage, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
