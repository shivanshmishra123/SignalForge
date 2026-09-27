"""Slack provider adapter."""

from typing import Protocol

import httpx

from app.domains.briefings import FakeSlackProvider, SlackProvider


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class DisabledSlackProvider:
    """Provider used outside development when Slack was intentionally disabled."""

    async def publish(
        self, *, channel_id: str, blocks: list[dict[str, object]], idempotency_key: str
    ) -> tuple[str, str]:
        raise RuntimeError(
            "Slack delivery is disabled; set SLACK_BACKEND=slack and configure SLACK_BOT_TOKEN."
        )

    async def reply(
        self, *, channel_id: str, thread_ts: str, text: str, idempotency_key: str
    ) -> tuple[str, str]:
        raise RuntimeError(
            "Slack delivery is disabled; set SLACK_BACKEND=slack and configure SLACK_BOT_TOKEN."
        )


class SlackApiProvider:
    def __init__(
        self,
        token: str,
        client: httpx.AsyncClient | None = None,
        api_url: str = "https://slack.com/api",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not token.strip():
            raise ValueError("A Slack bot token is required to construct the provider.")
        if timeout_seconds <= 0:
            raise ValueError("Slack timeout must be greater than zero.")
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def _post(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        authorization = "Bearer " + self._token
        try:
            response = await self._client.post(
                f"{self._api_url}/{method}",
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
        except httpx.TimeoutException as exc:
            raise RuntimeError("Slack request timed out; retry the delivery.") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("Slack request failed; retry the delivery.") from exc
        except ValueError as exc:
            raise RuntimeError("Slack returned an unreadable response.") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Slack returned an unexpected response.")
        if result.get("ok") is not True:
            error = result.get("error")
            safe_error = error if isinstance(error, str) and error else "unknown_error"
            raise RuntimeError(f"Slack rejected the request: {safe_error}.")
        return result

    async def publish(
        self, *, channel_id: str, blocks: list[dict[str, object]], idempotency_key: str
    ) -> tuple[str, str]:
        payload = await self._post(
            "chat.postMessage",
            {
                "channel": channel_id,
                "blocks": blocks,
                "text": "SignalForge weekly briefing",
            },
        )
        timestamp = str(payload.get("ts", idempotency_key))
        return timestamp, str(payload.get("ts", ""))

    async def reply(
        self, *, channel_id: str, thread_ts: str, text: str, idempotency_key: str
    ) -> tuple[str, str]:
        payload = await self._post(
            "chat.postMessage",
            {"channel": channel_id, "thread_ts": thread_ts, "text": text},
        )
        timestamp = str(payload.get("ts", idempotency_key))
        return timestamp, str(payload.get("ts", thread_ts))

    async def close(self) -> None:
        await self._client.aclose()


__all__ = [
    "DisabledSlackProvider",
    "FakeSlackProvider",
    "SlackApiProvider",
    "SlackProvider",
]
