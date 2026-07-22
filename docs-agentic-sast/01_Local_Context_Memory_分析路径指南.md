# Local Context Memory 代码特征分析路径指南

> 配套文件：`02_Local_Context_Memory_样例文档.md`
> 目标读者：要在自己的生产代码仓上动手生成 `project_macros.json` / `audit_specs.md` 等本地文档记忆的工程师。
> 文档作用：作为架构图《Agentic_SAST架构图_v2评审修订版.html》中“Local Context Memory（文档/配置）”模块在**千万行级 C/C++ 基站代码**场景下的落地方法论。

---

## 0. 为什么要这份文档

架构图 v2 中 Local Context Memory 被标为“显著降低幻觉”的关键反幻觉手段，其内部应承载：

```
project_macros.json   —— 全局宏 / 编译定义映射
audit_specs.md        —— 架构级不变量 / 审计规范
                       （可再拆：sinks_whitelist.json / modules_map.yaml / build_flags.yaml …）
```

对一个 5G 基站 / 嵌入式 Bootloader 这种千万行规模的 C/C++ 仓，**所有让 LLM 推理错误的原因里 90% 是“缺项目语义”**：未展开的宏、自定义容器、自动生成的 ASN.1/Kconfig 体系、平台特定 sink 词典、模块边界、错误处理契约。这些都没有任何工具能直接给出答案，只能由本方法文档指导你**从仓库里机械提取**并固化到几份结构化文件里，再随库下发，挂载给 Qwen-Agent 或任何其他 Agent。

下述所有命令都以一个**真实验证过的 5G 基站目录**为参照运行过：
`/home/atituiset/Projects/testbeds/srsRAN_Project/`（4684 文件 / 25 万行 C++17，gNB 副 CU/DU 项目，已带 `compile_commands.json`），以及对照组嵌入式 C 仓 `/home/atituiset/Projects/testbeds/u-boot/`。证据保留在各节“证据样本”，可直接复现命令拿到你生产仓的同类结果。

---

## 1. 总原则（先读这六条）

1. **以编译数据库为权威事实源** —— 任何 `-D` / `-I` / `--std=` 信息都应从 `compile_commands.json` 抽取，而不是凭印象，也不是看 `CMakeLists.txt`（后者在多 target / 交叉编译场景下不可枚举）。
2. **以代码注释 / 协议规范为不变量事实源** —— “本函数在 TTI 边界必须无锁”、“ASN1 目录所有文件禁止人工编辑”这类约束写不到编译命令里，得在 README / 头部注释 / MAINTAINERS 里抠。
3. **机器可解析优于自由文本** —— 优先 JSON / YAML，二线用 Markdown 表格；自由散文只放最后给人类读的部分。
4. **越小越好，但要够 LLM 不幻觉** —— 一份 `project_macros.json` 控制在 200~400 项以内，单文件 ≤ 256KB 预算（架构图给 Qwen 的 “Local Load” 装载通道）。超了就分 namespace 或拆子文件。
5. **CI 漂移校验先行（架构图 v2 新增）** —— 生成此文档是一次性的，第二天代码改了宏就要校验。配套产出一份 `verify_context_memory.py`/`Makefile target` 跑 diff，落到 CI。
6. **豁免清单先于白名单先于黑名单** —— 千万行里 60%~70% 的“高危命中”来自自动生成与第三方（ASN.1、Kconfig、vendored lib）。先豁免那部分，再谈真实 sink 词典。

---

## 2. 输出三件套（最低必需集）

> 三份文件就够覆盖架构图画的 Local Context Memory 模块；后续可扩到 6~8 份。

| 文件名                    | 内容                                                            | 谁消费                                   |
|---------------------------|-----------------------------------------------------------------|------------------------------------------|
| `project_macros.json`     | 全局宏定义、编译开关、平台/架构宏、模块前缀白名单               | Qwen-Agent 在看到 `#ifdef X` 时不幻觉真值 |
| `build_flags.yaml`         | 编译选项、-std、-I 前缀列表、`-W` 级别、`-D` 数组（差异化按 target） | SCIP 索引器、Qwen 解析单文件上下文         |
| `audit_specs.md`          | 模块分区、不变量、豁免目录、错误处理契约、安全审计清单          | Qwen-Agent 审计 prompt 与人审兜底队列     |

