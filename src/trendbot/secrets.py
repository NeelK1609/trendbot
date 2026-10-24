"""Kraken API credentials from Azure Key Vault (managed identity in Azure, `az login` locally)."""

from __future__ import annotations

from .kraken import KrakenClient

KEY_NAME = "kraken-api-key"
SECRET_NAME = "kraken-api-secret"


def kraken_from_vault(vault_url: str) -> KrakenClient:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    vault = SecretClient(vault_url, DefaultAzureCredential())
    return KrakenClient(vault.get_secret(KEY_NAME).value, vault.get_secret(SECRET_NAME).value)
