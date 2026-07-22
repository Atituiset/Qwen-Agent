# Local Context Memory 样例文档（参考样本）

> 配套文件：`01_Local_Context_Memory_分析路径指南.md`
> 用途：把 §2 的“三件套”落到具体财务产物，作为你在生产仓产出对应文件时的对照模板。本文件内的宏值、模块名、断言签名等取自一个已验证过的 5G 基站代码仓（srsRAN），故可直接套到千万行级 gNB / O-RAN / 嵌入式 bootloader 项目上验证合理性。
> 体量目标：四份合并 ≤ 256 KB / 约 6~8 k token，由 Local MCP Server 在每次 PR 审计前注入 Qwen-Agent（或其他 Agent）system prompt。

本目录接下来按指南 §2 的三件套 + 1 件附加，给出可直接复制粘贴起步的 schema 与样本值：

- `project_macros.json` — 全局宏映射
- `build_flags.yaml` — 编译选项与索引前缀
- `audit_specs.md` — 架构级不变量与豁免（**最关键**）
- `sinks_whitelist.json` — 误命中白名单（附加，防 R4）

下面分别给出完整样本。你完全可以保留 schema、把所有具体值替换为你本仓的真实值。

---

## A. `project_macros.json`（参考样本）

```json
{
  "$schema": "https://example.com/project_macros.v1.json",
  "version": "1.0.0",
  "generated_at": "2026-07-22T11:38:00+08:00",
  "project": "srsRAN_Project",
  "language": "c++17",
  "build_type": "RelWithDebInfo",
  "platform": {
    "os": "linux",
    "arch": "native",
    "warnings": [
      "march=native 视为运行机特定，SIMD 优化审计需单独走"
    ]
  },
  "macros": [
    {
      "name": "ASSERTS_ENABLED",
      "value": "1",
      "category": "assert",
      "tail": "runtime",
      "source": "compile_commands.json",
      "comment": "构建开关；关闭时 srsran_assert 会被消除，审计前需确认其当前所在编译单元的命令行是否带此 -D"
    },
    {
      "name": "NDEBUG",
      "value": "1",
      "category": "build",
      "tail": "runtime",
      "source": "compile_commands.json"
    },
    {
      "name": "BUILD_TYPE_RELWITHDEBINFO",
      "value": "1",
      "category": "build"
    },
    {
      "name": "HAVE_FFTW",
      "value": "1",
      "category": "vendor",
      "comment": "vendored 库存在性检测，非业务宏"
    },
    {
      "name": "LIBUS_NO_SSL",
      "value": "1",
      "category": "vendor"
    },
    {
      "name": "UWS_NO_ZLIB",
      "value": "1",
      "category": "vendor"
    },
    {
      "name": "BACKWARD_HAS_BACKTRACE_SYMBOL",
      "value": "1",
      "category": "diagnostics"
    },
    {
      "name": "BACKWARD_HAS_BFD",
      "value": "0",
      "category": "diagnostics"
    },
    {
      "name": "SRSRAN_RTSAN_SCOPED_DISABLER",
      "category": "project",
      "expansion": "RAII guard；析构时恢复 ThreadSanitizer 注入，构造时禁用",
      "location": ["include/srsran/support/rtsan.h"],
      "comment": "审计断言输出路径时认为是合法的 TSan 临时禁用，不是误用"
    }
  ],
  "prefix_whitelist": [
    "srsran/",
    "srsgnb/"
  ],
  "module_root": "include/srsran",
  "autogen_globs": [
    "lib/asn1/**",
    "include/srsran/asn1/**"
  ],
  "external_globs": [
    "external/**"
  ]
}
```

字段语义（专为 LLM 消费设计）：

| 字段           | 用途 |
|----------------|------|
| `category`     | assert/build/vendor/diagnostics/project/platform/refactor 中之一，LLM 据此决定是否兜底套用 std::swap/标准库行为 |
| `tail`         | `runtime`/`compiletime` 两值；为 `runtime` 时 LLM 审计要知道该宏在不同 TU 可能不同 |
| `expansion`    | 复杂宏的“人话解释”，不要求可执行，但必须让 LLM 一眼明白语义 |
| `location`     | 头文件路径或 `compile_commands.json`，便于 trace 回真源 |
| `autogen_globs` | 内嵌到 audit_specs.md §豁免清单的快捷键，Qwen 可据此直接触发跳过逻辑 |