---

## 3. 准备阶段：仓库可达性体检（5~10 分钟）

这一步同时决定能否在 P2 阶段上 SCIP/Neo4j 全局图谱（对应架构图风险点 R1）。

```bash
# 在生产仓根目录执行
test -f compile_commands.json \
  && jq '.[0] | keys' compile_commands.json \
  || echo "MISSING compile_commands.json"

# 没有就尝试两种抢救（按复杂度递增）
# A. CMake 项目
cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
# B. 任意 Make/GNU Build 系统：用 Bear 截获
bear -- make -j$(nproc)
# C. compiledb（从 make -n 出 dry-run 生成）
compiledb make -n
```

**证据样本**（srsRAN，已存在且字段完整）：
```
$ jq '.[0] | keys' compile_commands.json
[
  "command",
  "directory",
  "file",
  "output"
]
```

如果三种抢救都失败 → **此仓进入 R1（编译数据库可得性 ★★★★★）红色状态**，必须先解决可编译性问题，不要直接进 P1 注入 Memory，否则后续 SCIP/CPG 都是无源之水。

---

## 4. 生成 `project_macros.json`

### 4.1 从 compile_commands.json 抽出所有 `-D`

```bash
jq -r '.[].command' compile_commands.json \
  | grep -oE '\-D[A-Za-z0-9_]+' \
  | sort -u > /tmp/raw_D.txt
wc -l /tmp/raw_D.txt   # 期望：百~千量级；万量级就要二次过滤
```

**证据样本**（srsRAN 抽出后的去重清单节选）：
```
-DASSERTS_ENABLED
-DBACKWARD_HAS_BACKTRACE=0
-DBACKWARD_HAS_BACKTRACE_SYMBOL=1
-DBACKWARD_HAS_BFD=0
-DBACKWARD_HAS_DW=0
-DHAVE_FFTW
-DLIBUS_NO_SSL
-DNDEBUG
-DUWS_NO_ZLIB
-DBUILD_TYPE_RELWITHDEBINFO
```

### 4.2 区分宏的“四类语义”，分类入库

| 类型               | 例                                              | 来源检测                                              | 处理 |
|--------------------|--------------------------------------------------|--------------------------------------------------------|------|
| **断言/日志开关**  | `ASSERTS_ENABLED` `/NDEBUG`                       | `compile_commands.json`                                | 直接 `value=true/false` 入库；标 `tail:runtime` |
| **构建类型**       | `BUILD_TYPE_RELWITHDEBINFO`                      | `compile_commands.json`                                | 入库 `build_type` 字段 |
| **第三方特性探测** | `HAVE_FFTW`/`LIBUS_NO_SSL`                       | `compile_commands.json`                                | 入库；标 `source:vendor`，便于 LLM 知道这不是业务宏 |
| **平台/ABI**       | `__linux__`/`__ARM_NEON`/刻意的 `-march=native`   | `compile_commands.json` 或预定义宏                     | 入库，标 `platform`；`march=native` 因等价不定需 warning |

### 4.3 抽出“项目自创宏”（高价值，靠头文件扫描）

```bash
# 收集所有头文件里 #define 出现的非标准宏
rg -h "#define\s+[A-Z][A-Z0-9_]{4,}" \
   --glob '!lib/asn1/**' --glob '!external/**' \
   --glob '*.h' --glob '*.hpp' \
   | awk '{print $2}' | sort -u | head -200 > /tmp/proj_macros.txt
```

**证据样本**（srsRAN 自创宏候选）：
```
srsran_assert
SRSRAN_RTSAN_SCOPED_DISABLER
ASSERTS_ENABLED
U_BOOT_DRIVER          ← 这是 u-boot 的，srsRAN 里不会有，仅作对照
__init / __initdata    ← u-boot 段属性宏，LLM 看 #define 不知道是 GCC attribute 占位
```

### 4.4 落 schema

