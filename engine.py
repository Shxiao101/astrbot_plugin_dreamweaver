"""Independent story lifecycle, transactional progress and player isolation."""

import asyncio
import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .prompts import GENRES, HORRORS, LEGACY_SETTINGS, MOODS, system_prompt

logger = logging.getLogger("astrbot.interactive_story")

HELP = """织梦：每人独立进度，每轮选择1–3。
/story 开始 [题材] [恐怖度]（默认：{genre} {horror}）
/story 剧本 <题材> <恐怖度> <正文>（支持多行，最多3000字）
/story 1（或2、3）
/story 状态｜重试｜退出｜继续｜存档｜重玩
/story 记录 [页码]
题材：奇幻、神秘学、推理悬疑、恋爱、科幻
恐怖度：无、低、中、高
退出会保留进度；重玩会保留旧记录并重新生成故事。
裸数字不触发选择。群聊中的故事内容对群成员可见。"""


class StoryError(Exception):
    """An actionable game input or model-output error."""


def player_key(origin: str, sender: str) -> str:
    return "story:" + json.dumps(
        [origin, sender], ensure_ascii=False, separators=(",", ":")
    )


def parse_command(text: str):
    """Match only an explicit story command, preserving multiline script text."""
    match = re.fullmatch(r"/?story(?:\s+(.*))?", text.strip(), re.DOTALL)
    if not match:
        return None
    body = match[1]
    if not body:
        return "帮助", ""
    parts = body.split(maxsplit=1)
    return parts[0], parts[1] if len(parts) == 2 else ""


def parse_turn(raw: str, round_number: int, min_rounds: int, max_rounds: int) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise StoryError("模型回复为空")
    text = raw.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            text = match[1]
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise StoryError("回复不是有效JSON") from exc
    if not isinstance(data, dict):
        raise StoryError("回复必须为JSON对象")
    story = data.get("story")
    choices = data.get("choices")
    ending = data.get("isEnding")
    ending_type = data.get("endingType")
    mood = data.get("mood")
    if not isinstance(story, str) or not story.strip() or len(story) > 2000:
        raise StoryError("剧情为空或过长")
    if type(ending) is not bool or not isinstance(mood, str) or mood not in MOODS:
        raise StoryError("结局或情绪字段不合法")
    if not isinstance(choices, list):
        raise StoryError("选项必须是数组")
    if ending:
        if (
            choices
            or not isinstance(ending_type, str)
            or not ending_type.strip()
            or len(ending_type) > 100
        ):
            raise StoryError("结局需要名称和空选项数组")
        if round_number < min_rounds:
            raise StoryError(f"尚未到第{min_rounds}轮，请继续剧情")
    else:
        if round_number >= max_rounds:
            raise StoryError(f"第{max_rounds}轮必须收束为结局")
        if ending_type is not None or len(choices) != 3:
            raise StoryError("非结局需要三个选项，endingType必须为null")
        if any(
            not isinstance(c, str) or not c.strip() or len(c) > 200 for c in choices
        ):
            raise StoryError("选项为空或过长")
        if len({c.strip() for c in choices}) != 3:
            raise StoryError("三个选项不能重复")
    return {
        "story": story.strip(),
        "choices": [c.strip() for c in choices],
        "isEnding": ending,
        "endingType": ending_type,
        "mood": mood,
    }


def render(game: dict) -> str:
    prefix = f"第 {game['round']} 轮 · {game['genre']} · 恐怖度：{game['horror']}"
    if game["paused"]:
        prefix += " · 已暂停"
    turn = game["turn"]
    if turn is None:
        content = "开场尚未生成。"
    else:
        content = f"【{MOODS[turn['mood']]}】{turn['story']}"
        if turn["isEnding"]:
            content += f"\n\n结局：{turn['endingType']}\n可发送 /story 存档、/story 重玩 或 /story 退出。"
        else:
            content += "\n\n" + "\n".join(
                f"{i}. {choice}" for i, choice in enumerate(turn["choices"], 1)
            )
            content += "\n发送 /story 1、/story 2 或 /story 3。"
    if game["paused"]:
        content += "\n发送 /story 继续 恢复。"
    elif game["pending"] is not None:
        content += "\n本轮尚未提交；失败后发送 /story 重试。"
    return prefix + "\n\n" + content


def transcript(game: dict) -> str:
    lines = [f"织梦 · {game['genre']} · 恐怖度：{game['horror']}"]
    if game["script"]:
        lines += ["自定义剧本：", game["script"]]
    for i in range(0, len(game["history"]), 2):
        action = game["history"][i]["content"]
        turn = json.loads(game["history"][i + 1]["content"])
        lines += [f"\n第{i // 2 + 1}轮 · 玩家：{action}", turn["story"]]
        lines += [f"{n}. {c}" for n, c in enumerate(turn["choices"], 1)]
        if turn["isEnding"]:
            lines.append("结局：" + turn["endingType"])
    return "\n".join(lines)


