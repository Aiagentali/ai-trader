"""Token budget controller — hard daily cap enforced programmatically."""
from datetime import datetime, timezone


class BudgetExhausted(Exception):
    pass


class TokenBudgetController:
    def __init__(self, repo, daily_limit: int = 200_000):
        self.repo = repo
        self.daily_limit = daily_limit
        # Starting allocation shares (dynamic, not fixed):
        self.allocations = {
            "hourly_analysis": 0.55,
            "news_analysis": 0.12,
            "trade_decisions": 0.13,
            "position_monitoring": 0.10,
            "emergency_reserve": 0.10,
        }

    def usage(self) -> dict:
        u = self.repo.token_usage_today()
        u["limit"] = self.daily_limit
        u["remaining"] = max(0, self.daily_limit - u["total_tokens"])
        u["pct"] = round(100 * u["total_tokens"] / self.daily_limit, 1)
        return u

    def can_spend(self, task: str, estimated_tokens: int = 0) -> bool:
        u = self.usage()
        if u["total_tokens"] + estimated_tokens > self.daily_limit:
            return False
        return True

    def reserve_left(self) -> int:
        u = self.usage()
        reserve = int(self.daily_limit * self.allocations["emergency_reserve"])
        regular_cap = self.daily_limit - reserve
        # reserve usable only by critical tasks
        return max(0, regular_cap - u["total_tokens"])

    def check_and_allow(self, task: str, level: str = "NORMAL") -> bool:
        """Hard gate before any AI call. CRITICAL may use emergency reserve."""
        u = self.usage()
        if level == "CRITICAL":
            return u["total_tokens"] <= self.daily_limit  # CRITICAL allowed up to 100%
        if u["total_tokens"] >= self.daily_limit:
            return False
        if u["total_tokens"] >= self.daily_limit * 0.9:
            return False
        return True

    def record(self, task: str, input_tokens: int, output_tokens: int):
        self.repo.add_token_usage(input_tokens, output_tokens)