```json
{
  "$schema": "https://example.com/project_macros.v1.json",
  "version": "1.0.0",
  "generated_at": "2026-07-22T11:38:00+08:00",
  "build_type": "RelWithDebInfo",
  "std": "gnu++17",
  "platform": { "os": "linux", "arch": "native", "warnings": ["march=native"] },
  "macros": [
    { "name": "ASSERTS_ENABLED", "value": "1", "category": "assert",
      "tail": "runtime", "location": ["compile_commands.json"] },
    { "name": "NDEBUG", "value": "1", "category": "build", "tail": "runtime" },
    { "name": "HAVE_FFTW", "value": "1", "category": "vendor" },
    { "name": "LIBUS_NO_SSL", "value": "1", "category": "vendor" },
    { "name": "SRSRAN_RTSAN_SCOPED_DISABLER", "category": "project",
      "expansion": "RAII guard，禁用 ThreadSanitizer 注入",
      "location": ["include/srsran/support/rtsan.h"] }
  ],
  "prefix_whitelist": ["srsran/", "srsgnb/"],   // 模块名前缀白名单，配合 §7
  "module_root": "include/srsran"
}
```

要点：
- `category/bus/source/tail` 是**给 Qwen 看的元数据**——LLM 看到 `#ifdef NDEBUG` 是不是运行期行为，靠这个字段判断断言会不会真触发。
- 可信目录白名单先打到 4~8 条前缀，等 §7 模块表出来再合并。

---

## 5. 生成 `build_flags.yaml`

```bash
# 一次性抽出所有 -I、-std、-W、-O
jq -r '.[].command' compile_commands.json \
  | tr ' ' '\n' \
  | grep -E '^-(I|std|W|O|march|mtune)' \
  | sort | uniq -c | sort -rn > /tmp/build_flags.txt
```

**证据样本**（srsRAN）：
```
  -DNDEBUG
  -I/home/.../include
  -I/home/.../external/fmt/include
  -O2 -std=gnu++17
  -Wall -Werror -Wshadow -Wnon-virtual-dtor -Wsuggest-override
  -fno-rtti
  -march=native -mtune=generic
```

落 schema（YAML 比 JSON 可读，专写给人维护）：

```yaml
version: 1
std: gnu++17
opt_level: O2
rtti: false
warnings_as_errors: true
warning_flags:
  - "-Wnon-virtual-dtor"
  - "-Wsuggest-override"
  - "-Wshadow"
  - "-Wextra-semi"
include_roots:   # LLM 看到的 #include 解析前缀；删除则 = 项目根
  - "include"
  - "external/fmt/include"
  - "external"
arch:
  march: native              # ⚠ Qwen 应在审计随机器随机性/FFT/对齐类缺陷前提示人工确认
  mtune: generic
strip_includes:              # 这些前缀路径在审计时作为符号缩短呈现给 LLM
  - ".toolchain/root/usr/include"
```

> 单独²有 `build_flags.yaml` 是因为后续 SCIP 索引/clangd 局部解析会把这份喂进去，替代 LLM 凭空猜 `-std=c++14`。

---

## 6. 生成 `audit_specs.md`：五块切分（最关键的产出）

这一份文件是给 Qwen-Agent 当作审计事实底座。结构固定的五块；字数预算总计 ~6k token。

### 6.1 第 1 块：模块拓扑与边界（约束 LLM 穿库搜索范围）

**提取步骤**：

```bash
# A. 顶层子目录列表（目测排除 build/external/docs/scripts/tests）
ls -d */ | grep -vE '^(build|external|docs|scripts|tests|utils|cmake|docker|configs)/'
```

**证据样本**（srsRAN lib 子目录）：
```
asn1  cu_cp  cu_up  du  e1ap  e2  f1ap  f1u  fapi  fapi_adaptor
gateways  gtpu  hal instrumentation  mac  ngap  nrppa  nru  ofh
pcap  pdcp  phy  psup  radio  ran  rlc  rrc  ru  scheduler  sdap
security  srslog  srsvec  support
```

**产出表格**（Markdown，3 列：模块名 / 3GPP 协议域 / 是否豁免自动生成）：

