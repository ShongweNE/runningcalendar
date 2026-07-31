from dataclasses import dataclass, field
from datetime import date


@dataclass
class Race:
    name: str
    race_date: date
    source_site: str
    source_url: str
    distances: list[str] = field(default_factory=list)
    price_from: float | None = None
    city: str | None = None
    province: str | None = None

    def dedup_key(self) -> tuple[str, date]:
        normalized = "".join(ch.lower() for ch in self.name if ch.isalnum())
        return (normalized, self.race_date)


class ScraperAdapter:
    source_site: str

    def fetch(self) -> list[Race]:
        raise NotImplementedError
