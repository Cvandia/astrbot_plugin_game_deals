"""Opt-in live read-only queries. Run: python smoke_test.py [--source steam]."""
import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from astrbot_plugin_game_deals.deals import SteamSource
from astrbot_plugin_game_deals.jump import JumpSource


async def main(source_name):
    source = SteamSource() if source_name == 'steam' else JumpSource()
    try:
        results = await source.search('Hades' if source_name == 'steam' else '马力欧')
        id_ = '1145360' if source_name == 'steam' else 'jump:53387'
        assert any(found == id_ for found, _ in results), results
        game = await source.game(id_)
        assert game.name and game.final is not None
        if source_name != 'steam':
            assert game.platform == 'Switch 2' and game.source == 'Jump'
        featured = await source.today(3)
        assert featured and all(g.discounted for g in featured)
        if source_name != 'steam':
            assert all(g.platform == 'Switch 2' for g in featured)
        print(source_name + ' search / details / discounts: PASS')
        print(game.text())
        print('\nCurrent discount example:\n' + featured[0].text())
    finally:
        await source.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', choices=['jump', 'steam'], default='jump')
    asyncio.run(main(parser.parse_args().source))
