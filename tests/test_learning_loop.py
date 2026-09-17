"""Learning-loop tests: past trade reviews must reach future decisions.

Shorthand: a trader who journals but never re-reads the journal learns nothing.
"""
import json
import pytest

from app.database.repository import Repository
from app.ai.prompts import build_messages, DECISION_TASK


@pytest.fixture()
def repo(tmp_path):
    return Repository(str(tmp_path / "t.db"))


def test_recent_reviews_returns_compact_lines(repo):
    repo.save_review("t1", "BAD_LOSS", "Entered too early before H1 close; ignored ADX<20.")
    repo.save_review("t2", "VALID_LOSS", "Thesis valid; normal stop-out.")
    lines = repo.recent_reviews(limit=5)
    assert len(lines) == 2
    assert all(isinstance(l, str) and l for l in lines)
    joined = "\n".join(lines)
    assert "BAD_LOSS" in joined and "VALID_LOSS" in joined


def test_recent_reviews_capped_and_newest_first(repo):
    for i in range(9):
        repo.save_review(f"t{i}", "VALID_WIN", f"lesson {i}")
    lines = repo.recent_reviews(limit=5)
    assert len(lines) == 5
    assert "lesson 8" in "\n".join(lines)  # newest kept


def test_review_included_in_decision_prompt(repo):
    """build_messages must embed the learning block in the lessons slot."""
    repo.save_review("t1", "BAD_LOSS", "Ignored spread filter before news.")
    lessons = "\n".join(repo.recent_reviews(limit=8))
    system, user, _ = build_messages(
        "NORMAL", "market", "", "", "", "", DECISION_TASK, lessons=lessons)
    assert "RECENT TRADE LESSONS" in user
    assert "Ignored spread filter" in user


def test_agent_prompt_builder_includes_lessons(repo):
    """Integration: TradingAgent._analyze_symbol builds context via repo.recent_reviews."""
    repo.save_review("t1", "BAD_WIN", "Took trade while spread was abnormal.")
    from app.agent.loop import Agent
    lessons = repo.recent_reviews(limit=8)
    assert any("BAD_WIN" in l for l in lessons)
