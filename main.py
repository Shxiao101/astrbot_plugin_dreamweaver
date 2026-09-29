"""AstrBot command and model adapters for the interactive story engine."""

import math
from functools import partial
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .engine import StoryEngine, StoryError, parse_command
from .prompts import GENRES, HORRORS


@register("astrbot_plugin_interactive_story", "SHX", "QQ 独立进度互动故事", "1.0.2")
class InteractiveStoryPlugin(Star):
    """Route explicit story commands without sharing normal conversation history."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        provider_id = config["provider_id"]
        timeout = config["request_timeout"]
        if not isinstance(provider_id, str):
            raise TypeError("provider_id 必须为字符串，留空表示使用当前会话模型")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 10 <= timeout <= 600
        ):
            raise ValueError("request_timeout 必须为10至600秒")
        self.provider_id = provider_id.strip()
        settings = {
            key: config[key]
            for key in (
                "story_prompt",
                "document_prompt",
                "default_genre",
                "default_horror",
                "min_rounds",
                "max_rounds",
                "story_length",
                "script_length",
            )
        }
        for key in ("story_prompt", "document_prompt"):
            if not isinstance(settings[key], str) or not settings[key].strip():
                raise ValueError(f"{key} 必须为非空文本")
        if (
            settings["default_genre"] not in GENRES
            or settings["default_horror"] not in HORRORS
        ):
            raise ValueError("默认题材或恐怖度不合法")
        for key, low, high in (
            ("min_rounds", 1, 64),
            ("max_rounds", 1, 64),
            ("story_length", 50, 1500),
            ("script_length", 50, 1500),
        ):
            if type(settings[key]) is not int or not low <= settings[key] <= high:
                raise ValueError(f"{key} 必须为 {low}–{high} 的整数")
        if settings["min_rounds"] > settings["max_rounds"]:
            raise ValueError("最早结局轮次不能大于最晚结局轮次")
        self.engine = StoryEngine(
            partial(self.get_kv_data, default=None),
            self.put_kv_data,
            self.generate,
            self.provider,
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_interactive_story"
            / "archives",
            timeout,
            settings,
        )

    async def provider(self, origin):
        if self.provider_id:
            return self.provider_id
        try:
            return await self.context.get_current_chat_provider_id(umo=origin)
        except Exception as exc:
            logger.warning(f"Story provider resolution failed: {type(exc).__name__}")
            raise StoryError(
                "未找到当前会话的聊天模型，请在 AstrBot 配置模型后再开局。"
            ) from exc

    async def generate(self, provider, system, prompt, contexts):
        response = await self.context.llm_generate(
            chat_provider_id=provider,
            system_prompt=system,
            prompt=prompt,
            contexts=contexts,
        )
        return response.completion_text

    @filter.command("story")
    async def story(self, event: AstrMessageEvent):
        """开始互动故事，使用 /story 1、2、3 选择，或查看状态、存档。"""
        command = parse_command(event.get_message_str())
        if command is None:
            return
        event.should_call_llm(False)
        try:
            result = await self.engine.handle(
                event.unified_msg_origin,
                str(event.get_sender_id()),
                *command,
            )
        except StoryError as exc:
            result = str(exc)
        except Exception as exc:  # noqa: BLE001 - contain third-party provider/storage errors
            # Error text may contain provider URLs or credentials; never echo it to QQ.
            logger.error(f"Story command failed: {type(exc).__name__}")
            result = "操作失败，未确认保存成功。请检查 AstrBot 模型配置或数据目录权限，再用 /story 状态 检查进度。"
        if result is not None:
            label = f"玩家 {event.get_sender_id()}\n" if event.get_group_id() else ""
            # One Nodes component produces one forward card, even for long replies.
            nodes = [
                Node(
                    uin=str(event.get_self_id()),
                    name="互动故事",
                    content=[Plain(label + result[offset : offset + 1500])],
                )
                for offset in range(0, len(result), 1500)
            ]
            yield event.chain_result([Nodes(nodes)])
        event.stop_event()

    async def terminate(self):
        await self.engine.close()
