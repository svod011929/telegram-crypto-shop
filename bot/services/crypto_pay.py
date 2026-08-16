from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class CryptoPayError(RuntimeError):
    """Base error for Crypto Pay operations."""


class CryptoPayAPIError(CryptoPayError):
    def __init__(self, method: str, error: str) -> None:
        super().__init__(f"Crypto Pay {method}: {error}")
        self.method = method
        self.error = error


class CryptoPayTransportError(CryptoPayError):
    """Network or malformed-response failure with an uncertain remote outcome."""


class CryptoPayClient:
    def __init__(self, token: str, base_url: str, timeout_seconds: int = 15) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"Crypto-Pay-API-Token": self._token},
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        safe_retry: bool = False,
    ) -> Any:
        await self.start()
        if self._session is None:
            raise CryptoPayTransportError("Crypto Pay session is unavailable")
        attempts = 3 if safe_retry else 1
        for attempt in range(1, attempts + 1):
            try:
                async with self._session.post(
                    f"{self._base_url}/{method}", json=params or {}
                ) as response:
                    if response.status >= 500:
                        raise CryptoPayTransportError(
                            f"HTTP {response.status} on {method}"
                        )
                    if response.status >= 400:
                        body = (await response.text())[:300]
                        raise CryptoPayAPIError(
                            method, f"HTTP {response.status}: {body}"
                        )
                    try:
                        payload = await response.json()
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise CryptoPayTransportError(
                            f"Malformed JSON on {method}"
                        ) from exc
                    if not payload.get("ok"):
                        error = (
                            payload.get("error")
                            or payload.get("error_code")
                            or "unknown API error"
                        )
                        raise CryptoPayAPIError(method, str(error))
                    return payload.get("result")
            except CryptoPayAPIError:
                raise
            except (
                aiohttp.ClientError,
                TimeoutError,
                CryptoPayTransportError,
            ) as exc:
                logger.warning(
                    "Crypto Pay transport failure on %s (attempt %s/%s): %s",
                    method,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt == attempts:
                    raise CryptoPayTransportError(
                        f"Transport failure on {method}"
                    ) from exc
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
        raise CryptoPayTransportError(f"Unreachable retry state on {method}")

    @staticmethod
    def _items(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, list):
            return [dict(item) for item in result]
        if isinstance(result, dict) and isinstance(result.get("items"), list):
            return [dict(item) for item in result["items"]]
        return []

    async def get_me(self) -> dict[str, Any]:
        return dict(await self._request("getMe", safe_retry=True))

    async def create_invoice(self, **params: Any) -> dict[str, Any]:
        return dict(await self._request("createInvoice", params))

    async def get_invoices(self, **params: Any) -> list[dict[str, Any]]:
        return self._items(await self._request("getInvoices", params, safe_retry=True))

    async def delete_invoice(self, invoice_id: int) -> bool:
        return bool(
            await self._request(
                "deleteInvoice", {"invoice_id": invoice_id}, safe_retry=True
            )
        )

    async def transfer(self, **params: Any) -> dict[str, Any]:
        return dict(await self._request("transfer", params))

    async def get_transfers(self, **params: Any) -> list[dict[str, Any]]:
        return self._items(await self._request("getTransfers", params, safe_retry=True))

    async def get_balance(self) -> list[dict[str, Any]]:
        return self._items(await self._request("getBalance", safe_retry=True))

    async def get_exchange_rates(self) -> list[dict[str, Any]]:
        return self._items(await self._request("getExchangeRates", safe_retry=True))

    async def get_currencies(self) -> Any:
        return await self._request("getCurrencies", safe_retry=True)

    async def get_stats(self, **params: Any) -> dict[str, Any]:
        return dict(await self._request("getStats", params, safe_retry=True))

    # CamelCase aliases mirror the official method names for integrations.
    getMe = get_me
    createInvoice = create_invoice
    getInvoices = get_invoices
    deleteInvoice = delete_invoice
    getTransfers = get_transfers
    getBalance = get_balance
    getExchangeRates = get_exchange_rates
    getCurrencies = get_currencies
    getStats = get_stats
