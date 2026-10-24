import importlib
import sys
import types
import unittest
from unittest.mock import patch

from test_core import USAGE

chat = importlib.import_module("jq_test_plugin.chat_billing")


def usage(hit=10, miss=20, output=30):
    return types.SimpleNamespace(input_cached=hit, input_other=miss, output=output)


class UsageTests(unittest.TestCase):
    def test_normalized_usage_and_raw_deepseek_agree(self):
        normalized = chat.response_usage(types.SimpleNamespace(usage=usage()))
        raw = chat.response_usage(
            types.SimpleNamespace(raw_completion={"usage": USAGE})
        )
        self.assertEqual(normalized, raw)

    def test_openai_cache_usage(self):
        result = chat.response_usage(
            types.SimpleNamespace(
                raw_completion={
                    "usage": {
                        "prompt_tokens": 30,
                        "completion_tokens": 30,
                        "total_tokens": 60,
                        "prompt_tokens_details": {"cached_tokens": 10},
                    }
                }
            )
        )
        self.assertEqual((result.hit, result.miss, result.output), (10, 20, 30))

    def test_missing_zero_negative_boolean_usage_is_not_free(self):
        for value in (None, usage(0, 0, 0), usage(True), usage(-1)):
            with self.assertRaises(ValueError):
                chat.response_usage(types.SimpleNamespace(usage=value))

    def test_tool_loop_uses_total_not_last_response(self):
        event = types.SimpleNamespace(unified_msg_origin="group")
        response = types.SimpleNamespace(usage=usage(), role="assistant")
        runner = types.SimpleNamespace(
            provider=types.SimpleNamespace(get_model=lambda: "model"),
            stats=types.SimpleNamespace(token_usage=usage(100, 200, 300)),
        )
        with patch.object(chat, "active_runner", return_value=runner):
            result = chat.final_usage(event, response, True, "model")
        self.assertEqual((result.hit, result.miss, result.output), (100, 200, 300))

    def test_runner_identity_cannot_cross_users(self):
        event = types.SimpleNamespace(unified_msg_origin="group")
        module = types.ModuleType("astrbot.core.pipeline.process_stage.follow_up")
        runner = types.SimpleNamespace(
            run_context=types.SimpleNamespace(
                context=types.SimpleNamespace(event=object())
            )
        )
        module._ACTIVE_AGENT_RUNNERS = {"group": runner}
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assertIsNone(chat.active_runner(event))
            runner.run_context.context.event = event
            self.assertIs(chat.active_runner(event), runner)

    def test_response_model_mismatch_requires_review(self):
        response = types.SimpleNamespace(
            usage=usage(), raw_completion={"model": "other"}
        )
        with self.assertRaises(ValueError):
            chat.final_usage(object(), response, False, "model")
