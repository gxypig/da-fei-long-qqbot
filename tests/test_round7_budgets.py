import asyncio,unittest
import test_maintenance
from data.plugins.astrbot_plugin_proactive_agent.stage_budgets import total_budget,separate_stage,remaining

class ToolBudgets(unittest.IsolatedAsyncioTestCase):
    async def test_nested_execution_cannot_reset_parent_deadline(self):
        with self.assertRaises(TimeoutError):
            async with total_budget(.06):
                await asyncio.sleep(.03)
                async with total_budget(1):await asyncio.sleep(.04)

    async def test_actual_receipt_wait_is_excluded_but_execution_still_expires(self):
        async with total_budget(.4):
            await asyncio.sleep(.01)
            async with separate_stage('receipt',1):await asyncio.sleep(.5)
            self.assertGreater(remaining(1),.2)
            await asyncio.sleep(.01)
        with self.assertRaises(TimeoutError):
            async with total_budget(.04):await asyncio.sleep(.07)

    async def test_nested_stages_count_elapsed_once(self):
        async with total_budget(.4):
            async with separate_stage('queue',.4):
                await asyncio.sleep(.02)
                async with separate_stage('review',.3):await asyncio.sleep(.08)
            self.assertLess(remaining(1),.45)
            self.assertGreater(remaining(1),.2)

    async def test_stage_timeout_and_cancellation_restore_execution_context(self):
        async with total_budget(.2):
            with self.assertRaises(TimeoutError):
                async with separate_stage('receipt',.01):await asyncio.sleep(.03)
            self.assertGreater(remaining(1),.14)
        entered=asyncio.Event()
        async def run():
            async with total_budget(.1):
                async with separate_stage('review',10):entered.set();await asyncio.Future()
        t=asyncio.create_task(run());await entered.wait();t.cancel()
        result=await asyncio.gather(t,return_exceptions=True)
        self.assertIsInstance(result[0],asyncio.CancelledError)
        self.assertEqual(remaining(7),7)
