import unittest
import json
from unittest.mock import AsyncMock, patch

from backend.core.bridge import WorkerBridge, direct_websocket_options


class WebsocketProxyOptionsTests(unittest.TestCase):
    def test_modern_websockets_explicitly_bypasses_system_proxy(self):
        def connect(url, *, proxy=None, **kwargs):
            return None

        options = direct_websocket_options(connect, ping_interval=20)
        self.assertIsNone(options["proxy"])

    def test_older_websockets_without_proxy_keyword_stays_compatible(self):
        def connect(url, **kwargs):
            return None

        options = direct_websocket_options(connect, ping_interval=20)
        self.assertNotIn("proxy", options)


class WorkerModelOrderReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_welcome_restores_saved_order_before_serving_tasks(self):
        order = [{"id": "default", "models": ["second", "first"]}]
        received = []
        worker = WorkerBridge("wss://hub.invalid/worker", "private-token", "pc", AsyncMock(),
                              on_model_order=AsyncMock(side_effect=lambda value: received.append(value)))
        ws = AsyncMock()
        ws.recv.return_value = json.dumps({"type": "welcome", "model_order": order})

        async def serve(connection):
            self.assertIs(ws, connection)
            self.assertEqual([order], received)
            worker.stop()

        context = AsyncMock()
        context.__aenter__.return_value = ws
        with patch("websockets.connect", return_value=context), \
                patch.object(worker, "_serve", side_effect=serve):
            await worker.run_forever()
        hello = json.loads(ws.send.await_args.args[0])
        self.assertTrue(hello["model_order_sync"])
        worker._on_model_order.assert_awaited_once_with(order)


if __name__ == "__main__":
    unittest.main()
