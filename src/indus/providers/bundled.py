"""The provider profiles that ship with Indus Agent, in catalogue order.

Catalogue order is the order ``auto`` uses to pick the first provider with a usable key. Only
providers reachable with an API key are bundled (D-16.42); there is no portal provider (D-16.12)
and no route that borrows another product's identity (D-16.11, R01.12).

Base URLs and key names are third-party facts. Each row cites the vendor page that states them
(checked 2026-10-18); re-check a row against its page before changing it.
"""

from __future__ import annotations

from indus.providers.profile import ProviderProfile

# fmt: off
PROFILES: tuple[ProviderProfile, ...] = (
    # https://openrouter.ai/docs/quickstart (2026-10-18)
    ProviderProfile(
        name="openrouter", aliases=("or",), display_name="OpenRouter",
        description="aggregator: many vendors' models behind one key",
        base_url="https://openrouter.ai/api/v1", env_vars=("OPENROUTER_API_KEY",),
        supports_vision=True,
    ),
    # https://github.com/openai/openai-python README and client defaults (2026-10-18).
    # Chat completions until live Responses captures exist (P08-09 lead decision).
    ProviderProfile(
        name="openai", aliases=("openai-api",), display_name="OpenAI",
        description="OpenAI API",
        base_url="https://api.openai.com/v1", env_vars=("OPENAI_API_KEY",),
        max_tokens_param="max_completion_tokens", supports_vision=True,
        reasoning_models=("o1", "o3", "o4", "gpt-5"),
    ),
    # https://docs.anthropic.com/en/api/overview (2026-10-18)
    ProviderProfile(
        name="anthropic", aliases=("claude",), display_name="Anthropic",
        description="Anthropic Messages API",
        base_url="https://api.anthropic.com", api_mode="anthropic_messages",
        env_vars=("ANTHROPIC_API_KEY",),
        supports_vision=True, supports_model_listing=False,
    ),
    # https://ai.google.dev/gemini-api/docs/api-key and .../docs/text-generation (2026-10-18):
    # key in the x-goog-api-key header; the native generateContent wire (P08-11).
    ProviderProfile(
        name="gemini", aliases=("google",), display_name="Google Gemini",
        description="Gemini API (native generateContent)",
        base_url="https://generativelanguage.googleapis.com/v1beta", api_mode="gemini",
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        supports_vision=True, supports_model_listing=False,
    ),
    # https://api-docs.deepseek.com/ (2026-10-18)
    ProviderProfile(
        name="deepseek", display_name="DeepSeek", description="DeepSeek API",
        base_url="https://api.deepseek.com/v1", env_vars=("DEEPSEEK_API_KEY",),
        reasoning_models=("reasoner",),
    ),
    # https://docs.x.ai/docs/api-reference (2026-10-18)
    ProviderProfile(
        name="xai", aliases=("grok",), display_name="xAI", description="xAI Grok API",
        base_url="https://api.x.ai/v1", env_vars=("XAI_API_KEY",), supports_vision=True,
    ),
    # https://docs.mistral.ai/api/ (2026-10-18)
    ProviderProfile(
        name="mistral", display_name="Mistral AI", description="Mistral La Plateforme API",
        base_url="https://api.mistral.ai/v1", env_vars=("MISTRAL_API_KEY",),
    ),
    # https://huggingface.co/docs/inference-providers/index (2026-10-18)
    ProviderProfile(
        name="huggingface", aliases=("hf",), display_name="Hugging Face",
        description="Hugging Face inference providers router",
        base_url="https://router.huggingface.co/v1", env_vars=("HF_TOKEN",),
    ),
    # https://docs.api.nvidia.com/nim/reference/llm-apis (2026-10-18)
    ProviderProfile(
        name="nvidia", aliases=("nim",), display_name="NVIDIA NIM",
        description="NVIDIA hosted NIM endpoints",
        base_url="https://integrate.api.nvidia.com/v1", env_vars=("NVIDIA_API_KEY",),
    ),
    # https://docs.z.ai/guides/develop/openai/python (2026-10-18)
    ProviderProfile(
        name="zai", aliases=("glm",), display_name="Z.ai", description="Z.ai GLM API",
        base_url="https://api.z.ai/api/paas/v4", env_vars=("ZAI_API_KEY", "GLM_API_KEY"),
    ),
    # https://platform.moonshot.ai/docs/guide/start-using-kimi-api (2026-10-18)
    ProviderProfile(
        name="kimi-coding", aliases=("kimi", "moonshot"), display_name="Kimi (Moonshot AI)",
        description="Moonshot AI Kimi API",
        base_url="https://api.moonshot.ai/v1", env_vars=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    ),
    # https://platform.minimax.io/docs/api-reference/text-anthropic-api (2026-10-18)
    ProviderProfile(
        name="minimax", display_name="MiniMax", description="MiniMax (Anthropic-compatible API)",
        base_url="https://api.minimax.io/anthropic", api_mode="anthropic_messages",
        env_vars=("MINIMAX_API_KEY",), supports_model_listing=False,
    ),
    # https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope
    # (2026-10-18; international endpoint)
    ProviderProfile(
        name="alibaba", aliases=("dashscope", "qwen"), display_name="Alibaba Cloud Model Studio",
        description="Qwen models through DashScope (international)",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        env_vars=("DASHSCOPE_API_KEY",),
    ),
    # https://vercel.com/docs/ai-gateway/openai-compat (2026-10-18)
    ProviderProfile(
        name="ai-gateway", aliases=("vercel",), display_name="Vercel AI Gateway",
        description="Vercel AI Gateway",
        base_url="https://ai-gateway.vercel.sh/v1", env_vars=("AI_GATEWAY_API_KEY",),
    ),
    # https://docs.ollama.com/cloud (2026-10-18)
    ProviderProfile(
        name="ollama-cloud", display_name="Ollama Cloud", description="Ollama's hosted models",
        base_url="https://ollama.com/v1", env_vars=("OLLAMA_API_KEY",),
    ),
    # https://lmstudio.ai/docs/app/api/endpoints/openai (2026-10-18; default port 1234)
    ProviderProfile(
        name="lmstudio", aliases=("lm-studio",), display_name="LM Studio",
        description="LM Studio local server", family="custom", auto_detect=False,
        base_url="http://127.0.0.1:1234/v1", auth_type="api_key_optional",
        env_vars=("LM_API_KEY",), base_url_env_var="LM_BASE_URL",
    ),
    # Any OpenAI-compatible endpoint the user names. The generic OPENAI_API_KEY belongs here
    # (never to another vendor's host); OPENAI_BASE_URL is read only when config names no URL.
    ProviderProfile(
        name="custom", display_name="Custom endpoint",
        description="any OpenAI-compatible endpoint", family="custom", auto_detect=False,
        auth_type="api_key_optional", env_vars=("OPENAI_API_KEY",),
        base_url_env_var="OPENAI_BASE_URL",
    ),
)
# fmt: on


def by_name() -> dict[str, ProviderProfile]:
    """Canonical name → profile (aliases not included)."""
    return {p.name: p for p in PROFILES}


def env_var_names() -> tuple[str, ...]:
    """Every key and base-URL env name the bundled profiles read, in catalogue order."""
    seen: dict[str, None] = {}
    for profile in PROFILES:
        for name in (*profile.env_vars, profile.base_url_env_var):
            if name:
                seen.setdefault(name, None)
    return tuple(seen)


def register_managed_env_names() -> None:
    """Tell the ``.env`` loader these names are ours, so a key removed from ``.env`` is cleared."""
    from indus.foundation.envload import register_managed_keys

    register_managed_keys(*env_var_names())
