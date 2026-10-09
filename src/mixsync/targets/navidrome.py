from mixsync.ratelimit.client import PoliteClient
from mixsync.targets.subsonic import SubsonicClient


class NavidromeTarget:
    def __init__(self, client: PoliteClient, base_url: str, user: str, password: str) -> None:
        self._subsonic = SubsonicClient(client, base_url, user, password)

    async def rescan(self) -> None:
        await self._subsonic.start_scan()