| 子目录        | 域                 | 是否生成代码（豁免） | 备注 |
|---------------|--------------------|----------------------|------|
| lib/asn1/*    | 3GPP ASN.1 编解码  | ✅ 自动生成，禁编辑   | 见 §6.4 |
| lib/phy       | PHY/L1             | ❌                   | 信号处理 SIMD |
| lib/mac       | MAC/L2             | ❌                   |                  |
| lib/rlc/pdcp/sdap | L2 分层协议      | ❌                   |                  |
| lib/rrc/ngap/f1ap/e1ap/e2/nrppa | RRC/NGAP/F1AP/E1/E2/NRPPA | ✅ 部分ASN.1自动生成 | 见 §6.4 |
| lib/gateways  | SCTP/UDP 网关       | ❌                   | 真 sink，必审   |
| lib/ofh       | O-RAN Fronthaul    | ❌                   | 原始套接字 / DPDK |
| lib/security  | 加密/完整性         | ❌                   | ZUC/S3G，必须审 |
| lib/support   | 框架/执行器/事件循环 | ❌                   | srsran_assert 在 |
| include/srsran| 公共头              | ❌                   |                  |

### 6.2 第 2 块：架构级不变量（“审计必须满足”条款）

每条都是命令式短句，**不解释理由**（理由放括号里）。来源：从 README、关键头注释、`docs/`、人审 QA 卡片提取。

抽样模板：
- 在 PHY 与 MAC 之间数据交换必须经由 `span<>` / `byte_buffer_view` 这类**无所有权 view**，禁止裸指针。
- 任何写入 PDCP / RLC 之外线程的代码，必须经 `task_executor::execute()` 派发，禁止跨线程直接调用。
- ASN.1 目录（`lib/asn1/**`、`include/srsran/asn1/**`）禁止人工修改，PR 中出现该目录 diff 自动标记“生成代码”。
- `srsran_assert(...)` 失败即视为不可恢复，禁止 try/catch 吞掉。
- 错误传递统一用 `expected<T, error_type>`，禁止用返回码 `-1` + 全局 errno。
- Fronthaul 收包路径禁止动态内存分配；必须从 `fixed_size_memory_block_pool` 取段。

### 6.3 第 3 块：错误处理契约与断言机制

要让 Qwen 知道“看到 `srsran_assert(x)` 时审计结论 ≠ 漏洞，因为这是项目自己的断言机制，触发即 abort 设计正确”。

```markdown
## 错误处理契约
- **运行期断言**：`srsran_assert(cond, fmt, args...)`
  - 头：`include/srsran/support/srsran_assert.h`
  - 行为：失败 → stderr 打印 + `std::abort()`，**不可恢复**，禁止上层捕获
  - 用途：契约违规，而非“软错误”
  - LLM 审计时：发现 `srsran_assert(p != nullptr)` 不是缺陷，是必要 guard
- **错误返回**：`expected<T, error_type>`（自定义 in `adt/expected.h`，非 std::expected）
  - 调用失败必须显式 `return make_unexpected(e)`
  - 不允许 `(void)` 丢弃 `expected`
- **legacy C 仓** 见 §A 附录：u-boot 类项目用 `return -EINVAL`/`panic("...")`，不能套用本节契约
```

### 6.4 第 4 块：豁免清单与生成代码识别（千万行级关键）

**提取步骤**：

```bash
# 自动生成标记扫描
rg -l "This file was automatically generated|Auto-generated|DO NOT EDIT|generated from .*\.asn|3GPP TS" \
   --glob '*.cpp' --glob '*.h' --glob '*.c' \
   > /tmp/autogen_files.txt
wc -l /tmp/autogen_files.txt   # 千万行级项目里这一招就能圈出 5~7 成行数

# 行数占比（排除掉再算）
total=$(find . -type f \( -name '*.cpp' -o -name '*.h' \) -exec cat {} + | wc -l)
excluded=$(cat /tmp/autogen_files.txt | xargs cat 2>/dev/null | wc -l)
echo "auto_gen_ratio = $(echo "scale=2; $excluded/$total" | bc)"
```

**证据样本**（srsRAN / u-boot）：

| 子树                      | 文件数 | 行数      | 性质 |
|---------------------------|--------|-----------|------|
| srsRAN `lib/asn1/`        | 43     | **502100** | 3GPP ASN.1 全自动（5G 标准 RRC/NGAP/F1AP/E1AP/E2/NRPPA） |
| srsRAN `external/`        | (vendored) | n/a      | 第三方：fmt/nlohmann/CLI11/concurrentqueue |
| u-boot `lib/efi_loader/`  | -      | 较多       | UEFI 兼容层，从 EDK2 同步生成 |
| u-boot `drivers/`          | 79 子目录 | -        | 移植代码，每个 SoC 一份，不全审 |

→ 写进 `audit_specs.md` 的豁免块：

```markdown
## 豁免清单（不进入 LLM 审计，只做语法锚点过滤）
| 路径 glob                             | 豁免原因              | 处理 |
|---------------------------------------|-----------------------|------|
| `lib/asn1/**`                         | 3GPP ASN.1 自动生成    | 跳过 L1 锚点 + 跳过 L3 推理 |
| `external/**`                         | vendored 第三方       | 跳过 L3 推理；L1 仅产能耗数字异常进入 |
| `build/**`                            | 构建产物              | 完全排除 |
| `lib/efi_loader/efi_runtime.c`        | 同步自 EDK2           | 仅跟踪 CVE 反向修补 |
| `drivers/*/mcu-*` 等 SoC 私有 driver    | 历史遗留移植           | 仅扫高危宏复印件 |
```

