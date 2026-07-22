# Agentic SAST — Local Context Memory 专题

本目录是《Agentic_SAST架构图_v2评审修订版.html》（仓库根目录）中
**Local Context Memory（文档/配置）** 模块在 千万行级 C/C++ 基站代码 场景下的落地资料。

## 文档顺序

1. **`01_Local_Context_Memory_分析路径指南.md`**
   方法论：在你自己的生产仓上，按什么步骤、用什么命令链提取下面这些代码特征。
   含 11 节正文 + 3 个附录（含可直接复制的复现命令清单）。

2. **`02_Local_Context_Memory_样例文档.md`**
   参考样本：把第 1 份的 schema（三件套 + 1 附加）填上具体值的对照模板。
   schema 部分可整体复制到你的仓里，把里面的宏名 / 模块表 / 不变量替换为本仓真实值即可。

## 这两份文件的关系

- 第 1 份是**怎么生成**的指南，给负责落地的人。
- 第 2 份是**生成出来应该长什么样**的参照，给审阅与维护的人。

## 与上游架构图的对应

| 架构图节点 / 边 | 这里对应文档/小节 |
|---|---|
| Static Layer “Local Context Memory（文档/配置）” | 三件套 + sinks_whitelist = 该节点内容物 |
| Flow Layer Step 3a “挂载 Local Context Files” | 第 2 份 §A/§B/§C 描述注入动作 |
| v2 CHANGELOG 第 07 项 措辞修正（“显著降低” + CI 漂移校验） | 第 1 份 §8 + 第 2 份 §F |
| 风险点 R4（静默误杀真实漏洞） | 第 1 份 §7 “sinks_whitelist 为何必带” + 第 2 份 §D |
| Roadmap P0→P3 节奏 | 第 1 份 §10 |

## 验证仓

文中"证据样本"均取自：

- 5G 基站对照仓：`/home/atituiset/Projects/testbeds/srsRAN_Project/`（4684 文件，C++17，带 `compile_commands.json`）
- 嵌入式 C 对照仓：`/home/atituiset/Projects/testbeds/u-boot/`（14338 文件，Kbuild）

附录 A 给出了二者在本套方法下复用 80%、差异 20% 的对比表。
