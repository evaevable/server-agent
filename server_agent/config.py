"""配置加载。

优先级（高 -> 低）：进程环境变量 > 工作目录下的 .env 文件 > 代码里的默认值。
所有变量统一加 SA_ 前缀，避免和系统里其它程序的环境变量撞名。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field("127.0.0.1", description="监听地址。默认只监听本机，远程访问需显式改成 0.0.0.0")
    port: int = Field(8000, ge=1, le=65535, description="监听端口")
    log_level: str = Field("info", description="日志级别：debug / info / warning / error")
    api_token: str | None = Field(None, description="远程调用鉴权 Token，第 05 章启用")

    # ---- 第 02 章：LLM 配置。同时接受 LLM_XXX 与 SA_LLM_XXX 两种写法 ----
    llm_provider: str = Field("openai_compat", validation_alias=AliasChoices("LLM_PROVIDER", "SA_LLM_PROVIDER"),
                              description="openai_compat 或 mock")
    llm_base_url: str | None = Field(None, validation_alias=AliasChoices("LLM_BASE_URL", "SA_LLM_BASE_URL"),
                                     description="如 https://api.deepseek.com/v1")
    llm_api_key: str | None = Field(None, validation_alias=AliasChoices("LLM_API_KEY", "SA_LLM_API_KEY"))
    llm_model: str | None = Field(None, validation_alias=AliasChoices("LLM_MODEL", "SA_LLM_MODEL"))
    llm_temperature: float = Field(0.2, ge=0, le=2, validation_alias=AliasChoices("LLM_TEMPERATURE", "SA_LLM_TEMPERATURE"))
    llm_timeout: float = Field(60.0, gt=0, validation_alias=AliasChoices("LLM_TIMEOUT", "SA_LLM_TIMEOUT"))
    llm_max_retries: int = Field(2, ge=0, le=10, validation_alias=AliasChoices("LLM_MAX_RETRIES", "SA_LLM_MAX_RETRIES"))
    llm_stream_usage: bool = Field(True, validation_alias=AliasChoices("LLM_STREAM_USAGE", "SA_LLM_STREAM_USAGE"),
                                   description="流式时请求 usage；个别服务不支持 stream_options 时设为 false")
    llm_extra_body: dict[str, Any] = Field(default_factory=dict,
                                           validation_alias=AliasChoices("LLM_EXTRA_BODY", "SA_LLM_EXTRA_BODY"),
                                           description='JSON，合并进请求体的厂商私有参数，如 {"thinking":{"type":"disabled"}}')

    # ---- 第 04 章：Agent 循环 ----
    agent_max_steps: int = Field(12, ge=1, le=50, description="单次提问最多几步（一步=一次模型调用）")
    agent_timeout: float = Field(300.0, gt=0, description="单次提问的总超时（秒）")
    agent_max_tokens: int = Field(1024, ge=64, le=32768, description="模型单次回复的最大 token 数")

    @field_validator("log_level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        v = v.lower()
        if v not in {"debug", "info", "warning", "error"}:
            raise ValueError(f"不支持的日志级别: {v}")
        return v

    @field_validator("api_token", "llm_api_key", "llm_base_url", "llm_model")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return v or None

    def public_dict(self) -> dict:
        """用于打印/接口返回：敏感字段打码。"""
        data = self.model_dump()
        for k in ("api_token", "llm_api_key"):
            if data.get(k):
                data[k] = "***"
        return data


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试里需要换配置时调用 get_settings.cache_clear()。"""
    return Settings()
