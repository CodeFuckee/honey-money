"""按域名分通道的动态限速器。

策略（AIMD + 熔断）：
- 提速：通道滑动窗口记满且成功率 >= boost 阈值 → qps + 1（加性增，封顶 max_qps）；
- 降速：窗口记满且成功率 < degrade 阈值 → qps 减半（乘性减，托底 min_qps）；
- 熔断：连续失败达到阈值（与窗口无关，即时触发）→ 通道冷却 cooldown 秒，
  期间 acquire 睡到冷却结束再放行（半开：放行后若继续失败会再次熔断）。

clock / sleeper 可注入，测试不产生真实等待。
"""

import asyncio
import random
import time
from collections import deque


class ChannelState:
    """单个域名通道的限速状态。"""

    def __init__(self, qps: float):
        self.qps = qps
        self.next_allow = 0.0          # monotonic 时钟：下一个允许请求的时刻
        self.circuit_until = 0.0       # 熔断截止时刻（0 = 未熔断）
        self.consecutive_failures = 0
        self.requests = 0              # 累计上报次数（统计用）
        self.success_window: deque[bool] = deque()

    def success_rate(self) -> float:
        if not self.success_window:
            return 1.0
        return sum(self.success_window) / len(self.success_window)


class RateLimiter:
    def __init__(self, *, initial_qps: float = 2.0, min_qps: float = 0.2,
                 max_qps: float = 20.0, window: int = 30,
                 boost_success_rate: float = 0.98, degrade_success_rate: float = 0.90,
                 circuit_consecutive_failures: int = 8, circuit_cooldown: float = 60.0,
                 jitter: float = 0.0, rng: random.Random | None = None,
                 clock=time.monotonic, sleeper=asyncio.sleep):
        """jitter: 间隔随机抖动比例（0.3 = ±30%），消除固定节奏的机器特征；
        rng 可注入固定种子便于测试。"""
        self.min_qps, self.max_qps = min_qps, max_qps
        self.window = window
        self.boost_success_rate = boost_success_rate
        self.degrade_success_rate = degrade_success_rate
        self.circuit_consecutive_failures = circuit_consecutive_failures
        self.circuit_cooldown = circuit_cooldown
        self._initial_qps = initial_qps
        self._jitter = jitter
        self._rng = rng or random.Random()
        self._clock = clock
        self._sleeper = sleeper
        self._channels: dict[str, ChannelState] = {}

    @staticmethod
    def channel_key(url: str) -> str:
        """URL → 通道键：取 host 并去掉数字前缀子域。

        东财按负载均衡会轮换 1.push2his / 91.push2his 等子域，实为同一后端服务，
        归并成同一通道统一限速；不同域名（清单 push2 / K线 push2his）各自独立。
        """
        host = url.split("//", 1)[1].split("/", 1)[0]
        parts = host.split(".")
        if parts[0].isdigit():
            parts = parts[1:]
        return ".".join(parts)

    def channel(self, key: str) -> ChannelState:
        ch = self._channels.get(key)
        if ch is None:
            ch = self._channels[key] = ChannelState(self._initial_qps)
        return ch

    async def acquire(self, key_or_url: str) -> float:
        """取得发送许可，返回实际等待的秒数（含熔断冷却）。"""
        ch = self.channel(self.channel_key(key_or_url) if "://" in key_or_url else key_or_url)
        waited = 0.0
        now = self._clock()
        if ch.circuit_until > now:                     # 熔断冷却
            cooldown = ch.circuit_until - now
            await self._sleeper(cooldown)
            waited += cooldown
        now = self._clock()
        if ch.next_allow > now:                        # 令牌间隔
            gap = ch.next_allow - now
            await self._sleeper(gap)
            waited += gap
        interval = 1.0 / ch.qps
        if self._jitter:
            interval *= self._rng.uniform(1.0 - self._jitter, 1.0 + self._jitter)
        ch.next_allow = max(self._clock(), ch.next_allow) + interval
        return waited

    def report(self, key_or_url: str, ok: bool) -> None:
        """回报一次请求结果，驱动 AIMD 调速与熔断。"""
        ch = self.channel(self.channel_key(key_or_url) if "://" in key_or_url else key_or_url)
        ch.success_window.append(ok)
        if len(ch.success_window) > self.window:
            ch.success_window.popleft()
        ch.requests += 1

        if ok:
            ch.consecutive_failures = 0
        else:
            ch.consecutive_failures += 1
            if ch.consecutive_failures >= self.circuit_consecutive_failures:
                # 熔断：冷却后清空连续计数与统计窗口（半开重新采样）
                ch.circuit_until = self._clock() + self.circuit_cooldown
                ch.consecutive_failures = 0
                ch.success_window.clear()
                return

        # 窗口记满才评估成功率（升降速都在这里，与本次成败无关）
        if len(ch.success_window) != self.window:
            return
        rate = ch.success_rate()
        if rate >= self.boost_success_rate:
            ch.qps = min(self.max_qps, ch.qps + 1.0)
        elif rate < self.degrade_success_rate:
            ch.qps = max(self.min_qps, ch.qps / 2.0)
            # 已排期的放行时刻按新速率推迟（降速立即生效）
            ch.next_allow = max(ch.next_allow, self._clock() + 1.0 / ch.qps)

    def stats(self) -> dict[str, dict]:
        return {
            key: {
                "qps": round(ch.qps, 3),
                "success_rate": round(ch.success_rate(), 3),
                "requests": ch.requests,
                "circuit_until": ch.circuit_until,
            }
            for key, ch in self._channels.items()
        }
