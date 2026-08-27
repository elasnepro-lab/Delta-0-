"""Runtime env-var settings (secrets, RPC URLs, wallet addresses).

Never mixed into `config.yaml`. See `.env.example`.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    arbitrum_rpc_primary: str = Field(...)
    arbitrum_rpc_fallback: str = Field(default="")

    bot_master_address: str = Field(...)
    bot_master_private_key: SecretStr = Field(default=SecretStr(""))

    hl_agent_address: str = Field(default="")
    hl_agent_private_key: SecretStr = Field(default=SecretStr(""))

    tg_token: SecretStr = Field(default=SecretStr(""))
    tg_chat: str = Field(default="")

    delta0_mode: str = Field(default="DRY_RUN")


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
