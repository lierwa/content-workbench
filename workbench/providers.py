"""Extension contract; historical providers are registered but not scheduled or run."""
from typing import Protocol

class HistoricalProvider(Protocol):
    source_id: str
    source_name: str
    def collect(self) -> list[dict]:
        """Return basic fields: title, source_url, canonical_url, source_id,
        source_name, source_type='historical', plus nullable event_time,
        published_at, cover_url, platform_snapshot_time and raw metrics.
        Never invent an event date; no article content in this stage.
        """
        ...

HISTORICAL_PROVIDERS: dict[str, HistoricalProvider] = {}

def register_historical(provider: HistoricalProvider):
    if not provider.source_id or provider.source_id in HISTORICAL_PROVIDERS:
        raise ValueError("历史来源 ID 缺失或重复")
    HISTORICAL_PROVIDERS[provider.source_id] = provider

# Registration is an extension point only: no provider ships and no historical
# job executes in this version. A future operator must explicitly enable it.
