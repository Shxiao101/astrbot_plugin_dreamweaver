"""Long-lived plugin contracts: gameplay, persistence and isolation.

Owned by this plugin's test suite; provider and KV callbacks are boundary fakes.
"""

import asyncio
import importlib
import json
import re
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
engine_module = importlib.import_module(
    f"{Path(__file__).resolve().parents[1].name}.engine"
)
StoryEngine = engine_module.StoryEngine
StoryError = engine_module.StoryError
parse_command = engine_module.parse_command
parse_turn = engine_module.parse_turn
player_key = engine_module.player_key
DEFAULTS = {
    key: item["default"]
    for key, item in json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    ).items()
    if key not in {"provider_id", "request_timeout"}
}


def turn_json(number=1):
    ending = number >= 12
    return json.dumps(
        {
            "story": f"第{number}段剧情",
            "choices": [] if ending else ["开门", "观察", "等待"],
            "isEnding": ending,
            "endingType": "普通结局：归途" if ending else None,
            "mood": "joy" if ending else "calm",
        },
        ensure_ascii=False,
    )


class MemoryStore:
    """Model the copy/commit boundary of durable plugin KV writes."""

    def __init__(self):
        self.data = {}
        self.fail = False
        self.fail_completed = False

    async def load(self, key):
        return deepcopy(self.data.get(key))

    async def save(self, key, value):
        if self.fail or (self.fail_completed and value["game"]["pending"] is None):
            raise OSError("simulated disk error")
        self.data[key] = deepcopy(value)


class Model:
    """Generate round-specific replies and allow deterministic boundary failures."""

    def __init__(self):
        self.calls = []
        self.responses = []
        self.entered = asyncio.Event()
        self.release = None

    async def __call__(self, provider, system, prompt, contexts):
        self.calls.append((provider, system, prompt, deepcopy(contexts)))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        match = re.search(r"当前第(\d+)轮", prompt)
        return turn_json(int(match[1])) if match else "游戏概要\n实际经历与AI补全分支。"


