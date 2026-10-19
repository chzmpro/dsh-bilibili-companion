"""User-managed external MCP backends; credentials stay in environment variables."""

from __future__ import annotations

import re
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExternalPlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    transport: Literal["http", "stdio"] = "http"
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env_names: list[str] = Field(default_factory=list)
    headers_env: dict[str, str] = Field(default_factory=dict)
    account_id: str = Field(default="default", min_length=1, max_length=128)
    timeout_sec: float = Field(default=30, ge=1, le=600)
    min_interval_seconds: float = Field(default=5, ge=1, le=600)
    cache_ttl_seconds: int = Field(default=3600, ge=0, le=604800)
    asr_enabled: bool = False

    @field_validator("env_names", "headers_env")
    @classmethod
    def validate_environment_names(
        cls, value: list[str] | dict[str, str]
    ) -> list[str] | dict[str, str]:
        names = value.values() if isinstance(value, dict) else value
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
            raise ValueError("只允许填写环境变量名，不可填写密钥值")
        return value

    @model_validator(mode="after")
    def validate_endpoint(self) -> Self:
        if not self.enabled:
            return self
        if self.transport == "stdio":
            if not self.command.strip():
                raise ValueError("stdio backend 需要明确配置已安装的 command")
        else:
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("HTTP backend 需要 http(s) URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("backend URL 不得包含凭据、查询参数或片段；请使用 headers_env")
        return self


class ExternalPlatformsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    xiaohongshu: ExternalPlatformConfig = Field(default_factory=ExternalPlatformConfig)
    douyin: ExternalPlatformConfig = Field(default_factory=ExternalPlatformConfig)
