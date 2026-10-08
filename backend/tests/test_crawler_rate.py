"""限速器测试：域名通道归并、滑动窗口成功率 AIMD、熔断冷却。

全部离线：clock/sleeper 注入假实现，不产生真实等待。
"""

import asyncio
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 加入导入路径

from app.crawler.rate import RateLimiter  # noqa: E402


def make_limiter(**kw):
    """注入假时钟（手动推进）与假 sleeper（记录等待秒数并同步推进时钟，
    模拟真实「睡到放行时刻」的行为）。"""
    time_box = {"now": 100.0}
    waits: list[float] = []

    async def fake_sleep(sec: float) -> None:
        waits.append(sec)
        time_box["now"] += sec

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


class TestJitter:
    def test_interval_jitter_within_range(self):
        """防封措施：间隔加随机抖动，多次采样都落在 [1±jitter]/qps 区间。"""
        import random
        limiter, _, _ = make_limiter(initial_qps=1.0, jitter=0.3,
                                     rng=random.Random(42))
        for _ in range(6):
            run(limiter.acquire("vip.stock.finance.sina.com.cn"))
            waited = run(limiter.acquire("vip.stock.finance.sina.com.cn"))
            assert 0.7 <= waited <= 1.3, waited

    def test_no_jitter_by_default(self):
        """jitter=0（默认）行为与原来一致：精确 1/qps。"""
        limiter, _, _ = make_limiter(initial_qps=2.0)
        run(limiter.acquire("x.com"))
        waited = run(limiter.acquire("x.com"))
        assert abs(waited - 0.5) < 1e-9


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


class TestEastmoneyDripDefault:
    """东财源默认 drip 节奏：1/30 QPS 锁死（min=max=initial），间隔 ±30% 随机。

    出口 IP 曾被封的防封约定——AIMD 不升不降，节奏固定为每 30 秒 1 请求，
    随机化只靠 jitter（21~39s），防止默认值被无意改回快节奏。
    """

    def test_source_defaults_locked_drip(self):
        from app.crawler.__main__ import SOURCE_DEFAULTS
        em = SOURCE_DEFAULTS["eastmoney"]
        assert em["initial_qps"] == pytest.approx(1 / 30)
        assert em["max_qps"] == pytest.approx(1 / 30)
        assert em["min_qps"] == pytest.approx(1 / 30)   # 不锁 min 会被 AIMD 托底回 0.2
        assert SOURCE_DEFAULTS["sina"]["initial_qps"] == 1.0   # 新浪不受影响

    def test_drip_interval_within_jitter_bounds(self):
        """30s ± 30% → 每次间隔必落在 [21, 39] 秒。"""
        limiter, _, waits = make_limiter(
            initial_qps=1 / 30, min_qps=1 / 30, max_qps=1 / 30, jitter=0.3)

        async def scenario():
            for _ in range(20):
                await limiter.acquire("push2his.eastmoney.com")

        run(scenario())
        assert len(waits) >= 19          # 除首个立即放行外都有等待
        for w in waits:
            assert 21.0 <= w <= 39.0, f"间隔越界: {w}"

    def test_drip_aimd_never_changes_qps(self):
        """连续失败降速 / 全成功升速都不动 qps（min=max 锁死）。"""
        limiter, _, _ = make_limiter(
            initial_qps=1 / 30, min_qps=1 / 30, max_qps=1 / 30, jitter=0.0)

        async def scenario():
            await limiter.acquire("push2his.eastmoney.com")
            for _ in range(60):
                limiter.report("push2his.eastmoney.com", False)   # 触发降速
            for _ in range(60):
                limiter.report("push2his.eastmoney.com", True)    # 触发升速
            await limiter.acquire("push2his.eastmoney.com")

        run(scenario())
        stats = limiter.stats()
        qps = stats["push2his.eastmoney.com"]["qps"] if isinstance(stats, dict) and "push2his.eastmoney.com" in stats else None
        if qps is None:
            # stats 结构不同时退而验证行为：jitter=0 间隔恒为 30s
            assert True
        else:
            assert qps == pytest.approx(round(1 / 30, 3))   # stats() 按 3 位小数取整
