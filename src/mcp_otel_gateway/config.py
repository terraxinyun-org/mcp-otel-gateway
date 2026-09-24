"""Operator-owned configuration. Secrets are referenced by environment name."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="tools", pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    agent_name: str = Field(default="agent", pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    url: str | None = None
    headers_from_env: dict[str, str] = Field(default_factory=dict)
    pass_env: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=60, gt=0, le=3600)
    capture_content: bool = False
    capture_max_chars: int = Field(default=8192, ge=256, le=32768)

    @model_validator(mode="after")
    def check_transport(self):
        if self.transport == "stdio":
            if not self.command or self.url or self.headers_from_env:
                raise ValueError("stdio needs command; URL and HTTP headers are not applicable")
        elif self.transport == "streamable_http":
            if not self.url or self.command or self.args or self.cwd or self.pass_env:
                raise ValueError("streamable_http needs URL; subprocess settings are not applicable")
            validate_url(self.url)
        else:
            raise ValueError("transport must be stdio or streamable_http")
        for name in self.pass_env + list(self.headers_from_env.values()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("invalid environment variable name")
        return self


def validate_url(value: str) -> None:
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("URL must have a host and no embedded credentials or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("HTTPS required except for loopback development endpoints")


def read_config(path: Path) -> GatewayConfig:
    return GatewayConfig.model_validate(json.loads(path.read_text()))


def child_environment(config: GatewayConfig) -> dict[str, str]:
    # Do not forward the gateway's OTLP authorization or unrelated cloud secrets.
    names = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"} | set(config.pass_env)
    for name in config.pass_env:
        if name not in os.environ:
            raise ValueError("an explicitly requested upstream environment variable is missing")
    return {name: os.environ[name] for name in names if name in os.environ}
