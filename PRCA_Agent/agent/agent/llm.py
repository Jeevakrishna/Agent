"""LLM factory — returns a LangChain chat model based on the configured provider.

Only free-tier providers are wired up by default. See each provider's
comments in .env.example for where to get free API keys. The factory can
be extended later, but the default wiring stays $0.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent.config import get_settings


def get_chat_model(**model_kwargs: Any) -> BaseChatModel:
    """Return a LangChain chat model for the configured LLM_PROVIDER.

    Supports providers with meaningful free tiers:
      - gemini     (Google Gemini API, free tier)
      - groq       (Groq Cloud, free tier)
      - openrouter (OpenRouter, free-tier models / free credits)
      - ollama     (fully local, no API key needed)

    All model names come from env vars (see Settings). Model-specific kwargs
    can be passed through and will be forwarded to the constructor.
    """
    settings = get_settings()
    provider = settings.LLM_PROVIDER.lower()

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            google_api_key=settings.GEMINI_API_KEY or None,
            **model_kwargs,
        )

    if provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=settings.GROQ_MODEL,
            api_key=settings.GROQ_API_KEY or None,
            **model_kwargs,
        )

    if provider == "openrouter":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.OPENROUTER_MODEL,
            api_key=settings.OPENROUTER_API_KEY or None,
            base_url="https://openrouter.ai/api/v1",
            **model_kwargs,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.OLLAMA_MODEL,
            **model_kwargs,
        )

    raise ValueError(
        f"Unsupported LLM_PROVIDER '{settings.LLM_PROVIDER}'. "
        "Supported providers: gemini, groq, openrouter, ollama."
    )
