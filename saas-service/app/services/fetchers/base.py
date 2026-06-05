from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List


@dataclass
class FetchResult:
    source_channel: str
    source_url: str
    text: str
    author_role_hint: str = "unknown"
    fetched_at: str = ""

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat()
