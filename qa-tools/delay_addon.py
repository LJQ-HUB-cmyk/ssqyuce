"""mitmproxy addon：只对命中 TARGET 的请求注入延迟（时序缺陷复现用，刘工维护）。

用法（已实测，mitmproxy 12.2.3）：
  # 1) 起代理（只对 TARGET 路径加 3s，其余放行）
  mitmdump -q -s ssqyuce/web/tools/delay_addon.py --set delay=3.0 --set target=/api/items -p 8080
  # 2) 验证代理真的生效（务必走 -x，否则工具跑了但流量没经过代理）
  curl -x http://127.0.0.1:8080 -s -o /dev/null -w 'via proxy %{time_total}s\n' http://TARGET_HOST/api/items
  curl -x http://127.0.0.1:8080 -s -o /dev/null -w 'not match %{time_total}s\n' http://TARGET_HOST/api/other

设计要点：
- 延迟发生在 response 阶段 → 响应已由后端返回但被扣住，用于构造「旧请求慢、新请求快」的顺序；
- 只按 URL 子串匹配，不按路径枚举；
- 无状态：每个命中的请求都延迟，因此「复现率 n/m」可重复（reset 概念不适用）。
- 浏览器要用它必须显式配代理；仅设置 HTTPS_PROXY 环境变量对 Chrome 无效，
  需 `--proxy-server=http://127.0.0.1:8080` 或系统代理。
"""
import time

from mitmproxy import ctx
from mitmproxy import http


class DelayAddon:
    def __init__(self, target: str = "/api/", delay: float = 3.0) -> None:
        self.target = target
        self.delay = delay

    def load(self, loader) -> None:
        # mitmproxy 12.x 的 options 只支持 str/int/... 等有限类型，float 会抛
        # NotImplementedError: unsupported option type: <class 'float'>（v1 踩过）；
        # 因此延迟用 str 传参、在 configure 里解析成 float。
        loader.add_option("delay", str, "3.0", "命中 target 的响应延迟秒数（字符串，支持小数）")
        loader.add_option("target", str, "/api/", "URL 子串匹配，命中则延迟")

    def configure(self, updated) -> None:
        try:
            self.delay = float(ctx.options.delay)
        except (TypeError, ValueError):
            self.delay = 3.0
        self.target = str(ctx.options.target)

    def response(self, flow: http.HTTPFlow) -> None:
        if self.target and self.target in flow.request.pretty_url:
            ctx.log.info(f"[delay_harness] {self.delay}s <- {flow.request.pretty_url}")
            time.sleep(self.delay)


addons = [DelayAddon()]   # 缺这一行 addon 不会被加载