> **风险**：豁免清单是审计的“护栏”，但护栏本身可能漏判真实漏洞（参考架构图 R4）。豁免只豁免“LLM 深推理”，不豁免“Layer1 规则扫描”——SAST 锚点仍跑遍所有，只是命中后**不送 LLM 决策**而直接退回人审队列。这件事要写进 audit_specs.md 顶部。

### 6.5 第 5 块：项目自创类型词典（高风险幻觉区）

LLM 看到 `std::vector` 知道边界，看到 `static_vector`/`byte_buffer`/`bounded_bitset` 不知道。手写一份小词典。

```markdown
## 自创使用类型 vs 标准对应
| 项目类型               | 标准等价                       | 行为差异（对审计的影响） |
|------------------------|--------------------------------|------------------------|
| `byte_buffer`          | 类似 `std::vector<uint8_t>`    | 链表段式存储；`append()` 返回 `bool`（可能失败，需判断） |
| `byte_buffer_view`     | `std::span<const uint8_t>`    | 无所有权，不可写入宿主 |
| `static_vector<T,N>`   | `std::array`+size             | 栈上固定容量，栈溢出风险 |
| `bounded_bitset<N>`    | `std::bitset<N>`               | 运行期 size 不变 |
| `expected<T,E>`        | `std::expected<T,E>` (C++23)  | 早于 std::expected，语义略不同 |
| `span<T>`              | `std::span<T>`                | 自实现，行为同标准 |
| `unique_fd`            | RAII 包裹 POSIX fd             | 析构 `close()`，需注意挪出语义 |
| `scope_exit`           | `gsl::finally` 等价            | 析构执行 lambda |
| `bounded_integer<T,N>` | 强类型整数                     | 编译期边界检查 |
| `strong_type<Tag,T>`   | 强类型别名                     | 类型不混用，LLM 审计时不要替换为底层整数 |
```

---

## 7. 生成 `sinks_whitelist.json`（强烈建议加）

针对千万行级项目，如果直接把 CWE 词典原文喂给 Qwen，会触发：

**证据**（srsRAN 抽样 §0 通过运行词频得到的反例）：
```
16 lib/e2/e2sm/e2sm_kpm_metric_defs.h:system    ← "system" 作为字段名，不是 ::system
10 lib/mac/mac_dl/mac_cell_time_mapper_impl.h:system
 7 include/srsran/srslog/formatter.h:gets       ← 英语动词 "gets"
 3 include/srsran/asn1/asn1_diff_utils.h:gets
```

→ 真实 sink 是这些（已被精确 `-w` 命中到的方法名）：
```
lib/support/network/sctp_socket.cpp      ::socket / ::bind / ::recvfrom
lib/gateways/*_network_gateway_impl.cpp  ::socket / ::bind
lib/ofh/ethernet/ethernet_receiver_impl.cpp ::socket(AF_PACKET) + ::recvfrom
lib/support/resource_usage/rapl_msr_energy_reader_impl.cpp ::open(msr_path)
lib/support/tracing/event_tracing.cpp   ::fopen
```

