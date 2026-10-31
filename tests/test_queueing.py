import asyncio

from app.queueing import JobGate


def test_bounded_capacity_and_per_user_exclusivity():
    async def scenario():
        gate = JobGate(max_pending=2, cooldown_seconds=0)
        assert (await gate.admit(1)).accepted
        assert not (await gate.admit(1)).accepted
        assert (await gate.admit(2)).accepted
        assert not (await gate.admit(3)).accepted
        await gate.release(1)
        assert (await gate.admit(3)).accepted
        async with gate.processing_slot():
            assert True
        await gate.release(2)
        await gate.release(3)
    asyncio.run(scenario())


def test_cooldown_is_applied_after_release():
    async def scenario():
        gate = JobGate(max_pending=2, cooldown_seconds=60)
        assert (await gate.admit(7)).accepted
        await gate.release(7)
        decision = await gate.admit(7)
        assert not decision.accepted
        assert "Подождите" in decision.message
    asyncio.run(scenario())
