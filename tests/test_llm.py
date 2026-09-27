"""Offline tests for simulator.llm: key resolution, OpenRouter defaults, generic overrides and None-on-failure."""
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator import llm


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def chat_reply(text):
    return FakeResponse(json.dumps(dict(choices=[dict(message=dict(content=text))])).encode())


class LLMConfigTests(unittest.TestCase):
    def test_defaults_are_openrouter_and_space_bunny(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(llm.base_url(), 'https://openrouter.ai/api/v1')
            self.assertEqual(llm.model(), 'stealth/space-bunny-alpha')
        with patch.dict('os.environ', {'LLM_BASE_URL': 'http://localhost:11434/v1/', 'LLM_MODEL': 'llama3'}, clear=True):
            self.assertEqual(llm.base_url(), 'http://localhost:11434/v1')
            self.assertEqual(llm.model(), 'llama3')

    def test_key_resolution_order_and_no_key_means_none(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(llm, 'ENV_FILE', Path('/nonexistent/.env')):
            self.assertFalse(llm.configured()); self.assertIsNone(llm.complete('s', 'u')); self.assertIsNone(llm.complete_json('s', 'u'))
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'or'}, clear=True):
            self.assertEqual(llm.api_key(), 'or')
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'or', 'LLM_API_KEY': 'generic'}, clear=True):
            self.assertEqual(llm.api_key(), 'generic')

    def test_complete_posts_bearer_to_chat_completions_and_never_leaks_the_key(self):
        seen = {}
        def fake_urlopen(req, timeout=None):
            seen.update(url=req.full_url, auth=req.get_header('Authorization'), body=json.loads(req.data.decode()), timeout=timeout)
            return chat_reply('  {"diagnosis": "ok"} trailing')
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'secret-or'}, clear=True), patch.object(llm.request, 'urlopen', fake_urlopen):
            self.assertEqual(llm.complete_json('sys', 'usr'), {'diagnosis': 'ok'})
        self.assertEqual(seen['url'], 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(seen['auth'], 'Bearer secret-or')
        self.assertEqual(seen['body']['model'], 'stealth/space-bunny-alpha')
        self.assertEqual([m['role'] for m in seen['body']['messages']], ['system', 'user'])
        self.assertEqual(seen['timeout'], llm.DEFAULT_TIMEOUT); self.assertLessEqual(llm.DEFAULT_TIMEOUT, 30)

    def test_failures_degrade_to_none(self):
        with patch.dict('os.environ', {'LLM_API_KEY': 'k'}, clear=True):
            with patch.object(llm.request, 'urlopen', side_effect=llm.error.URLError('down')):
                self.assertIsNone(llm.complete('s', 'u'))
            with patch.object(llm.request, 'urlopen', side_effect=TimeoutError()):
                self.assertIsNone(llm.complete('s', 'u'))
            with patch.object(llm.request, 'urlopen', return_value=FakeResponse(b'{"error": "no credits"}')):
                self.assertIsNone(llm.complete('s', 'u'))
            with patch.object(llm.request, 'urlopen', return_value=chat_reply('not json at all')):
                self.assertIsNone(llm.complete_json('s', 'u'))


if __name__ == '__main__':
    unittest.main()
