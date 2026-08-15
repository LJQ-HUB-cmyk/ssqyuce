"""ssq.TXT 行解析。数据源格式（已实测验证，每行固定 31 字段）：

期号 日期 红1..红6(升序) 蓝 红1..红6(摇奖顺序) 销量 奖池
一等奖注数 一等奖奖金 二等奖注数 二等奖奖金 三等奖注数 三等奖奖金
四等奖注数 四等奖奖金 五等奖注数 五等奖奖金 六等奖注数 六等奖奖金 预留 预留
"""
from __future__ import annotations

from typing import Dict, List

FIELD_COUNT = 31


class ParseError(ValueError):
    pass


def parse_line(line: str) -> Dict:
    parts = line.split()
    if len(parts) != FIELD_COUNT:
        raise ParseError(f"字段数异常: {len(parts)} != {FIELD_COUNT} | 行: {line[:80]}")
    issue, date = parts[0], parts[1]
    reds = [int(x) for x in parts[2:8]]
    blue = int(parts[8])
    order = [int(x) for x in parts[9:15]]
    try:
        sales, pool = int(parts[15]), int(parts[16])
        prizes = [int(x) for x in parts[17:29]]
    except ValueError:
        sales, pool, prizes = 0, 0, [0] * 12
    if len(set(reds)) != 6 or not all(1 <= x <= 33 for x in reds):
        raise ParseError(f"红球非法: {reds}")
    if not 1 <= blue <= 16:
        raise ParseError(f"蓝球非法: {blue}")
    return {
        "issue": issue,
        "date": date,
        "reds": reds,
        "blue": blue,
        "order": order,
        "sales": sales,
        "pool": pool,
        "prizes": prizes,
    }


def parse_text(text: str) -> List[Dict]:
    draws = []
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            draws.append(parse_line(line))
        except ParseError as e:
            raise ParseError(f"第 {i} 行: {e}")
    return draws