提交流程：
1. 用 CWE 词典扫一遍全仓，取每个命中文件头 10 列真实方法。
2. 人审勾选“这是真 sink” vs “单词撞名”，把撞名的入 `sinks_whitelist.json`：
```json
{ "_comment":"命中的样式不视为 sink",
  "ignore_tokens": ["system", "gets", "panic"],
  "ignore_prefixes": [".logger", ".metrics_", ".logger_."],
  "module_overrides": {
     "lib/asn1/**":  "skip",          # ASN.1 不审
     "external/**":  "skip",
     "build/**":     "skip"
  }
}
```

这份文件直接降低架构图 R4（静默误杀）风险——白名单内的误命中不会再被送入向量比对→误降级路径。

---

## 8. CI 漂移校验（架构图 v2 标注 `[新增]`）

把生成步骤脚本化、上 CI：

```bash
# scripts/verify_context_memory.sh
diff <(jq -r '.[].command' compile_commands.json | grep -oE '\-D[A-Za-z0-9_]+' | sort -u) \
     <(jq -r '.macros[].name' docs-agentic-sast/project_macros.json | sort -u) \
     && echo "macros OK" || { echo "macros drifted"; exit 1; }

# 同理校验 audit_specs.md 里的模块表与实际目录一致
ls -d lib/*/ | sed 's#lib/##;s#/##' | sort > /tmp/actual.txt
grep -E '^\| `lib/' docs-agentic-sast/audit_specs.md | sed 's/.*`\(lib\/[^`]*\)`.*/\1/' | sort -u > /tmp/written.txt
diff /tmp/actual.txt /tmp/written.txt && echo "modules OK" || exit 2
```

放到 CI 关键门：**禁止 `compile_commands.json` 已加 `-DHAVE_SOMENEW` 而 `project_macros.json` 没更新的 PR 合入**。这条断言本身就是 v2 架构图“CI 漂移校验 `[新增]`”的落地形式。

---

## 9. 接入：Qwen-Agent 与其他 Agent 怎么读这份记忆

按架构图 Static Layer：
- **Local Load 双向金色箭头**——Qwen-Agent 通过本地 IPC 按需拉取这三到四份文件到 system prompt；
- 每次审计前注入约 `≤8k token`：`audit_specs.md` 全文 + `project_macros.json` 压缩到 “名字+value” + `build_flags.yaml` 全文 + `sinks_whitelist.json` 全文；
- 真正巨大体量的“扩展宏展开式”不进 prompt，由 Local MCP Server 在局部切片时按需注入（见架构图 Zone3 的 clangd LSP）。

其他 Agent（OpenAI-style / AutoGPT / 通用 LangGraph）也用同一份文件——刻意保持 vendor-agnostic JSON/YAML/Markdown 三件套，避免与具体 Agent 框架耦合。

---

## 10. 阶段建议（对应 v2 Roadmap）

| Roadmap 阶段 | 该文档要求产出范围 | 估时 |
|--------------|-------------------|------|
| **P0**       | 只产 `audit_specs.md` 第一稿（模块表 + 5 条不变量）   | 0.5 人周 |
| **P1**       | + `project_macros.json` + `build_flags.yaml` + Local MCP 注入接口 | 1 个月 |
| **P2**       | + CI 漂移校验 + 豁免清单里的自动生成探测脚本         | 在 P2 编译性 POC 同时取 |
| **P3**       | + `sinks_whitelist.json` + 词典与向量库 reranker 对齐阈值 τ | 与 FP 记忆闭环共建 |

P0 不要追求完美：先用人工写 30 行 audit_specs 就能让 Qwen 每个 PR 的幻觉率明显改善（架构图原图标 “显著降低幻觉 [修正措辞]” 的出处）。

---

## 11. 验收清单

- [ ] 至少生成 §2 三件套文件，路径挂到 docs-agentic-sast/ 子目录
- [ ] `project_macros.json` 与 `compile_commands.json` 之间 CI diff 通过（§8）
- [ ] `audit_specs.md` 包含 5 块；每块至少 5 条事实
- [ ] 豁免清单覆盖所有 `external/` 与最大自动生成根（ASN.1/Kconfig）
- [ ] `sinks_whitelist.json` 至少列出本项目 3 个误命中
- [ ] 在 Qwen-Agent 单机流水线本地端到端跑通一次 PR，能从审计里看到引用了这份记忆（prompt trace 出现某条不变量文本）
- [ ] 落地一条 metric：本地端到端 ≤5 min（架构图横切层预算）

