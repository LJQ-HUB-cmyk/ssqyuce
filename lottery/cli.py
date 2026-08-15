"""命令行入口：fetch / stats / backtest / predict / offline_eval / online_check / serve。"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _draws():
    from . import db
    return db.load_draws()


def cmd_fetch(args):
    from . import data_fetcher
    info = data_fetcher.fetch_and_update()
    print(json.dumps(info, ensure_ascii=False, indent=2))


def cmd_stats(args):
    from . import features as F
    draws = _draws()
    st = F.compute_features(draws)
    print(f'最新一期: {st["issue"]} {st["date"]} 红{st["last_reds"]} 蓝{st["last_blue"]}')
    for wname, w in st["windows"].items():
        r, b = w["red"], w["blue"]
        print(f'\n[{wname}] n={r["n_draws"]}')
        print(f'  和值 均值={r["sum_mean"]:.1f} 分位={ {p: round(v,1) for p,v in r["sum_pct"].items()} }')
        print(f'  三区比 Top3: {list(r["zone_hist"].items())[:3]}')
        print(f'  红球热Top6: {r["hot_top6"]}  冷Top6: {r["cold_top6"]}  遗漏Top6: {r["omit_top6"]}')
        print(f'  连号率={r["consecutive_rate"]:.3f} 同尾率={r["same_tail_rate"]:.3f} 重号均值={r["repeat_mean"]:.3f}')
        print(f'  蓝球热Top3: {b["hot_top3"]}  遗漏Top3: {b["omit_top3"]}  重号率={b["repeat_rate"]:.3f}')


def cmd_backtest(args):
    from . import backtest as BT
    draws = _draws()
    results = BT.run_all_backtests(draws, min_start=args.min_start)
    print(f'{"规律":<18}{"窗口":<6}{"n":>6}{"avg":>8}{"exp":>7}{"边际":>8}{"p":>9}{"adj_p":>8} 等级')
    for r in results:
        bt = r.get("backtest", {})
        print(f'{r["name_zh"]:<18}{r["kind"]:<6}{r.get("n",0):>6}'
              f'{bt.get("avg_hits", 0):>8.3f}{bt.get("expected", 0):>7.3f}'
              f'{r.get("margin", 0):>+8.3f}{r["p_value"]:>9.4f}{r.get("p_adj", 1):>8.4f}  {r.get("grade", "C")}')
    print("等级分布:", {g: sum(1 for r in results if r["grade"] == g) for g in "ABC"})
    return results


def cmd_predict(args):
    from . import engine
    draws = _draws()
    res = engine.predict_next(draws, use_llm=None if not args.no_llm else False,
                              n_tickets=args.n)
    print(f'预测期号: {res["issue"]} | LLM: {res["llm_used"]} | 生成时间: {res["generated_at"]}')
    for i, t in enumerate(res["tickets"], 1):
        src = t.get("reasoning", "") or ",".join(t.get("patterns_used", []))
        print(f'  {i:>2}. 红{t["reds"]} 蓝{t["blue"]:>2}  置信度 {t["confidence"]:5.1f}  [{t["method"]}] {src[:60]}')
    print("提示:", res["note"])


def cmd_offline_eval(args):
    from . import evaluate
    draws = _draws()
    res = evaluate.offline_backtest(draws, issues=args.issues, n_tickets=args.n,
                                    use_llm=False)
    s, rnd = res["system"], res["random_baseline"]
    print(f'\n=== 离线引擎回测（{res["n_issues"]} 期 × {res["n_tickets_per_issue"]} 注/期）===')
    print(f'{"指标":<22}{"系统":>12}{"随机基线":>12}')
    print(f'{"红球平均命中":<22}{s["red_hits_mean"]:>12.3f}{rnd["red_hits_mean"]:>12.3f}')
    print(f'{"蓝球命中率":<22}{s["blue_hit_rate"]:>12.4f}{rnd["blue_hit_rate"]:>12.4f}')
    print(f'{">=五等奖命中率":<22}{s["prize_rate_ge5"]:>12.4f}{rnd["prize_rate_ge5"]:>12.4f}')
    print(f'{"总奖金":<22}{s["reward_total"]:>12.1f}{rnd["reward_total"]:>12.1f}')
    print(f'{"总成本":<22}{s["cost_total"]:>12.1f}{rnd["cost_total"]:>12.1f}')
    print(f'{"回报率ROI":<22}{s["roi"]:>12.1%}{rnd["roi"]:>12.1%}')
    print("红球命中分布(系统):", s["red_hits_dist"])
    print("红球命中分布(基线):", rnd["red_hits_dist"])
    print("结论:", res["note"])


def cmd_online(args):
    from . import evaluate
    res = evaluate.online_check()
    print(json.dumps(res["summary"], ensure_ascii=False, indent=2))
    if res["rows"]:
        for r in res["rows"][-10:]:
            print(f'  {r["issue"]} 红命中{r["red_hits"]} 蓝命中{r["blue_hit"]} 奖金{r["reward"]:.0f}')


def cmd_serve(args):
    import uvicorn
    port = getattr(args, "port", None) or int(os.environ.get("PORT", "18000"))
    uvicorn.run("lottery.api_app:app", host=args.host, port=port, log_level="info")


def main(argv=None):
    p = argparse.ArgumentParser(prog="lottery", description="双色球智能预测分析系统")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="抓取最新开奖数据并增量入库")
    sub.add_parser("stats", help="输出最新多尺度统计")
    b = sub.add_parser("backtest", help="运行规律 walk-forward 回测")
    b.add_argument("--min-start", type=int, default=300)
    pr = sub.add_parser("predict", help="生成下一期预测")
    pr.add_argument("-n", type=int, default=10)
    pr.add_argument("--no-llm", action="store_true", help="仅统计模型")
    oe = sub.add_parser("offline_eval", help="离线引擎回测（对照随机基线）")
    oe.add_argument("--issues", type=int, default=120)
    oe.add_argument("-n", type=int, default=10)
    sub.add_parser("online_check", help="在线预测对照评估")
    s = sub.add_parser("serve", help="启动 Web 服务（默认端口 18000，可用 PORT 环境变量覆盖）")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=None)
    args = p.parse_args(argv)
    fn = globals().get("cmd_" + args.cmd)
    if not fn:
        p.error(f"未知命令 {args.cmd}")
    fn(args)


if __name__ == "__main__":
    main()