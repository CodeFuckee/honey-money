"""限速器测试：域名通道归并、滑动窗口成功率 AIMD、熔断冷却。

全部离线：clock/sleeper 注入假实现，不产生真实等待。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 加入导入路径

from app.crawler.rate import RateLimiter  # noqa: E402


def make_limiter(**kw):
    """注入假时钟（手动推进）与假 sleeper（记录请求的等待秒数，立即返回）。"""
    time_box = {"now": 100.0}
    waits: list[float] = []

    async def fake_sleep(sec: float) -> None:
        waits.append(sec)

    limiter = RateLimiter(clock=lambda: time_box["now"], sleeper=fake_sleep, **kw)
    return limiter, time_box, waits


def run(coro):
    return asyncio.run(coro)


class TestChannelKey:
    def test_numbered_subdomain_merged(self):
        """91.push2his 与 push2his 是同一后端服务，归并为同一通道。"""
        a = RateLimiter.channel_key("https://91.push2his.eastmoney.com/api/qt/stock/kline/get")
        b = RateLimiter.channel_key("https://push2his.eastmoney.com/api/qt/stock/kline/get")
        assert a == b == "push2his.eastmoney.com"

    def test_different_hosts_separate(self):
        """清单接口与 K 线接口不同域名 → 不同通道（各自限速互不干扰）。"""
        assert (RateLimiter.channel_key("https://push2.eastmoney.com/api/qt/clist/get")
                != RateLimiter.channel_key("https://push2his.eastmoney.com/api/qt/stock/kline/get"))

    def test_plain_host_without_prefix(self):
        assert RateLimiter.channel_key("http://example.com/x") == "example.com"


class TestAcquire:
    def test_first_acquire_no_wait(self):
        limiter, _, waits = make_limiter(initial_qps=2.0)
        assert run(limiter.acquire("push2his.eastmoney.com")) == 0.0
        assert waits == []

    def test_second_acquire_waits_interval(self):
        """时钟不走时，第二次 acquire 必须等到 1/qps。"""
        limiter, _, waits = make_limiter(initial_qps=2.0)
        run(limiter.acquire("push2his.eastmoney.com"))
        waited = run(limiter.acquire("push2his.eastmoney.com"))
        assert abs(waited - 0.5) < 1e-9
        assert abs(waits[-1] - 0.5) < 1e-9

    def test_acquire_respects_dynamic_qps(self):
        """qps 被调整后，等待间隔随之变化（降速后间隔变长）。"""
        limiter, _, waits = make_limiter(initial_qps=4.0, window=1)
        run(limiter.acquire("push2his.eastmoney.com"))
        limiter.report("push2his.eastmoney.com", ok=False)  # 窗口满且成功率 0 → 4 → 2
        waited = run(limiter.acquire("push2his.eastmoney.com"))
        assert abs(waited - 0.5) < 1e-9  # 1/2 qps


class TestAimd:
    def test_full_success_window_boosts_qps(self):
        """窗口满且成功率达标 → 提速 +1。"""
        limiter, _, _ = make_limiter(initial_qps=2.0, window=10)
        for _ in range(10):
            limiter.report("push2his.eastmoney.com", ok=True)
        assert limiter.channel("push2his.eastmoney.com").qps == 3.0

    def test_low_success_window_degrades_qps(self):
        """窗口满且成功率 < 阈值 → qps 减半（AIMD 乘性减）。"""
        limiter, _, _ = make_limiter(initial_qps=4.0, window=10)
        for i in range(10):
            limiter.report("push2his.eastmoney.com", ok=(i >= 2))  # 8/10=0.8 < 0.9
        assert limiter.channel("push2his.eastmoney.com").qps == 2.0

    def test_qps_floor(self):
        """降速不低于 min_qps。"""
        limiter, _, _ = make_limiter(initial_qps=0.4, min_qps=0.2, window=4)
        for _ in range(4):   # 一轮满窗全失败：0.4 → 0.2
            limiter.report("push2his.eastmoney.com", ok=False)
        for _ in range(4):   # 再一轮：已到底，保持 0.2
            limiter.report("push2his.eastmoney.com", ok=False)
        assert limiter.channel("push2his.eastmoney.com").qps == 0.2

    def test_qps_ceiling(self):
        """提速不超过 max_qps。"""
        limiter, _, _ = make_limiter(initial_qps=19.5, max_qps=20.0, window=5)
        for _ in range(5):
            limiter.report("push2his.eastmoney.com", ok=True)
        assert limiter.channel("push2his.eastmoney.com").qps == 20.0

    def test_mixed_window_below_boost_keeps_qps(self):
        """成功率未达提速阈值 → qps 不变（不升不降）。"""
        limiter, _, _ = make_limiter(initial_qps=2.0, window=10)
        for i in range(10):
            limiter.report("push2his.eastmoney.com", ok=(i % 20 != 0))  # 9/10=0.9 < 0.98
        assert limiter.channel("push2his.eastmoney.com").qps == 2.0


class TestCircuitBreaker:
    def test_consecutive_failures_open_circuit(self):
        """连续失败达到阈值 → 通道熔断，acquire 需等冷却结束。"""
        limiter, t, waits = make_limiter(circuit_consecutive_failures=3, circuit_cooldown=60.0)
        for _ in range(3):
            limiter.report("push2his.eastmoney.com", ok=False)
        assert limiter.channel("push2his.eastmoney.com").circuit_until > t["now"]
        waited = run(limiter.acquire("push2his.eastmoney.com"))
        assert abs(waited - 60.0) < 1e-9

    def test_circuit_recovers_after_cooldown(self):
        """时钟推进超过冷却时间后，acquire 不再等待冷却。"""
        limiter, t, _ = make_limiter(circuit_consecutive_failures=2, circuit_cooldown=60.0)
        limiter.report("push2his.eastmoney.com", ok=False)
        limiter.report("push2his.eastmoney.com", ok=False)
        t["now"] += 61.0
        assert run(limiter.acquire("push2his.eastmoney.com")) == 0.0

    def test_success_resets_consecutive_count(self):
        """成功打断连续失败计数，不误熔断。"""
        limiter, _, _ = make_limiter(circuit_consecutive_failures=3)
        limiter.report("push2his.eastmoney.com", ok=False)
        limiter.report("push2his.eastmoney.com", ok=False)
        limiter.report("push2his.eastmoney.com", ok=True)
        limiter.report("push2his.eastmoney.com", ok=False)
        assert limiter.channel("push2his.eastmoney.com").circuit_until == 0.0

    def test_channels_independent(self):
        """A 通道熔断不影响 B 通道。"""
        limiter, t, _ = make_limiter(circuit_consecutive_failures=1, circuit_cooldown=60.0)
        limiter.report("push2.eastmoney.com", ok=False)
        assert t["now"] < limiter.channel("push2.eastmoney.com").circuit_until
        assert run(limiter.acquire("push2his.eastmoney.com")) == 0.0


class TestStats:
    def test_stats_shape(self):
        limiter, _, _ = make_limiter(window=5)
        for i in range(5):
            limiter.report("push2his.eastmoney.com", ok=(i < 4))
        stats = limiter.stats()
        entry = stats["push2his.eastmoney.com"]
        assert set(entry) >= {"qps", "success_rate", "requests", "circuit_until"}
        assert entry["requests"] == 5
        assert abs(entry["success_rate"] - 0.8) < 1e-9