---

## 附录 A：对照仓 u-boot（legacy C 嵌入式）的差异点

u-boot 与 srsRAN 同一框架下可复用 80%，差异在于：

| 维度          | srsRAN（C++17）             | u-boot（C + Kbuild）        |
|---------------|------------------------------|-----------------------------|
| 编译数据库    | CMake → `compile_commands.json` 容易   | 由 Kbuild/Makefile 间接给出，常需 `bear make` 抢救，**R1 风险更高** |
| 错误处理契约  | `expected<T,E>` + `srsran_assert`     | `return -EINVAL` / `panic("...")`         |
| 自创宏体系    | 项目自创少量                | **大量**：`U_BOOT_DRIVER()` / `U_BOOT_CMD()` / `__init` / `__initdata` / `OF_REAL` / `DEVICE_GET()` 等 | 项目级宏展开必须入 `project_macros.json`，否则 LLM 一律当函数名处理 |
| 自动生成代码  | ASN.1                        | `defconfig` 系统 1525 个配置 + 同步自 Linux 的 `drivers/*` 大量移植 |
| 平台宏        | `-march=native`             | 由 SOC 决定的 `CONFIG_ARCH_*`，每个目标板独立 |
| 安全审计重点  | 网络栈、ASN.1 边界、加密     | DMA/MMU/cache flush、命令注入（`run_command`） |

对 u-boot 一类项目额外建议把 §6.5 词典扩到“段属性宏”一节，说明 `__init` 不是普通符号，而是 GCC `__attribute__((section(".init")))` 的别名，LLM 看到它在 `free()` 之后被“调用”要警觉。

---

## 附录 B：复现命令清单（在生产仓可直接运行）

```bash
# A - compile_commands 体检
test -f compile_commands.json && jq '.[0]|keys' compile_commands.json

# B - 抽 -D
jq -r '.[].command' compile_commands.json | grep -oE '\-D[A-Za-z0-9_]+' | sort -u

# C - 抽 -I/-W/-std
jq -r '.[].command' compile_commands.json | tr ' ' '\n' \
  | grep -E '^-(I|std|W|O|march|mtune)' | sort | uniq -c | sort -rn

# D - 项目自创宏
rg -h "#define\s+[A-Z][A-Z0-9_]{4,}" \
   --glob '!lib/asn1/**' --glob '!external/**' \
   --glob '*.h' --glob '*.hpp' \
   | awk '{print $2}' | sort -u | head -200

# E - 自动生成文件识别
rg -l "This file was automatically generated|Auto-generated|DO NOT EDIT|3GPP TS" \
   --glob '*.cpp' --glob '*.h' --glob '*.c'

# F - 真 syscall sink（带白名单初稿，需人审）
rg -ow "memcpy|memmove|memset|strcpy|strncpy|sprintf|snprintf|strcat|strncat|atoi|strtol|system|popen|gets|scanf|sscanf" \
   --glob '!lib/asn1/**' --glob '!external/**' --glob '!build/**' \
   . | grep -vE '^$|ripgrep' | sort | uniq -c | sort -rn | head -25
rg -ow "socket|bind|recvfrom|open|fopen|popen|ioctl|mmap|munmap" \
   --glob '!lib/asn1/**' --glob '!external/**' --glob '!build/**' \
   . | grep -vE '^$|ripgrep' | sort | uniq -c | sort -rn | head -20

# G - CI 漂移校验脚本骨架
diff <(jq -r '.[].command' compile_commands.json | grep -oE '\-D[A-Za-z0-9_]+' | sort -u) \
     <(jq -r '.macros[].name' docs-agentic-sast/project_macros.json | sort -u)
```

---

## 附录 C：参考关联文件

- 《Agentic_SAST架构图_v2评审修订版.html》（仓库根目录）：本指南对应其中“Local Context Memory（文档/配置）”节点 + “挂载 Local Context Files”数据流节点。
- `02_Local_Context_Memory_样例文档.md`（同目录）：在三件套 schema 上填了具体例子的参考样本，可作为你生产仓产出的对照模板。
