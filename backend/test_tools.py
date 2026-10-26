"""Tool registry snapshots stay stable while tools reload concurrently."""
import asyncio
import threading
import unittest

from backend.core.tools import Tool, ToolRegistry, with_risk_confirmation


async def _noop():
    return "ok"


class _BlockingTool(Tool):
    def __init__(self, started: threading.Event, release: threading.Event):
        super().__init__("first", "first", {"type": "object"}, _noop)
        self.started = started
        self.release = release

    def to_openai_schema(self):
        self.started.set()
        self.release.wait(2)
        return super().to_openai_schema()


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_enumeration_uses_snapshot_during_concurrent_register(self):
        registry = ToolRegistry()
        started = threading.Event()
        release = threading.Event()
        mutated = threading.Event()
        schemas = []
        errors = []
        registry.register(_BlockingTool(started, release))

        def enumerate_schemas():
            try:
                schemas.extend(registry.all_schemas())
            except Exception as exc:
                errors.append(exc)

        def register_second():
            registry.register(Tool("second", "second", {"type": "object"}, _noop))
            mutated.set()

        reader = threading.Thread(target=enumerate_schemas)
        writer = threading.Thread(target=register_second)
        reader.start()
        self.assertTrue(started.wait(1))
        writer.start()
        try:
            self.assertTrue(mutated.wait(1), "schema 生成期间不应长期持有 registry 锁")
        finally:
            release.set()
            reader.join(2)
            writer.join(2)

        self.assertEqual([], errors)
        self.assertEqual(["first"], [item["function"]["name"] for item in schemas])
        self.assertEqual(["first", "second"], registry.names())

    async def test_handler_runs_outside_registry_lock(self):
        registry = ToolRegistry()
        started = asyncio.Event()
        release = asyncio.Event()

        async def waiting_handler():
            started.set()
            await release.wait()
            return "done"

        registry.register(Tool("waiting", "waiting", {"type": "object"}, waiting_handler))
        task = asyncio.create_task(registry.call("waiting", {}))
        await started.wait()
        registry.unregister("waiting")
        self.assertIsNone(registry.get("waiting"))
        release.set()
        self.assertEqual("done", await task)

    def test_risk_confirmation_notice_is_normalized_without_duplicates(self):
        accurate = "执行演示操作（风险审批模式下执行前会请求确认。）"
        normalized = with_risk_confirmation(accurate)
        self.assertTrue(normalized.startswith(accurate))
        self.assertIn("同一对话最多请求一次危险操作授权", normalized)
        self.assertIn("拒绝或中止后不再重复弹窗", normalized)
        self.assertEqual(normalized, with_risk_confirmation(normalized))

        absolute = with_risk_confirmation("执行演示操作，执行前会请求确认。")
        self.assertIn("风险审批模式下执行前会请求确认", absolute)
        self.assertEqual(1, absolute.count("执行前会请求确认"))
        self.assertEqual(1, absolute.count("风险审批模式"))

        appended = with_risk_confirmation("执行演示操作")
        self.assertIn("风险审批模式下执行前会请求确认", appended)
        self.assertEqual(appended, with_risk_confirmation(appended))
        self.assertEqual(1, appended.count("同一对话最多请求一次危险操作授权"))


if __name__ == "__main__":
    unittest.main()
