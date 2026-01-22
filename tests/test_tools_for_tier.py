from app.agent_runtime_v1 import tools_for_tier


def test_heartmula_in_tier_one():
    t0 = tools_for_tier(0)
    t1 = tools_for_tier(1)
    assert "heartmula_generate" not in t0
    assert "heartmula_generate" in t1


def test_heartmula_in_tier_two():
    t2 = tools_for_tier(2)
    assert "heartmula_generate" in t2
