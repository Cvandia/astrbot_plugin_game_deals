"""Conservative edition matching: titles/regions remain visible, failures are partial."""
import asyncio
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from .deals import SourceError
from .heybox import normalized

# Crosswalk checked against both live catalogues. ID identities are checked again at lookup.
MARIO8 = {'马车8','马车8豪华版','马力欧卡丁车8豪华版','马里奥赛车8豪华版','mariokart8deluxe','mk8dx'}


class Comparison:
    def __init__(self,jump,heybox):
        self.jump,self.heybox=jump,heybox

    async def compare(self,query):
        if not isinstance(query,str) or not query.strip() or len(query)>200:
            raise ValueError('请输入 1-200 字符的游戏名称，例如马车8。')
        key=normalized(query)
        known=key in {normalized(x) for x in MARIO8}
        quotes,errors,candidates=[],[],[]
        if known:
            chosen=[('Jump',self.jump,'jump:294'),('小黑盒',self.heybox,'heybox:900000090')]
            target='马力欧卡丁车8 豪华版'
        else:
            chosen=[]
            results=await asyncio.gather(self.jump.search(query),self.heybox.search(query),return_exceptions=True)
            for (label,source),result in zip([('Jump',self.jump),('小黑盒',self.heybox)],results):
                if isinstance(result,Exception):
                    errors.append(label+'：查询失败，未取得价格')
                    continue
                matches=[(id_,name) for id_,name in result if normalized(name.split(' [')[0])==key]
                if len(matches)==1:chosen.append((label,source,matches[0][0]))
                else:
                    candidates.extend(f'{label}：{name} — {id_}' for id_,name in result)
                    errors.append(label+'：未找到唯一的同名同版本条目；请使用候选的精确名称')
            target=query.strip()
        results=await asyncio.gather(*(src.game(id_) for _,src,id_ in chosen),return_exceptions=True)
        for (label,_,id_),result in zip(chosen,results):
            if isinstance(result,Exception):
                errors.append(label+'：价格查询失败，稍后重试')
                continue
            if result.id!=id_ or normalized(result.name)!=normalized(target):
                errors.append(label+'：条目名称/版本不匹配，未参与比价')
                continue
            if known:
                # Curated mapping is for Switch Deluxe base game; never compare it with World/DLC.
                if label=='Jump' and result.platform!='Switch':
                    errors.append('Jump：平台不匹配');continue
                result=replace(result,platform='Switch')
            quotes.append(result)
        header=f'🎮 综合比价：{target}\n查询时间：'+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M 北京时间')
        text=header+'\n\n'+'\n\n'.join('【'+g.source+'】\n'+g.text() for g in quotes)
        comparable=(len(quotes)>=2 and len({g.platform for g in quotes})==1
                    and all(g.platform in {'Switch','Switch 2'} and g.currency=='CNY≈' and g.final is not None for g in quotes))
        if comparable:
            low=min(g.final for g in quotes)
            winners='、'.join(g.source for g in quotes if g.final==low)
            text+=f'\n\n本次已取得报价中的较低参考值：{winners}，约人民币 {low/100:.2f} 元。'
        elif quotes:
            text+='\n\n报价不完整或平台代际未确认，暂不计算跨来源最低价。'
        if errors:text+='\n\n来源状态：\n'+'\n'.join(errors)
        if candidates:text+='\n\n候选版本：\n'+'\n'.join(candidates[:12])
        text+='\n\n仅比较数字版游戏本体的来源参考价，地区可能不同；不包含实体卡、账号、租赁及会员券后价。两个数据源可能引用同一商店，不是两个独立销售渠道。'
        return text
