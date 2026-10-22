# 并发用例、探针与守卫：把"偶尔绿"变成可证伪

本文件是 SKILL.md 的深度补充：遇到偶发红、探针假绿、守卫误报时按这里的步骤做。

## 1. 偶发红先分类，别先改代码

三类，判据是"同一份产品代码在放大条件下是否复现"：

| 类 | 症状 | 判据 |
|---|---|---|
| 产品真串行/竞态 | 换机器、加负载也复现 | 能给出线程名/锁持有者/事件归属 |
| 测试同步手法 | 只在机器卡顿时红 | 注入延迟即可复现旧写法红、新写法绿 |
| 探针共享状态 | 同一份代码既红又绿 | 用例串行后结果稳定 |

**禁止**先改产品再观察、或先放宽断言再看。先写一个 30 秒的探针证明属于哪类。

## 2. 并发用例：屏障是唯一的同步原语

反例（会红在与产品无关的断言上）：

```python
# 桩里嵌入硬超时 —— 机器卡顿超时后，这一轮以 error 收尾并被摘出池，
# cancel 随后返回 no_running_turn，用例红在 assert cancelled["ok"] is True
assert release_a.wait(timeout=10)
```

正例：

```python
barrier = threading.Barrier(2)

# 每轮模型调用前都过屏障（收口轮也算，不能只放第一轮）
barrier.wait(timeout=30)   # 30s 只当死锁哨兵，不承担同步语义
```

- **每一轮/每个阶段都要过屏障**。只在"第一轮模型调用前"汇合，会让最后到达者被 `notify_all`
  立即唤醒，另一方还要再抢一次条件锁 → 对端延迟 ≥1ms 就出现 `[B第一轮, B收口轮, A第一轮, A收口轮]`，
  而两轮事件一条不少（说明产品没问题，断言太苛刻）。
- **"对端还没收尾"用结果信箱证明**（`runtime.result(turn_b) is None`），不要等一个时间窗口。
- `timeout` 只当死锁哨兵；`finally` 里 `barrier.abort()`，避免红的时候把后续用例挂住。

### 验收标准（缺一不可）

1. 正常连跑 **30/30** 全绿；
2. **注入对端延迟**（1/3/8ms 各 10 次）仍然 **30/30**；
3. 同一条注入条件下，**旧写法必须红**（否则证明不了你修的是那个 flake）；
4. 汇报里如实写明"注入条件是构造的，不是当初红的现场"。

```bash
for i in $(seq 30); do pytest tests/runtime/test_x.py::test_y -q || break; done
```

## 3. 探针：串行、每例 reset、留调用序

反例：四条异步用例 `Promise.all` 并发、共用 `sent/notices/running`。后一条的 `sent.length = 0`
抹掉前一条刚 push 的唯一记录 → **同一份产品代码同时给出**：

- 假红：`预期 ['只看论文'] 实际 []`
- 假绿："再点一次没有重复发送" 其实**从来没有发过**

正例：逐条 `await` 串行；每例开头 `resetSpies()`；保留 `order` 数组记录 `open:s2` / `send:都可以` 的先后。

**桩要绑到产品真正读的那个标识符**：产品函数在**声明时**捕获 `send`/`openSession`，事后 `var send = …`
不会生效。把桩注入到 `new Function` 的作用域里，**不要为了好测去改产品签名**。

## 4. 守卫：更精确，不是更松

- 检查前**剥注释**（引号里的 `//` 不是注释，剥错会把后文吞成空判）。
- 每放宽一条规则，**补一条反例用例**：代码里硬编码必须红、注释里出现不许红。
- 设计令牌守卫：用**具名白名单**（`ALLOWED_NEW_TOKENS`）+ 注释说明"为什么这是新维度"，
  其余令牌仍然 0 个；断言里留一条"白名单外的名字会被拦"。
- 改守卫前先跑守卫自己的测试拿基线；**假绿比红危险**（它静默关掉一道检查）。

## 5. 绝对几何断言必须钉死窗口

- `BASELINE_GEOM`（如 `left216/chat1042/right380`）只在**某个窗口宽度**下成立，窗口一改就失效。
- Edge `--window-size` 是**外框**：实测视口 = 外框 − `(30,95)`。
- 三个常量：`BASELINE_WINDOW=(1680,1000)`、`WINDOW_CHROME=(30,95)`、`BASELINE_VIEWPORT=(1650,905)`；
  探针第一步校验视口，不等**直接返回错误**，不许静默出数。

## 6. 外部接口：能力判定读元数据，降级要留痕

- `GET {base}/models` 可能直接带 `supported_levels` / `default_level` —— 比文档可靠。
  实测 `effort.supported_levels=["low","high","max"]`、`default_level="high"`，于是五档 UI 映射到三个真档位，
  **选项文字标真实档位**（"中（high）"），不假装五档各不相同。
- 降级路径：接口 **400 且错误里提到该参数** → 摘掉字段**只重试一次**，并在回包标注
  `reasoning_degraded` / 说明文案；非该原因的 400 一次都不重试。
- **循环依赖的判断**：`level_options.supported` 之类若用"降级后"的值判，降级必然落回支持集 →
  每一档都被判支持、置灰形同虚设。用**原始映射**判（`LEVEL_TO_EFFORT[value] in supported`）。

## 7. 第三方身份标识：桩测不出"跨应用"

- `open_id` 这类 id **按应用隔离**：拿另一个应用的 `open_id` 发消息，飞书恒回
  `{"code":99992361,"msg":"open_id cross app"}`（HTTP 400）。
- 修法：**入站时**把事件里的 `sender.sender_id.open_id` upsert 进配对表（`source=inbound`），
  只在白名单内写、`drop` 分支不写、空昵称不覆盖、写库失败只记日志。表里原有的手工 id 会在
  用户下一次私聊机器人时被覆盖。
- 发送失败要把平台 **code/msg** 带进结果与日志（`FetchError.body` / `platform_code()`），
  只报"请求失败（400）"等于把可诊断信息丢掉。
- 收尾前至少真发一条（可发给用户本人），不要只用桩。

## 8. 对 `object` 形参做数值转换

```python
if isinstance(value, bool) or value is None:
    return None                     # bool 是 int 子类，必须先排除
if isinstance(value, (int, float)):
    number = int(value)             # float 版：float(value)
elif isinstance(value, str):
    try:
        number = int(value.strip())  # ValueError -> None
    except ValueError:
        return None
```

禁 `type: ignore`、禁把形参标 `Any` 掩写、禁改签名迎合检查器。

## 9. 并行写者下"重新应用"而不是"覆盖"

- 旧 stash / 旧 worktree 里的 `config.py`、`feishu_send.py` 可能已被别的批次改过：
  只**读设计意图**，把改动重新加到**当前版本**上。
- 提交前用 `git diff --numstat -- <file>` 核：别人改过的文件应当 **0 删除行**；
  有删除就逐行说明（例：`send_message_url(domain)` 加 `receive_id_type=` 默认参数，老调用点语义不变）。
- 两批混在一个工作区时**分两笔提交**（渠道一笔、界面一笔），否则回滚互相牵连。
