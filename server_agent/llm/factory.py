from __future__ import annotations

from server_agent.config import Settings, get_settings
from server_agent.llm.base import LLMClient, LLMError


def create_llm(settings: Settings | None = None, *, mock: bool = False) -> LLMClient:
    """根据配置创建模型客户端。provider=mock 或 mock=True 时返回 echo 模式的 MockLLM。"""
    s = settings or get_settings()
    if mock or s.llm_provider == "mock":
        from server_agent.llm.mock import MockLLM

        return MockLLM()
    missing = [k for k, v in {"LLM_BASE_URL": s.llm_base_url, "LLM_MODEL": s.llm_model}.items() if not v]
    if missing:
        raise LLMError(f"缺少模型配置: {', '.join(missing)}。请在 .env 中设置，或用 --mock 体验")
    from server_agent.llm.openai_compat import OpenAICompatClient

    return OpenAICompatClient(
        s.llm_base_url,
        s.llm_api_key,
        s.llm_model,
        temperature=s.llm_temperature,
        timeout=s.llm_timeout,
        max_retries=s.llm_max_retries,
        stream_usage=s.llm_stream_usage,
        extra_body=s.llm_extra_body,
    )