class StoryContracts(unittest.IsolatedAsyncioTestCase):
    """Keep player-visible state consistent through the complete game lifecycle."""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store, self.model = MemoryStore(), Model()
        self.provider_name = "original-model"
        self.settings = deepcopy(DEFAULTS)
        self.engine = self.new_engine()

    def new_engine(self):
        async def provider(origin):
            return self.provider_name

        return StoryEngine(
            self.store.load,
            self.store.save,
            self.model,
            provider,
            Path(self.directory.name),
            2,
            self.settings,
        )

    async def command(self, action, args="", origin="qq:group:100", user="1"):
        return await self.engine.handle(origin, user, action, args)

    def game(self, origin="qq:group:100", user="1"):
        return self.store.data[player_key(origin, user)]["game"]

    async def finish(self):
        await self.command("开始")
        for _ in range(11):
            await self.command("1")

    async def test_complete_game_archive_and_replay(self):
        await self.finish()
        self.assertEqual(self.game()["round"], 12)
        self.assertTrue(self.game()["turn"]["isEnding"])
        with self.assertRaises(StoryError):
            await self.command("1")
        result = await self.command("存档")
        self.assertIn("完整故事设定已存档", result)
        self.assertIn("AI补全", await self.command("记录"))
        self.assertGreaterEqual(len(list(Path(self.directory.name).glob("*.txt"))), 2)
        self.provider_name = "new-model"
        await self.command("重玩")
        self.assertEqual(self.game()["round"], 1)
        self.assertEqual(self.game()["provider"], "new-model")
        self.assertIn("第12轮", await self.command("记录"))

    async def test_multiline_custom_script_and_replay_settings(self):
        script = "维修员的冒险\n角色：失忆AI\n目标：逃出空间站"
        await self.command("剧本", "科幻 低 " + script)
        self.assertEqual(self.game()["script"], script)
        self.assertIn(script, self.model.calls[0][1])
        await self.command("重玩")
        self.assertEqual(
            (self.game()["genre"], self.game()["horror"], self.game()["script"]),
            ("科幻", "低", script),
        )

    async def test_isolation_across_users_groups_and_private_chat(self):
        identities = [
            ("qq:group:100", "1"),
            ("qq:group:100", "2"),
            ("qq:group:200", "1"),
            ("qq:private:1", "1"),
            ("other:group:100", "1"),
        ]
        for origin, user in identities:
            await self.command("开始", origin=origin, user=user)
        await self.command("2")
        self.assertEqual(
            [self.game(o, u)["round"] for o, u in identities], [2, 1, 1, 1, 1]
        )
        self.assertEqual(self.model.calls[-1][3][0]["content"], "开始游戏")

    async def test_pause_restart_status_resume_and_provider_pinned(self):
        await self.command("开始")
        await self.command("退出")
        calls = len(self.model.calls)
        self.engine = self.new_engine()
        self.assertIn("已暂停", await self.command("状态"))
        await self.command("继续")
        self.assertEqual(len(self.model.calls), calls)
        self.provider_name = "changed"
        await self.command("1")
        self.assertEqual(self.model.calls[-1][0], "original-model")
        self.assertEqual(self.game()["round"], 2)

    async def test_invalid_input_never_calls_model_or_changes_progress(self):
        for action, args in [
            ("开始", "错误 无"),
            ("开始", "奇幻 错误"),
            ("开始", "奇幻 无 多余"),
            ("剧本", "奇幻 无 " + "字" * 3001),
            ("剧本", "奇幻 无"),
            ("未知", ""),
        ]:
            with (
                self.subTest(action=action, args=args[:20]),
                self.assertRaises(StoryError),
            ):
                await self.command(action, args)
        self.assertEqual(len(self.model.calls), 0)
        await self.command("开始")
        before = deepcopy(self.game())
        for action, args in [
            ("0", ""),
            ("4", ""),
            ("1", "a"),
            ("开始", ""),
            ("状态", "extra"),
            ("重试", ""),
        ]:
            with self.subTest(action=action, args=args), self.assertRaises(StoryError):
                await self.command(action, args)
        self.assertEqual(self.game(), before)

    async def test_bad_json_repaired_once_then_retry_without_duplicate_history(self):
        self.model.responses = ["not json", "still not json"]
        self.assertIn("未推进", await self.command("开始"))
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(self.game()["round"], 0)
        self.engine = self.new_engine()
        await self.command("重试")
        self.assertEqual(self.game()["round"], 1)
        self.assertEqual(len(self.game()["history"]), 2)
        self.model.responses = [None, turn_json(2)]
        await self.command("2")
        self.assertEqual(self.game()["round"], 2)
        self.assertEqual(len(self.game()["history"]), 4)

    async def test_model_timeout_preserves_pending_action_for_retry(self):
        await self.command("开始")
        self.engine.timeout = 0.01
        self.model.release = asyncio.Event()
        self.assertIn("未推进", await self.command("3"))
        self.assertEqual(self.game()["round"], 1)
        self.assertEqual(self.game()["pending"], "等待")
        self.model.release = None
        await self.command("重试")
        self.assertEqual(self.game()["round"], 2)
        self.assertEqual(self.game()["history"][2]["content"], "等待")

    async def test_concurrent_selection_rejected_and_exit_invalidates_late_response(
        self,
    ):
        await self.command("开始")
        self.model.entered.clear()
        self.model.release = asyncio.Event()
        pending = asyncio.create_task(self.command("1"))
        await self.model.entered.wait()
        with self.assertRaises(StoryError):
            await self.command("2")
        await self.command("退出")
        self.model.release.set()
        self.assertIsNone(await pending)
        self.assertTrue(self.game()["paused"])
        self.assertEqual(self.game()["round"], 1)
        await self.command("继续")
        await self.command("重试")
        self.assertEqual(self.game()["round"], 2)

    async def test_exit_then_replay_old_request_cannot_overwrite_new_game(self):
        await self.command("开始")
        self.model.entered.clear()
        release = self.model.release = asyncio.Event()
        pending = asyncio.create_task(self.command("1"))
        await self.model.entered.wait()
        await self.command("退出")
        self.model.release = None
        await self.command("重玩")
        new_game = deepcopy(self.game())
        release.set()
        self.assertIsNone(await pending)
        self.assertEqual(self.game(), new_game)
        self.assertEqual(self.game()["round"], 1)

    async def test_storage_failure_before_and_after_generation(self):
        self.store.fail = True
        with self.assertRaises(OSError):
            await self.command("开始")
        self.assertEqual(self.model.calls, [])
        self.store.fail = False
        await self.command("开始")
        self.store.fail_completed = True
        self.assertIn("未推进", await self.command("1"))
        self.assertEqual(self.game()["round"], 1)
        self.store.fail_completed = False
        await self.command("重试")
        self.assertEqual(self.game()["round"], 2)
        self.assertEqual(len(self.game()["history"]), 4)

    async def test_midgame_archive_has_only_actual_history_and_paginates(self):
        await self.command("剧本", "奇幻 无 " + "设定" * 1499)
        count = len(self.model.calls)
        await self.command("存档")
        self.assertEqual(len(self.model.calls), count)
        self.assertIn("1/3", await self.command("记录"))
        self.assertIn("第1轮", await self.command("记录", "3"))
        for page in ["0", "-1", "4", "hello"]:
            with self.assertRaises(StoryError):
                await self.command("记录", page)

    async def test_document_failure_preserves_raw_archive(self):
        await self.finish()
        self.model.responses = [RuntimeError("provider error")]
        self.assertIn("实际游玩记录已存档", await self.command("存档"))
        self.assertIn("第12轮", await self.command("记录"))
        self.assertEqual(self.game()["round"], 12)

    async def test_round_sixteen_requires_ending_and_repair_uses_same_round(self):
        await self.command("开始")
        for number in range(2, 16):
            self.model.responses = [turn_json(1)]
            await self.command("1")
        self.model.responses = [turn_json(1), turn_json(16)]
        result = await self.command("2")
        self.assertIn("结局", result)
        self.assertEqual(self.game()["round"], 16)
        self.assertIn("当前第16轮", self.model.calls[-1][2])
        self.assertEqual(len(self.game()["history"]), 32)

    async def test_unload_cancels_generation_but_preserves_retry(self):
        self.model.release = asyncio.Event()
        pending = asyncio.create_task(self.command("开始"))
        await self.model.entered.wait()
        await self.engine.close()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.game()["round"], 0)
        self.engine = self.new_engine()
        self.model.release = None
        await self.command("重试")
        self.assertEqual(self.game()["round"], 1)

    async def test_configured_defaults_prompts_round_limits_and_replay(self):
        self.settings.update(
            {
                "default_genre": "科幻",
                "default_horror": "低",
                "story_prompt": "以航海日志的风格主持。",
                "document_prompt": "整理为人物传记。",
                "min_rounds": 2,
                "max_rounds": 3,
                "story_length": 400,
            }
        )
        self.engine = self.new_engine()
        self.assertIn("默认：科幻 低", await self.command("帮助"))
        await self.command("开始")
        self.assertEqual((self.game()["genre"], self.game()["horror"]), ("科幻", "低"))
        self.assertIn("以航海日志", self.model.calls[-1][1])
        self.assertIn("400字", self.model.calls[-1][1])
        self.assertIn("第2至3轮", self.model.calls[-1][1])
        self.model.responses = [turn_json(12)]
        await self.command("1")
        await self.command("存档")
        self.assertEqual(self.model.calls[-1][1], "整理为人物传记。")
        self.settings["story_prompt"] = "新提示词"
        self.provider_name = "new-model"
        self.engine = self.new_engine()
        await self.command("重玩")
        self.assertEqual(self.game()["provider"], "new-model")
        self.assertIn("新提示词", self.model.calls[-1][1])

    async def test_running_game_preserves_settings_and_legacy_games_resume(self):
        await self.command("开始")
        self.settings.update(
            {"story_prompt": "变更文风", "min_rounds": 1, "max_rounds": 2}
        )
        self.engine = self.new_engine()
        await self.command("1")
        self.assertIn("第12至16轮", self.model.calls[-1][1])
        self.assertNotIn("变更文风", self.model.calls[-1][1])
        # v1.0.1 KV records lack settings. Preserve their original gameplay defaults.
        del self.game()["settings"]
        self.engine = self.new_engine()
        await self.command("2")
        self.assertEqual(self.game()["round"], 3)
        self.assertIn("第12至16轮", self.model.calls[-1][1])
        self.assertNotIn("变更文风", self.model.calls[-1][1])

    async def test_numeric_commands_select_only_current_player_game(self):
        await self.command("开始")
        for option in ["1", "2", "3"]:
            action, args = parse_command("/story " + option)
            await self.command(action, args)
        self.assertEqual(self.game()["round"], 4)
        self.assertIn("/story 1", await self.command("状态"))