---

## B. `build_flags.yaml`（参考样本）

```yaml
version: 1
generated_at: 2026-07-22T11:38:00+08:00
project: srsRAN_Project

language_std: gnu++17
opt_level: O2
rtti: false
exceptions: false                 # -fno-exceptions 隐含项，若未体现在命令行也照实填
warnings_as_errors: true
warning_flags:
  - "-Wall"
  - "-Werror"
  - "-Wnon-virtual-dtor"
  - "-Wsuggest-override"
  - "-Wshadow"
  - "-Wextra-semi"
  - "-Wno-error=stringop-overflow"

include_roots:                    # LLM 看到的 #include 起源前缀
  - "include"
  - "external/fmt/include"
  - "external"
strip_includes:                    # 解析结果显露时省略的路径分段
  - ".toolchain/root/usr/include"

arch:
  march: native                    # ⚠ LLM 在审计与 SIMD / FFT / 对齐相关缺陷前应警告人工核对
  mtune: generic

compile_db:
  path: "compile_commands.json"
  present: true
  generated_by: "cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON"

notes:
  - "node size <= 256KB / 8k token 预算：本文件 + project_macros.json + audit_specs.md + sinks_whitelist.json 合并不超"
```

---

## C. `audit_specs.md`（参考样本，**最重要**）

下面把 §6 的 5 个块按 markdown 模板填实，可整体复制后再改值。设计时请保留每一节标题与"非解释"的命令式语气。

