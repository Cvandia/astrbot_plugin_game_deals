"""Read-only live preview; does not subscribe or send chat messages."""

import asyncio
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from astrbot_plugin_game_deals.digest_image import DigestImageRenderer
from astrbot_plugin_game_deals.jump import JumpSource

PREVIEW_DIR = Path(__file__).resolve().parent.parent / "game_deals_preview"


async def main():
    source = JumpSource(max_pages=20)
    try:
        games = await source.today(5)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        snapshot = {
            "day": now.date().isoformat(),
            "source": "jump",
            "games": [asdict(g) for g in games],
            "page": 1,
            "pages": 1,
            "total": len(games),
            "start": 0,
            "timezone": "Asia/Shanghai",
            "generated": now.strftime("%Y-%m-%d %H:%M"),
        }
        path = await asyncio.to_thread(DigestImageRenderer(PREVIEW_DIR).render, snapshot)
        print(path)
    finally:
        await source.close()


if __name__ == "__main__":
    asyncio.run(main())
