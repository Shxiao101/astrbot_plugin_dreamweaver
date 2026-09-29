"""Long-lived AstrBot boundary contracts, owned by this plugin.

Fakes follow v4.9.2 public signatures; they do not replace real QQ smoke testing.
"""

import importlib
import json
import logging
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
PACKAGE = Path(__file__).resolve().parents[1].name
DEFAULTS = {
    key: item["default"]
    for key, item in json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    ).items()
}


class StarStub:
    """Expose actual v4.9.2 KV method signatures, including required default."""

    def __init__(self, context):
        self.context = context
        self.data = {}

    async def get_kv_data(self, key, default):
        return deepcopy(self.data.get(key, default))

    async def put_kv_data(self, key, value):
        self.data[key] = deepcopy(value)


class EventStub:
    """Represent already-normalized messages delivered by AstrBot's wake stage."""

    def __init__(self, text, group="100"):
        self.text = text
        self.group = group
        self.unified_msg_origin = "qq:group:100" if group else "qq:private:1"
        self.call_llm = True
        self.stopped = False

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return "1"

    def get_self_id(self):
        return "123456"

    def get_group_id(self):
        return self.group

    def should_call_llm(self, value):
        self.call_llm = value

    def chain_result(self, chain):
        return chain

    def stop_event(self):
        self.stopped = True


class ContextStub:
    """Keep exact model keyword arguments observable at the plugin boundary."""

    async def get_current_chat_provider_id(self, umo):
        return "qq-model"

    async def llm_generate(self, *, chat_provider_id, system_prompt, prompt, contexts):
        self.request = (chat_provider_id, system_prompt, prompt, contexts)
        return SimpleNamespace(
            completion_text=json.dumps(
                {
                    "story": "你来到一扇门前。",
                    "choices": ["开门", "观察", "离开"],
                    "isEnding": False,
                    "endingType": None,
                    "mood": "calm",
                },
                ensure_ascii=False,
            )
        )


class AdapterContracts(unittest.IsolatedAsyncioTestCase):
    """Protect registration, KV API use, response routing and send-failure recovery."""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        modules = {
            name: ModuleType(name)
            for name in [
                "astrbot",
                "astrbot.api",
                "astrbot.api.event",
                "astrbot.api.star",
                "astrbot.api.message_components",
                "astrbot.core",
                "astrbot.core.utils",
                "astrbot.core.utils.astrbot_path",
            ]
        }
        modules["astrbot.api"].AstrBotConfig = dict
        modules["astrbot.api"].logger = logging.getLogger("test.adapter")
        modules["astrbot.api.event"].AstrMessageEvent = EventStub
        components = modules["astrbot.api.message_components"]
        components.Plain = lambda text: SimpleNamespace(text=text)
        components.Node = lambda **kwargs: SimpleNamespace(**kwargs)
        components.Nodes = lambda nodes: SimpleNamespace(nodes=nodes)

        def command(name):
            self.command_name = name
            return lambda fn: fn

        modules["astrbot.api.event"].filter = SimpleNamespace(command=command)
        modules["astrbot.api.star"].Context = ContextStub
        modules["astrbot.api.star"].Star = StarStub
        modules["astrbot.api.star"].register = lambda *args: lambda cls: cls
        modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: (
            self.directory.name
        )
        with patch.dict(sys.modules, modules):
            sys.modules.pop(f"{PACKAGE}.main", None)
            main = importlib.import_module(f"{PACKAGE}.main")
        self.addCleanup(sys.modules.pop, f"{PACKAGE}.main", None)
        self.context = ContextStub()
        self.plugin = main.InteractiveStoryPlugin(self.context, deepcopy(DEFAULTS))
        self.addAsyncCleanup(self.plugin.terminate)

    async def test_normalized_multiline_command_calls_correct_model_and_kv(self):
        event = EventStub("story 剧本 科幻 无 第一行\n第二行")
        results = [r async for r in self.plugin.story(event)]
        self.assertEqual(self.command_name, "story")
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]), 1)
        node = results[0][0].nodes[0]
        self.assertEqual(node.uin, "123456")
        self.assertIn("玩家 1", node.content[0].text)
        self.assertIn("第 1 轮", node.content[0].text)
        self.assertIn("第一行\n第二行", self.context.request[1])
        self.assertEqual(self.context.request[0], "qq-model")
        self.assertEqual(self.context.request[3], [])
        self.assertFalse(event.call_llm)
        self.assertTrue(event.stopped)
        self.assertEqual(len(self.plugin.data), 1)

    async def test_ordinary_chat_is_untouched_and_private_reply_has_no_group_label(
        self,
    ):
        for text in ["1", "聊天", "/other", "storyboard"]:
            event = EventStub(text)
            self.assertEqual([r async for r in self.plugin.story(event)], [])
            self.assertTrue(event.call_llm)
            self.assertFalse(event.stopped)
        event = EventStub("story 开始", group="")
        results = [r async for r in self.plugin.story(event)]
        self.assertNotIn("玩家 1", results[0][0].nodes[0].content[0].text)

    async def test_long_reply_is_one_forward_card_with_multiple_nodes(self):
        async def reply(*args):
            return "故事" * 2000

        self.plugin.engine.handle = reply
        for group in ["100", ""]:
            results = [
                r async for r in self.plugin.story(EventStub("story 状态", group=group))
            ]
            self.assertEqual(len(results), 1)
            self.assertEqual(len(results[0]), 1)
            nodes = results[0][0].nodes
            self.assertEqual(len(nodes), 3)
            content = "".join(n.content[0].text.removeprefix("玩家 1\n") for n in nodes)
            self.assertEqual(content, "故事" * 2000)

    async def test_send_failure_does_not_rollback_committed_turn(self):
        event = EventStub("story 开始")
        replies = self.plugin.story(event)
        first = await anext(replies)
        # The transport fails after the handler yields; the engine is already committed.
        await replies.aclose()
        recovered = [r async for r in self.plugin.story(EventStub("story 状态"))]
        self.assertEqual(first, recovered[0])

    async def test_config_validation_and_explicit_provider(self):
        cls = type(self.plugin)
        for config in [
            {"provider_id": 1, "request_timeout": 120},
            {"provider_id": "", "request_timeout": -1},
            {"min_rounds": 20, "max_rounds": 10},
            {"story_prompt": ""},
            {"story_length": 2001},
        ]:
            with self.assertRaises((TypeError, ValueError)):
                cls(self.context, DEFAULTS | config)
        plugin = cls(self.context, DEFAULTS | {"provider_id": "fixed-model"})
        self.assertEqual(await plugin.provider("qq"), "fixed-model")

    async def test_panel_config_applies_to_generation_and_numeric_selection(self):
        cls = type(self.plugin)
        plugin = cls(
            self.context,
            DEFAULTS
            | {
                "provider_id": "fixed-model",
                "story_prompt": "以侦探第一人称主持故事。",
                "story_length": 300,
                "default_genre": "推理悬疑",
            },
        )
        [r async for r in plugin.story(EventStub("story 开始"))]
        results = [r async for r in plugin.story(EventStub("story 2"))]
        self.assertIn("第 2 轮", results[0][0].nodes[0].content[0].text)
        self.assertEqual(self.context.request[0], "fixed-model")
        self.assertIn("侦探第一人称", self.context.request[1])
        self.assertIn("300字", self.context.request[1])
        self.assertIn("观察", self.context.request[2])


if __name__ == "__main__":
    unittest.main()
