from typing import Any, Optional

GEMINI_PRICING: dict[str, dict[str, float]] = {
    # Gemini 3.1 family
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "cached": 0.025},
    "gemini-3.1-pro":        {"input": 2.00, "output": 12.00, "cached": 0.20},

    # Gemini 3 family
    "gemini-3-flash":        {"input": 0.50, "output": 3.00,  "cached": 0.05},

    # Gemini 2.5 family
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40,  "cached": 0.01},
    "gemini-2.5-flash":      {"input": 0.30, "output": 2.50,  "cached": 0.03},
    "gemini-2.5-pro":        {"input": 1.25, "output": 10.00, "cached": 0.125},

    # Gemini 2.0 family (deprecated June 2026, kept for back-compat)
    "gemini-2.0-flash-lite": {"input": 0.075, "output": 0.30, "cached": 0.0},
    "gemini-2.0-flash":      {"input": 0.10,  "output": 0.40, "cached": 0.025},
}


def _lookup_pricing(model_name: str) -> Optional[dict[str, float]]:
    """Find pricing for a model name by case-insensitive substring match.
    Returns None if no entry in GEMINI_PRICING matches."""
    if not model_name:
        return None
    name_lower = model_name.lower()
    # Iterate in declared dict order — more specific keys first
    for key, prices in GEMINI_PRICING.items():
        if key in name_lower:
            return prices
    return None


def _compute_costs_for_model(
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    prices: dict[str, float],
) -> tuple[float, float, float]:
    """Returns (prompt_cost_excl_cache, cached_cost, completion_cost) in USD."""
    non_cached_prompt = max(0, prompt_tokens - cached_tokens)
    prompt_cost = non_cached_prompt * prices["input"] / 1_000_000
    cached_cost = cached_tokens * prices.get("cached", 0.0) / 1_000_000
    completion_cost = completion_tokens * prices["output"] / 1_000_000
    return prompt_cost, cached_cost, completion_cost


def _zero_payload(
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    entry_count: int,
    reason: str,
) -> dict[str, Any]:
    """costData with zero costs, tagged with why we couldn't compute."""
    return {
        "totalPromptTokens": prompt_tokens,
        "totalCompletionTokens": completion_tokens,
        "totalTokens": total_tokens,
        "totalCost": 0.0,
        "totalPromptCost": 0.0,
        "totalCompletionCost": 0.0,
        "totalCachedTokens": cached_tokens,
        "totalCachedCost": 0.0,
        "entryCount": entry_count,
        "costSource": f"unavailable_{reason}",
    }


def fill_missing_cost(usage: Any) -> Optional[dict[str, Any]]:
    """
    Build a costData dict from a browser-use usage summary.

    - If usage.total_cost > 0  → pass through what browser-use computed
      (tagged costSource='browser_use').
    - If usage.total_cost == 0 → compute locally from per-model token counts
      via GEMINI_PRICING (tagged costSource='calculated_local').
    - If usage is missing/unreadable → return None.

    The returned dict has the same keys callers already expect from the old
    extract_cost_data, plus 'costSource' for traceability.
    """
    if usage is None:
        return None

    try:
        prompt_tokens = int(usage.total_prompt_tokens or 0)
        cached_tokens = int(usage.total_prompt_cached_tokens or 0)
        completion_tokens = int(usage.total_completion_tokens or 0)
        total_tokens = int(usage.total_tokens or 0)
        entry_count = int(usage.entry_count or 0)
        reported_total = float(usage.total_cost or 0)
        reported_prompt = float(usage.total_prompt_cost or 0)
        reported_cached = float(usage.total_prompt_cached_cost or 0)
        reported_completion = float(usage.total_completion_cost or 0)
    except Exception as e:
        print(f"[cost] failed to read usage fields: {e}")
        return None

    # ── Happy path: browser-use already gave us a real cost ──
    if reported_total > 0:
        return {
            "totalPromptTokens": prompt_tokens,
            "totalCompletionTokens": completion_tokens,
            "totalTokens": total_tokens,
            "totalCost": reported_total,
            "totalPromptCost": reported_prompt,
            "totalCompletionCost": reported_completion,
            "totalCachedTokens": cached_tokens,
            "totalCachedCost": reported_cached,
            "entryCount": entry_count,
            "costSource": "browser_use",
        }

    # ── Cost is 0 — compute locally per model ──
    by_model = getattr(usage, "by_model", None) or {}
    if not by_model:
        print("[cost] total_cost=0 and no by_model breakdown — cannot compute")
        return _zero_payload(
            prompt_tokens, cached_tokens, completion_tokens,
            total_tokens, entry_count, reason="no_by_model",
        )

    total_prompt_cost = 0.0
    total_cached_cost = 0.0
    total_completion_cost = 0.0
    unmatched: list[str] = []

    for model_name, stats in by_model.items():
        prices = _lookup_pricing(model_name)
        if not prices:
            unmatched.append(model_name)
            print(f"[cost] no pricing found for model='{model_name}' — counted as $0. "
                  f"Add it to GEMINI_PRICING in cost_calculator.py")
            continue

        m_prompt = int(getattr(stats, "prompt_tokens", 0) or 0)
        m_completion = int(getattr(stats, "completion_tokens", 0) or 0)

        # Cached tokens may not be exposed per-model. If missing, distribute
        # the global cached-token count proportionally to this model's share
        # of the total prompt tokens.
        m_cached = int(getattr(stats, "prompt_cached_tokens", 0) or 0)
        if m_cached == 0 and prompt_tokens > 0 and cached_tokens > 0:
            ratio = m_prompt / prompt_tokens
            m_cached = int(cached_tokens * ratio)

        p_cost, c_cost, o_cost = _compute_costs_for_model(
            m_prompt, m_cached, m_completion, prices,
        )
        total_prompt_cost += p_cost
        total_cached_cost += c_cost
        total_completion_cost += o_cost

    grand_total = total_prompt_cost + total_cached_cost + total_completion_cost

    print(
        f"[cost] computed locally: prompt=${total_prompt_cost:.6f} "
        f"cached=${total_cached_cost:.6f} completion=${
            total_completion_cost:.6f} "
        f"total=${grand_total:.6f}"
        + (f" unmatched_models={unmatched}" if unmatched else "")
    )

    payload = {
        "totalPromptTokens": prompt_tokens,
        "totalCompletionTokens": completion_tokens,
        "totalTokens": total_tokens,
        "totalCost": round(grand_total, 6),
        "totalPromptCost": round(total_prompt_cost, 6),
        "totalCompletionCost": round(total_completion_cost, 6),
        "totalCachedTokens": cached_tokens,
        "totalCachedCost": round(total_cached_cost, 6),
        "entryCount": entry_count,
        "costSource": "calculated_local",
    }
    if unmatched:
        payload["unmatchedModels"] = unmatched
    return payload
