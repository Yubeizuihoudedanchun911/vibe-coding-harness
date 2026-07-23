from __future__ import annotations

from vibe.providers.base import ProviderAdapter


def provider_adapter(name: str) -> ProviderAdapter:
    if name == "codex-cli":
        from vibe.providers.codex_cli import CodexCLIAdapter

        return CodexCLIAdapter()
    raise ValueError(f"unsupported provider: {name}")