```markdown
# Audit Specifications — srsRAN_Project

> 本文档随库下发，由 Qwen-Agent / AgentSAST 在每次 PR 审计前注入 system prompt。
> 维护责任：架构师；CI 校验见 `scripts/verify_context_memory.sh`。
> 修改原则：事实，不解释；理由仅放括号中。

## 1. 模块拓扑与审计边界

| 子目录                        | 域                 | 生成代码 | 备注                  |
|-------------------------------|--------------------|----------|-----------------------|
| lib/asn1/**, include/srsran/asn1/** | 3GPP ASN.1 编解码 | ✅ 自动 | 禁止人工 diff，跳过 L3 推理 |
| lib/phy/**                    | PHY / L1           | ❌       | SIMD：AVX2/AVX512/NEON，需平台宏配 |
| lib/mac/**                    | MAC / L2           | ❌       |                       |
| lib/rlc/**, lib/pdcp/**, lib/sdap/** | L2 分层协议   | ❌       |                       |
| lib/rrc/**, lib/ngap/**, lib/f1ap/**, lib/e1ap/**, lib/e2/**, lib/nrppa/** | RRC/NGAP/F1AP/E1/E2/NRPPA | 部分ASN.1自动 | 业务暴露面在 `lib/<proto>_adaptor/` 一侧 |
| lib/gateways/**               | SCTP/UDP 网关      | ❌       | 真 sink，必审         |
| lib/ofh/**                    | O-RAN Fronthaul    | ❌       | 原始套接字 / DPDK     |
| lib/gtpu/**, lib/f1u/**, lib/nru/** | 用户面隧道      | ❌       | TEID 路由             |
| lib/security/**               | 加密 / 完整性       | ❌       | ZUC / S3G，必须审     |
| lib/cu_cp/**, lib/cu_up/**, lib/du/** | 协议栈控制/用户面 | ❌       |                       |
| lib/support/**                | 框架 / executor / 同步 | ❌     | srsran_assert 在此     |
| include/srsran/adt/**         | 自定义容器         | ❌       | 见 §5 词典             |
| external/**                   | vendored 第三方    | (vendored) | L3 不审，CVE 反向跟踪 |

## 2. 架构级不变量（审计必须满足；违反即上 R4 人工队列）

1. PHY 与 MAC 间任何数据交换必须使用 `span<>` / `byte_buffer_view` 这类无所有权 view，**禁止裸指针**。
2. 跨 PHY/MAC/RLC/PDCP 任意线程的访问必须经 `task_executor::execute()` 派发；`std::thread` 直接跨调是违规。
3. ASN.1 目录不允许人工 PR diff；CI 中 `lib/asn1/**` diff 应由生成器一次性提交。
4. `srsran_assert(...)` 失败判定 `std::abort()`，禁止 `try/catch` 吞掉（§3）。
5. 错误传递统一使用 `expected<T, error_type>`；`return -1` 或 errno 模式为违规。
6. Fronthaul 收包关键路径禁止动态内存分配；必须取自 `fixed_size_memory_block_pool`。
7. 加密上下文（`lib/security/**`）禁止把密钥 / IV 拷出栈缓冲区；`byte_buffer` 持有期 >= 解密完成。
8. SCTP/UDP socket fd 必须由 `unique_fd` RAII 持有，禁止裸 `int fd` 跨函数传递。
9. `bounded_integer` / `strong_type` 包装的数值不可隐式转回 `int` 后再参与计算（审计要追下游是否丢失强类型）。
10. PCAP 写入仅在 debug build（`BUILD_TYPE_DEBUG` / `BUILD_TYPE_RELWITHDEBINFO`）打开；Release 不应 bring PCAP 依赖。

## 3. 错误处理与断言契约

- **运行期断言** `srsran_assert(cond, fmt, args...)`
  - 头：`include/srsran/support/srsran_assert.h`
  - 行为：失败 → stderr 打印 + `std::abort()`（**不可恢复**），上层禁止 try/catch
  - LLM 审计时：发现 `srsran_assert(p != nullptr)` 不是缺陷，是必要 guard；**不要把这个判成“可绕过的 assert”**
  - 关闭信号：编译命令行未带 `-DASSERTS_ENABLED` ⇒ 宏体被消除
- **错误返回** `expected<T, error_type>`（自定义于 `include/srsran/adt/expected.h`，非 `std::expected`）
  - 任一 `expected` 返回值必须显式 check；丢弃 (`(void)`/未 check) 是违规
  - `make_unexpected(e)` 是唯一构造错误通道
- **legacy C 对照仓**：见附录 A.u-boot，使用 `return -EINVAL`/`panic("...")`，本节契约不适用

## 4. 豁免清单（不进入 LLM 推理；L1 规则扫描照常）

> 豁免 LLM 深推理 ≠ 豁免 SAST 锚点扫描：豁免区命中进人审队列，不进 GPU 比对→降级路径（防 R4 静默误杀）。

| 路径 glob                             | 豁免原因              | CI 处理 |
|---------------------------------------|-----------------------|---------|
| lib/asn1/**                           | 3GPP ASN.1 自动生成   | PR 中出现该 diff 自动 `needs-human-review` + 不送 L3 |
| include/srsran/asn1/**                | 同上                  | 同上    |
| external/**                           | vendored 第三方        | 仅 CVE 反向跟踪           |
| build/**                              | 构建产物              | 完全排除 |
| tools/**                              | 离线工具脚本          | 不审计（Python/lua 等）   |
| lib/efi_loader/efi_runtime.c          | （u-boot 类）同步自 EDK2 | 仅 PR 触发 CVE 关键词      |

## 5. 项目自创类型词典

| 项目类型             | 标准等价               | 审计敏感行为 |
|----------------------|------------------------|--------------|
| byte_buffer          | std::vector<uint8_t>  | 链表段式存储；`append(seq)` 可能失败，必须判断返回 bool |
| byte_buffer_view     | std::span<const uint8_t> | 无所有权，不可写回宿主 buffer |
| static_vector<T,N>   | std::array + size      | 栈上定容；溢出风险 |
| bounded_bitset<N>    | std::bitset<N>        | 运行期 size 不变 |
| expected<T,E>        | std::expected<T,E>    | 早于 std，**允许 implicit 构造 T/Elt**，区别于标准 |
| span<T>              | std::span<T>          | 自实现，行为同标准 |
| unique_fd           | RAII POSIX fd         | 析构 close，禁 `release()` 后再用 |
| scope_exit           | gsl::finally          | 析构执行 lambda；不可复制 |
| bounded_integer<T,N>| 强类型范围整数        | 编译期边界检查 |
| strong_type<Tag,T>   | 强类型别名            | 类型间不可隐式互转；审计时不要降级为底层 int |

---

（文档结束，保持简洁，事实优先于修辞。）
```

---

## D. `sinks_whitelist.json`（附加，建议必带）

针对每个千万行级 C/C++ 仓，这个词典至少应包含 3 条“撞名误命中”。

```json
{
  "$schema": "https://example.com/sinks_whitelist.v1.json",
  "version": "1.0.0",
  "project": "srsRAN_Project",
  "_comment": "词典级别：本文件只说明 L1 锚点扫描时的单词撞名豁免；真正 sink 判定仍走 L2/L3",
  "ignore_tokens": [
    "system",
    "gets",
    "panic"
  ],
  "_rationale": {
    "system": "项目内作为字段名频繁出现，如 lib/e2/e2sm/e2sm_kpm_metric_defs.h 里 metrics.system；非 ::system() 调用",
    "gets": "英语动词，出现在注释/字段名中；非 gets(3) 调用",
    "panic": "在 u-boot 类legacy 仓里是项目自定义宏，行为不同于 Go 的 panic"
  },
  "ignore_prefixes": [
    ".logger.system",
    ".metrics.system"
  ],
  "module_overrides": {
    "lib/asn1/**":  "skip_l3",
    "include/srsran/asn1/**": "skip_l3",
    "external/**":  "skip_l3",
    "build/**":     "skip_all"
  },
  "_evidence_2026_07": {
    "real_syscall_sinks": [
      "lib/support/network/sctp_socket.cpp ::socket/::bind/::recvfrom",
      "lib/gateways/udp_network_gateway_impl.cpp ::socket/::bind",
      "lib/gateways/sctp_network_gateway_impl.cpp ::socket",
      "lib/ofh/ethernet/ethernet_receiver_impl.cpp ::socket(AF_PACKET)+::recvfrom",
      "lib/support/resource_usage/rapl_msr_energy_reader_impl.cpp ::open",
      "lib/support/tracing/event_tracing.cpp ::fopen"
    ],
    "false_positives": [
      "lib/e2/e2sm/e2sm_kpm_metric_defs.h 16× system (字段名)",
      "include/srsran/srslog/formatter.h 7× gets (英语动词)"
    ]
  }
}
```

---

## E. 产物总览与体积预算

| 文件                    | 常见行数 | 目标字节 | Token 估算 |
|-------------------------|---------|----------|-----------|
| project_macros.json     | 30~80   | < 16 KB  | ~1.5 k     |
| build_flags.yaml        | 25~60   | < 4 KB   | ~0.6 k     |
| audit_specs.md          | 90~200  | < 40 KB  | ~4 k       |
| sinks_whitelist.json    | 30~80   | < 8 KB   | ~1.2 k     |
| **合计**                 | —       | **< 68 KB** | **≤ 7.3 k token** |

处在架构图标注的“Local Load”通道预算内；超预算即必须分 namespace（按 target 仓拆），不可单一文件膨胀。

---

## F. 落地体检要点（本仓自评分卡）

- [ ] 三（+一）份文件全部生成，路径挂在 `docs-agentic-sast/`
- [ ] CI 跑通 `verify_context_memory.sh`（见指南 §8）且 PR 中宏漂移会被阻塞
- [ ] audit_specs.md 含 5 块；每块 ≥ 5 条事实；附录 C 关键断言头位置可点击跳
- [ ] 豁免清单至少覆盖 `external/**` + 项目最大自动生成根（ASN.1 / Kconfig / EDK2）
- [ ] sinks_whitelist.json 至少 3 条本项目真实撞名实例（不可直接抄本样本的 token）
- [ ] Qwen-Agent / AgentSAST 单机流水线跑通一次 PR，能看到 prompt trace 里引用了某条不变量文本
- [ ] 收尾一条指标纳入监控看板：PR 端到端 ≤ 5 min（架构图横切层预算）

---

## G. 与架构图的引用关系

| 架构图节点 / 边                                                       | 本文档对应产出 |
|-----------------------------------------------------------------------|----------------|
| Static Layer 的 “Local Context Memory（文档/配置）” 节点                | 三件套 + sinks_whitelist 即该节点内容物 |
| Flow Layer Step 3a “挂载 Local Context Files”                          | C/B/A 三份的注入动作 |
| v2 CHANGELOG 第 07 项 “措辞：100% 无幻觉 → 显著降低 + CI 漂移校验 [新增]” | audit_specs.md + verify_context_memory.sh |
| Zone3 Local MCP Server → 中枢 Qwen-Agent 的 Local Load 双向边           | 每次 PR 审计时把四份文件压 token 后挂到 system prompt |
| 风险点 R4 “静默误杀真实漏洞”                                            | sinks_whitelist.json + §4 豁免清单“只豁免 LLM 推理不豁免 L1 扫描” 的双重护栏 |
| Roadmap P0/P1/P2/P3                                                    | 指南 §10 给的落地节奏 |

---

（文档结束）
