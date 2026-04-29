import json
import time
import config

KNOWN_MODELS = {
    "sonnet": [
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
        {"id": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4"},
    ],
    "haiku": [
        {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 (alt)"},
    ],
}

MODEL_FALLBACKS = {
    "claude-sonnet-4-6":          ["claude-sonnet-4-5", "claude-sonnet-4-20250514"],
    "claude-sonnet-4-5":          ["claude-sonnet-4-6", "claude-sonnet-4-20250514"],
    "claude-sonnet-4-20250514":   ["claude-sonnet-4-6", "claude-sonnet-4-5"],
    "claude-haiku-4-5-20251001":  ["claude-haiku-4-5"],
    "claude-haiku-4-5":           ["claude-haiku-4-5-20251001"],
}

_detected: dict = {}   # {"sonnet": "claude-sonnet-4-6", "haiku": "..."}
_last_detect: float = 0


def _load_saved() -> dict:
    try:
        if config.SETTINGS_PATH.exists():
            d = json.loads(config.SETTINGS_PATH.read_text())
            return d.get("models", {})
    except Exception:
        pass
    return {}


def _save_detected(models: dict):
    try:
        d = {}
        if config.SETTINGS_PATH.exists():
            d = json.loads(config.SETTINGS_PATH.read_text())
        d["models"] = models
        config.SETTINGS_PATH.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


def detect_models(api_key: str = None) -> dict:
    global _detected, _last_detect
    api_key = api_key or config.ANTHROPIC_API_KEY
    if not api_key:
        return {}

    import anthropic
    detected = {}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.models.list(limit=100)
        available = {m.id for m in response.data}
        for tier, models in KNOWN_MODELS.items():
            for m in models:
                if m["id"] in available:
                    detected[tier] = m["id"]
                    break
    except Exception as e:
        # Fall back to trial if list endpoint unavailable
        import anthropic as ant
        client = ant.Anthropic(api_key=api_key)
        for tier, models in KNOWN_MODELS.items():
            for m in models:
                try:
                    client.messages.create(
                        model=m["id"], max_tokens=5,
                        messages=[{"role": "user", "content": "hi"}]
                    )
                    detected[tier] = m["id"]
                    break
                except ant.NotFoundError:
                    continue
                except Exception:
                    break

    if detected:
        _detected.update(detected)
        _last_detect = time.time()
        _save_detected(_detected)
    return detected


def get_model(tier: str) -> str:
    global _detected
    # Use in-memory cache first
    if _detected.get(tier):
        return _detected[tier]
    # Try saved settings
    saved = _load_saved()
    if saved.get(tier):
        _detected[tier] = saved[tier]
        return saved[tier]
    # Auto-detect if key available
    if config.ANTHROPIC_API_KEY:
        detected = detect_models()
        if detected.get(tier):
            return detected[tier]
    # Hard fallback
    return KNOWN_MODELS[tier][0]["id"]


def create_message(tier: str, **kwargs):
    """Drop-in wrapper for client.messages.create() with automatic fallback."""
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    model_id = get_model(tier)
    kwargs["model"] = model_id

    try:
        return client.messages.create(**kwargs)
    except anthropic.NotFoundError:
        fallbacks = MODEL_FALLBACKS.get(model_id, [])
        for fb in fallbacks:
            try:
                kwargs["model"] = fb
                result = client.messages.create(**kwargs)
                # Update detected model to the one that worked
                _detected[tier] = fb
                _save_detected(_detected)
                return result
            except anthropic.NotFoundError:
                continue
        raise RuntimeError(
            f"Model {model_id} not found and all fallbacks failed: {fallbacks}. "
            f"Go to Settings → Models and click 'Detect Available Models'."
        )


def get_status() -> dict:
    return {
        "detected": _detected or _load_saved(),
        "last_detect": _last_detect,
        "known": KNOWN_MODELS,
    }


# Warm up on import if key already saved
if config.ANTHROPIC_API_KEY and not _load_saved():
    try:
        detect_models()
    except Exception:
        pass
elif _load_saved():
    _detected.update(_load_saved())
