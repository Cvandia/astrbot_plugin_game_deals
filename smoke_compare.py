"""Opt-in read-only Jump / Heybox comparison smoke test."""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from astrbot_plugin_game_deals.jump import JumpSource
from astrbot_plugin_game_deals.heybox import HeyboxSource
from astrbot_plugin_game_deals.comparison import Comparison


async def main():
    jump=JumpSource(scope='all')
    heybox=HeyboxSource(max_pages=2)
    try:
        result=await Comparison(jump,heybox).compare('马车8')
        assert '【Jump】' in result and '【小黑盒】' in result,result
        assert 'jump:294' in result and 'heybox:900000090' in result,result
        assert '本次已取得报价中的较低参考值' in result,result
        hits=await heybox.search('马力欧卡丁车8')
        assert any(id_=='heybox:900000090' for id_,_ in hits),hits
        deals=await heybox.today(3)
        assert deals and all(g.discounted for g in deals)
        print('Jump / Heybox comparison, catalogue search and discounts: PASS')
        print(result)
    finally:
        await asyncio.gather(jump.close(),heybox.close())


if __name__=='__main__':asyncio.run(main())