@dataclass
class PlayerRuntime:
    """Serialize short storage operations while allowing exit during generation."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    token: object | None = None


class StoryEngine:
    """Own story state; injected callbacks connect persistence and LLM to AstrBot."""

    def __init__(
        self,
        load,
        save,
        generate,
        provider,
        archive_dir: Path,
        timeout: float,
        settings: dict,
    ):
        self.load = load
        self.save = save
        self.generate = generate
        self.provider = provider
        self.archive_dir = archive_dir
        self.timeout = timeout
        self.settings = deepcopy(settings)
        self.players: dict[str, PlayerRuntime] = {}
        self.tasks: set[asyncio.Task] = set()

    async def close(self):
        for runtime in self.players.values():
            runtime.token = None
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _call(self, game, system, prompt, contexts):
        task = asyncio.create_task(
            self.generate(game["provider"], system, prompt, contexts)
        )
        self.tasks.add(task)
        try:
            return await asyncio.wait_for(task, self.timeout)
        finally:
            self.tasks.discard(task)

    async def _archive(self, text):
        # UUID file names never contain user-controlled QQ IDs, titles or paths.
        path = self.archive_dir / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_") + uuid4().hex + ".txt"
        )

        def write():
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as output:
                output.write(text)

        await asyncio.to_thread(write)

    async def handle(self, origin, sender, action, args):
        if action == "帮助":
            return HELP.format(
                genre=self.settings["default_genre"],
                horror=self.settings["default_horror"],
            )
        allowed = {
            "开始",
            "剧本",
            "1",
            "2",
            "3",
            "状态",
            "重试",
            "退出",
            "继续",
            "存档",
            "记录",
            "重玩",
        }
        if action not in allowed:
            if action.isdigit():
                raise StoryError("请选择1、2或3，例如 /story 1。")
            raise StoryError("未知指令。发送 /story 查看帮助。")
        if (
            action in {"1", "2", "3", "状态", "重试", "退出", "继续", "存档", "重玩"}
            and args
        ):
            raise StoryError("此指令不接受额外参数。")
        key = player_key(origin, sender)
        runtime = self.players.setdefault(key, PlayerRuntime())
        async with runtime.lock:
            stored = await self.load(key)
            record = (
                deepcopy(stored)
                if stored is not None
                else {"game": None, "archive": None}
            )
            game = record["game"]
            if game is not None and "settings" not in game:
                game["settings"] = deepcopy(LEGACY_SETTINGS)
            if action == "记录":
                archive = record["archive"]
                if archive is None:
                    raise StoryError("还没有存档，请先发送 /story 存档。")
                if args and not re.fullmatch(r"[1-9][0-9]{0,5}", args):
                    raise StoryError("页码应为正整数。")
                page = int(args) if args else 1
                pages = [archive[i : i + 1200] for i in range(0, len(archive), 1200)]
                if page > len(pages):
                    raise StoryError(f"存档共{len(pages)}页。")
                return f"存档 {page}/{len(pages)} 页\n\n{pages[page - 1]}"
            if action == "状态":
                if game is None:
                    raise StoryError("没有故事，请发送 /story 开始。")
                return render(game)
            if action == "退出":
                if game is None:
                    raise StoryError("没有进行中的故事。")
                game["paused"] = True
                await self.save(key, record)
                runtime.token = (
                    None  # In-flight responses cannot commit after this point.
                )
                return "故事已暂停并保存。发送 /story 继续 恢复。"
            if runtime.token is not None:
                raise StoryError("正在生成或存档，请等待；可发送 /story 退出 暂停。")
            if action in {"开始", "剧本", "重玩"}:
                if (
                    action != "重玩"
                    and game is not None
                    and not (game["turn"] and game["turn"]["isEnding"])
                ):
                    raise StoryError(
                        "已有未结束故事。发送 /story 继续，或 /story 重玩 重新开始。"
                    )
                if action == "重玩":
                    if game is None:
                        raise StoryError("还没有可重玩的故事。")
                    genre, horror, script = (
                        game["genre"],
                        game["horror"],
                        game["script"],
                    )
                    old_text = transcript(game)
                    await self._archive(old_text)
                    record["archive"] = old_text
                else:
                    parts = args.split(maxsplit=2)
                    if action == "剧本":
                        if (
                            len(parts) != 3
                            or not parts[2].strip()
                            or len(parts[2]) > 3000
                        ):
                            raise StoryError(
                                "用法：/story 剧本 <题材> <恐怖度> <1–3000字正文>"
                            )
                        genre, horror, script = parts
                    else:
                        if len(parts) > 2:
                            raise StoryError("用法：/story 开始 [题材] [恐怖度]")
                        genre = parts[0] if parts else self.settings["default_genre"]
                        horror = (
                            parts[1]
                            if len(parts) == 2
                            else self.settings["default_horror"]
                        )
                        script = ""
                    if genre not in GENRES or horror not in HORRORS:
                        raise StoryError("题材或恐怖度不合法。发送 /story 查看可选值。")
                    if game is not None:
                        await self._archive(transcript(game))
                provider = await self.provider(origin)
                if not provider:
                    raise StoryError(
                        "未配置可用聊天模型，请在 AstrBot 插件配置面板选择模型后再开局。"
                    )
                game = {
                    "genre": genre,
                    "horror": horror,
                    "script": script,
                    "provider": provider,
                    "settings": {
                        key: value
                        for key, value in self.settings.items()
                        if key not in {"default_genre", "default_horror"}
                    },
                    "round": 0,
                    "history": [],
                    "turn": None,
                    "paused": False,
                    "pending": "开始游戏",
                }
                record["game"] = game
                await self.save(key, record)
            elif game is None:
                raise StoryError("没有故事，请发送 /story 开始。")
            elif action == "继续":
                game["paused"] = False
                await self.save(key, record)
                return render(game)
            elif action in {"1", "2", "3"}:
                if game["paused"]:
                    raise StoryError("故事已暂停，请先发送 /story 继续。")
                if game["pending"] is not None:
                    raise StoryError("有尚未完成的回合，请发送 /story 重试。")
                if game["turn"] is None or game["turn"]["isEnding"]:
                    raise StoryError("当前没有可选择的选项。")
                game["pending"] = game["turn"]["choices"][int(action) - 1]
                await self.save(key, record)
            elif action == "重试":
                if game["paused"]:
                    raise StoryError("请先发送 /story 继续。")
                if game["pending"] is None:
                    raise StoryError(
                        "没有失败或中断的回合。发送 /story 状态 查看当前剧情。"
                    )
            elif action == "存档":
                if game["round"] == 0:
                    raise StoryError("开场尚未生成，没有可保存的剧情。")
                # Raw history survives even if the subsequent document call fails.
                record["archive"] = transcript(game)
                await self._archive(record["archive"])
                await self.save(key, record)
                if not game["turn"]["isEnding"]:
                    return "当前剧情已存档，不含未经历分支。发送 /story 记录 查看。"
            token = object()
            runtime.token = token
        try:
            if action == "存档":
                text = await self._call(
                    game, game["settings"]["document_prompt"], transcript(game), []
                )
                if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                    raise StoryError("设定文档为空或过长")
                text = (
                    "完整故事设定\n未经历分支与隐藏机制属于AI补全，非实际游玩记录。\n\n"
                    + text.strip()
                )
                async with runtime.lock:
                    if runtime.token is not token:
                        return None
                    await self._archive(text)
                    record["archive"] = text
                    await self.save(key, record)
                return "完整故事设定已存档。发送 /story 记录 查看。"
            number = game["round"] + 1
            settings = game["settings"]
            prompt = f"当前第{number}轮（共{settings['min_rounds']}–{settings['max_rounds']}轮）。玩家行动：{game['pending']}"
            if number == settings["max_rounds"]:
                prompt += "\n本轮必须给出结局，禁止继续提供选项。"
            system = system_prompt(game)
            contexts = deepcopy(game["history"])
            raw = await self._call(game, system, prompt, contexts)
            try:
                turn = parse_turn(
                    raw, number, settings["min_rounds"], settings["max_rounds"]
                )
            except StoryError as exc:
                async with runtime.lock:
                    if runtime.token is not token:
                        return None
                previous = raw[:8000] if isinstance(raw, str) else "（空回复）"
                repair = f"{prompt}\n上次回复不合规范：{exc}。请重新生成本轮合法JSON，不推进轮次。\n上次回复：\n{previous}"
                raw = await self._call(game, system, repair, contexts)
                turn = parse_turn(
                    raw, number, settings["min_rounds"], settings["max_rounds"]
                )
            game["history"] += [
                {"role": "user", "content": game["pending"]},
                {"role": "assistant", "content": json.dumps(turn, ensure_ascii=False)},
            ]
            game["round"], game["turn"], game["pending"] = number, turn, None
            async with runtime.lock:
                if runtime.token is not token:
                    return None
                await self.save(key, record)
            return render(game)
        except Exception as exc:  # noqa: BLE001 - preserve progress across provider/storage failures
            logger.warning("Story operation %s failed: %s", action, type(exc).__name__)
            async with runtime.lock:
                if runtime.token is not token:
                    return None
            if action == "存档":
                return "完整设定生成或保存失败；实际游玩记录已存档。可再次 /story 存档，或 /story 记录 查看。"
            return (
                "本轮生成或保存失败，进度未推进。请检查模型或存储后发送 /story 重试。"
            )
        finally:
            async with runtime.lock:
                if runtime.token is token:
                    runtime.token = None
