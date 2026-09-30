# 织梦 · Dreamweaver

在 QQ 上和 AI 一起走进故事。选择行动，让剧情随你的决定展开。

支持五种题材、自定义剧本、独立进度、暂停续玩和存档，回复使用合并聊天记录。

## 安装与配置

需要 **AstrBot ≥ 4.9.2**，通过 **OneBot v11 / NapCat** 接入 QQ。

1. 在 AstrBot 插件管理中，通过仓库链接安装：
   `https://github.com/Shxiao101/astrbot_plugin_dreamweaver`
2. 打开「织梦 → 配置」，选择模型，按需调整提示词、题材、恐怖度、轮数和字数。
3. 保存后，在 QQ 发送 `/story 开始`。默认为奇幻、无恐怖，12–16轮结束。

模型和主持设置在新开局、重玩时生效，进行中的故事保留原设置。

## 怎么玩

```text
/story 开始 推理悬疑 低
/story 1
/story 存档
```

```text
/story 剧本 科幻 无 你是空间站里最后一名维修员。
角色：失忆的导航AI、迟到三十年的货船船长。
目标：查出空间站为什么每天重复同一天。
```

题材：奇幻、神秘学、推理悬疑、恋爱、科幻。
恐怖度：无、低、中、高。

| 指令 | 用途 |
|---|---|
| `/story` | 查看帮助 |
| `/story 开始 [题材] [恐怖度]` | 开始故事 |
| `/story 剧本 <题材> <恐怖度> <正文>` | 自定义剧本，支持多行，最多3000字 |
| `/story 1`、`/story 2`、`/story 3` | 选择行动 |
| `/story 状态`、`/story 重试` | 查看当前剧情、重试失败回合 |
| `/story 退出`、`/story 继续` | 暂停、恢复 |
| `/story 存档`、`/story 记录 [页码]` | 保存、查看最近存档 |
| `/story 重玩` | 保留故事设定，用当前配置重新开始 |

每人在不同群和私聊中拥有独立进度，群内故事对群成员可见。AI 即兴生成剧情，重玩可能得到不同故事；通关设定中未经历的分支会标注为 AI 补全。

## 来源与许可

改编自 [InternalBeyond](https://github.com/Sui-IB/InternalBeyond) 的 Story 玩法和提示词，为非官方作品。
代码采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。

Required Notice: Copyright © 2025–2026 Sui. Internal Beyond (https://github.com/Sui-IB/InternalBeyond)
