"""配置加载。

优先级（高 -> 低）：进程环境变量 > 工作目录下的 .env 文件 > 代码里的默认值。
所有变量统一加 SA_ 前缀，避免和系统里其它程序的环境变量撞名。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
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

    @field_validator("log_level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        v = v.lower()
        if v not in {"debug", "info", "warning", "error"}:
            raise ValueError(f"不支持的日志级别: {v}")
        return v

    @field_validator("api_token")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return v or None

    def public_dict(self) -> dict:
        """用于打印/接口返回：敏感字段打码。"""
        data = self.model_dump()
        if data.get("api_token"):
            data["api_token"] = "***"
        return data


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试里需要换配置时调用 get_settings.cache_clear()。"""
    return Settings()
