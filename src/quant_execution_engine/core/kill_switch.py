"""Global kill-switch (hard rule 3): checked FIRST in the submit path.

Engaged = the boot-time env flag OR the runtime Redis trip key. The env flag
is the unkillable backstop: it cannot be disengaged at runtime (env wins).
Trip semantics (ADR §G): reject all new submits with a typed error AND
mass-cancel open orders — the mass-cancel sweep itself lives on the router
(it needs the store + adapters); the admin route runs both.
"""

from __future__ import annotations

import logging
from typing import Any

from src.quant_execution_engine.cache.errors import CacheError
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.errors import (
    KillSwitchEngagedError,
    KillSwitchPinnedError,
)

logger = logging.getLogger(__name__)

KILL_SWITCH_KEY = "exe:kill_switch"


class KillSwitch:
    """Env-flag + Redis-key switch state."""

    def __init__(self, settings: Settings, redis: Any | None) -> None:
        self._settings = settings
        self._redis = redis

    async def status(self) -> tuple[bool, str | None]:
        """Return ``(engaged, source)`` where source is ``env`` | ``redis`` | None."""
        if self._settings.kill_switch_engaged:
            return True, "env"
        if self._redis is not None:
            try:
                if await self._redis.get(KILL_SWITCH_KEY) is not None:
                    return True, "redis"
            except Exception:  # noqa: BLE001 - env flag remains the backstop
                logger.warning("kill-switch redis read failed; using env flag only")
        return False, None

    async def assert_disengaged(self) -> None:
        engaged, source = await self.status()
        if engaged:
            raise KillSwitchEngagedError(
                f"kill switch engaged (source: {source}); all new submits rejected"
            )

    async def engage(self) -> None:
        """Trip the runtime switch (no TTL — it stays until disengaged)."""
        if self._redis is None:
            raise CacheError("redis unavailable; cannot trip the runtime kill-switch")
        await self._redis.set(KILL_SWITCH_KEY, "engaged")
        logger.warning("kill switch ENGAGED (runtime trip)")

    async def disengage(self) -> None:
        """Clear the runtime trip; refuses while the env flag pins it on."""
        if self._settings.kill_switch_engaged:
            raise KillSwitchPinnedError(
                "kill switch is pinned by EXECUTION_ENGINE_KILL_SWITCH_ENGAGED; "
                "unset the environment flag to disengage"
            )
        if self._redis is None:
            raise CacheError("redis unavailable; cannot clear the runtime kill-switch")
        await self._redis.delete(KILL_SWITCH_KEY)
        logger.warning("kill switch disengaged")
