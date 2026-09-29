"""配置加载。

优先级（高 -> 低）：进程环境变量 > 工作目录下的 .env 文件 > 代码里的默认值。
所有变量统一加 SA_ 前缀，避免和系统里其它程序的环境变量撞名。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
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

    # ---- 第 15 章：可观测与评测 ----
    trace_enabled: bool = Field(True, description="是否记录 Trace（耗时树）")
    trace_dir: str = Field("data/traces", description="Trace 落盘目录（JSONL）")
    evals_dir: str = Field("evals", description="评测用例目录")

    # ---- 第 13 章：知识库 ----
    knowledge_dir: str = Field("knowledge", description="文档目录（RAG 索引源）")

    # ---- 第 12 章：规划与 Runbook ----
    agent_planning: bool = Field(False, description="是否启用 Plan-and-Execute（先出计划再执行）")
    runbooks_dir: str = Field("runbooks", description="Runbook 手册目录")

    # ---- 第 11 章：沙箱 ----
    sandbox_enabled: bool = Field(True, description="是否允许 Agent 在沙箱里执行自己写的代码")
    sandbox_backend: str = Field("auto", description="auto | local_docker | ags")
    sandbox_timeout: float = Field(30.0, gt=0, le=600, description="单次沙箱执行超时（秒）")
    sandbox_image: str = Field("python:3.12-slim", description="本地 Docker 沙箱镜像")

    # ---- 第 10 章：多主机 ----
    inventory_path: str = Field("inventory.yaml", description="主机清单路径（不存在则只用本机）")
    ssh_connect_timeout: float = Field(10.0, gt=0, description="SSH 连接超时（秒）")

    # ---- 第 09 章：安全策略 ----
    policy_allow_paths: list[str] = Field(default_factory=lambda: ["/tmp", "/var/tmp"],
                                          description="clean_directory 允许的路径前缀（白名单）")
    policy_allow_services: list[str] = Field(default_factory=lambda: ["nginx", "redis", "redis-server"],
                                             description="restart_service 允许的服务白名单")
    policy_require_approval: bool = Field(True, description="high 风险操作是否必须人工审批")
    approval_timeout: float = Field(120.0, gt=0, description="审批等待超时（秒），超时按拒绝处理")
    audit_enabled: bool = Field(True, description="是否写审计日志")
    audit_path: str = Field("data/audit.jsonl", description="审计日志路径（JSONL）")

    # ---- 第 08 章：记忆与上下文 ----
    db_path: str = Field("data/server_agent.db", description="SQLite 数据库路径")
    context_max_tokens: int = Field(32000, ge=1000, description="模型上下文窗口（token）")
    context_reserve_output: int = Field(2000, ge=256, description="为模型输出预留的 token")
    context_keep_recent: int = Field(4, ge=1, description="最近 N 条工具结果不做压缩")
    memory_enabled: bool = Field(True, description="是否把 run 与事件写入 SQLite，并在新会话注入历史记忆")

    # ---- 第 07 章：提示词与结构化报告 ----
    prompt_variant: str = Field("sre", description="提示词变体：sre（带排障方法论）或 plain（对照用）")
    report_repair: bool = Field(True, description="报告解析失败时，追加一次「改写为 JSON」的请求")

    # ---- 第 06 章：前端 ----
    web_dir: str | None = Field(None, description="前端静态目录；默认取仓库根目录下的 web/")

    # ---- 第 05 章：服务 ----
    cors_origins: list[str] = Field(default_factory=list,
                                    description="允许跨域的前端来源，如 ['http://localhost:5173']；空表示不启用 CORS")

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

    @field_validator("prompt_variant")
    @classmethod
    def _check_variant(cls, v: str) -> str:
        from server_agent.prompts import VARIANTS

        v = (v or "sre").lower()
        if v not in VARIANTS:
            raise ValueError(f"未知的提示词变体: {v}（可用：{', '.join(VARIANTS)}）")
        return v

    @field_validator("web_dir", mode="before")
    @classmethod
    def _default_web_dir(cls, v):
        if v:
            return v
        return str(Path(__file__).resolve().parents[1] / "web")

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