class InputContracts(unittest.TestCase):
    """External command and JSON boundaries reject ambiguous game state."""

    def test_only_explicit_commands_and_multiline_body(self):
        for text in [
            "1",
            "选 1",
            "普通聊天",
            "/storyboard",
            "hello /story",
            "/story选 1",
        ]:
            self.assertIsNone(parse_command(text))
        self.assertEqual(parse_command("/story"), ("帮助", ""))
        self.assertEqual(parse_command("/story 1"), ("1", ""))
        self.assertEqual(
            parse_command("story 剧本 科幻 无 第一行\n第二行"),
            ("剧本", "科幻 无 第一行\n第二行"),
        )

    def test_json_contract_and_code_fences(self):
        self.assertEqual(
            parse_turn("```json\n" + turn_json() + "\n```", 1, 12, 16)["mood"], "calm"
        )
        for change in [
            {"choices": ["a", "b"]},
            {"choices": ["a", "a", "a"]},
            {"isEnding": "false"},
            {"mood": "unknown"},
            {"mood": []},
            {"story": ""},
        ]:
            data = json.loads(turn_json())
            data.update(change)
            with self.subTest(change=change), self.assertRaises(StoryError):
                parse_turn(json.dumps(data), 1, 12, 16)
        with self.assertRaises(StoryError):
            parse_turn(turn_json(12), 3, 12, 16)
        with self.assertRaises(StoryError):
            parse_turn(turn_json(1), 16, 12, 16)


if __name__ == "__main__":
    unittest.main()
