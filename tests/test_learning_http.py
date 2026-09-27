import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from urllib.request import urlopen

from simulator import server


@unittest.skipUnless(hasattr(server, 'RUNTIME'), 'legacy server.Controller (RUNTIME, box, robot, analyst) is gone in the new architecture; the adaptation stack is not wired into SimulatorSession yet')
class LearningHTTPTests(unittest.TestCase):
    def test_learning_route_returns_json_through_real_http_handler(self):
        controller = MagicMock()
        expected = dict(episodes=[dict(incident_id='past', mean_regret=12)],
                        lessons=[], experience=None)
        controller.learning.return_value = expected
        ready = threading.Event()
        servers = []

        def server_factory(address, handler):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            servers.append(server)
            ready.set()
            return server

        with patch('simulator.server.Controller', return_value=controller), \
             patch('simulator.server.ThreadingHTTPServer', side_effect=server_factory), \
             patch('simulator.server.atexit.register'):
            worker = threading.Thread(target=server.serve, kwargs=dict(port=0), daemon=True)
            worker.start()
            self.assertTrue(ready.wait(5))
            server = servers[0]
            try:
                with urlopen(f'http://127.0.0.1:{server.server_port}/api/learning', timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers['Content-Type'], 'application/json')
                    self.assertEqual(json.load(response), expected)
                controller.learning.assert_called_once_with()
            finally:
                server.shutdown()
                worker.join(5)
            self.assertFalse(worker.is_alive())
