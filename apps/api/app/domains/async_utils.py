import inspect
from collections.abc import Awaitable


async def maybe_await[T](value: T | Awaitable[T]) -> T:
    """Allow the domain boundary to support sync memory and async durable adapters."""
    if inspect.isawaitable(value):
        return await value
    return value
