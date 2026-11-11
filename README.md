# 跨境履约轨迹乱序归并

离线采集工程使用 Python 标准库和 SQLite。adapter 读取本项目的真实来源格式，sources 提供可重复响应序列，capture 原子归档解析后的来源记录。已有 --capture 只记录原始观察，不作业务投影；--run 工作流是待扩展功能。

## 环境与准备

推荐 Python 3.12.12，支持 Python 3.11+；requirements.lock 为空表示没有第三方包，运行、测试和验收均不下载数据或依赖。Dockerfile 提供同一 Linux 环境。需要容器时先在正式运行之外准备：

```sh
docker build -t q008-carrier-events:initial .
docker run --rm q008-carrier-events:initial
```

## 基线测试与原始采集

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q collector_08 tests acceptance
scratch=$(mktemp -d)
python3 -m collector_08.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /carrier
python3 -m collector_08.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /carrier
```

第一次 decoded=2、inserted=2，重抓同一观测 inserted=0。临时库不提交。tests 覆盖采集基础能力与 --run 扩展功能（投影裁决、更正隔离、通知去重、重放隔离）的回归测试。

## 来源协议

轨迹身份为 authority+id，event_at 统一 UTC 排序，received_at 只作接收证据不能决定履约先后。TRANSIT、DELIVERED、RETURNED 分别映射 transit、delivered、returned。相同事件时间和权威的不同终态必须 conflict，不能借输入顺序选赢家；不同权威按配置权重，但更正只能替换同权威已存在事件，循环或不存在目标须隔离。

replaces 是显式更正，旧事件保留在 raw 和 superseded，不能原地覆写。终态通知使用持久化去重键；普通采集首次得到 delivered 仅发一条，重复输入不得再发。replay=true 使用新映射生成独立投影、绝不写通知队列，不改变原始接收时点；mapping_version=2 的样例映射与1语义相同，重点检验版本隔离。

FixtureTransport 每次进程创建从 routes.json 首条响应开始，同一路由多次 request 依次推进，最后一条可重复；calls 保存请求证据。源数据相对 fixture_root 读取，不能越出该目录。所有响应是本地合成数据，不发出公网请求。单场景 JSON 的 fixture_root 相对当前工作目录解析；在仓库根目录执行即可。run_id 是采集批次标识，name 仅作展示标签。

## 扩展接口与可观察输出

`python3 -m collector_08.cli --run 场景文件 --database 状态库 --output 结果文件`

场景文件含 name、run_id、sources（有序根来源）、options、fixture_root；name 只作日志标签。expected 是公开最小预期，产品不应读取它。所有来源和选项应独立支持；四个场景不能替代全部边界测试。业务结果投影：parcels、timeline（UTC 时刻及接收证据）、conflicts、superseded、notifications（此次新建通知数）、raw_count。数组顺序不影响检查，公开预期数组是子集，但空数组要求确实为空。内部 SQLite 表、架构和调度实现由开发者选择。每个场景在同一状态库上用两个独立进程执行，以检查重启和重复输入；业务投影应保持一致，notifications 为数量时表示此次新建通知数，第二次应为0，为数组时表示持久化待处理通知清单。

## 投影裁决与输出契约

`--run` 把多来源轨迹解析为可审计时间线并派生当前履约状态，输出字段：

- `parcels`：每票 `{id, state}`，state 为 transit/delivered/returned/conflict。
- `timeline`：活跃事件按 event_at 的 UTC 时刻排序（跨时区偏移先归一化再比较，不按字符串排序）；每条带 `received_at` 接收证据、`authority`、`weight` 与 `source`。重复扫描（同权威+id+事件本质）合并为一条并计 `copies`。
- `conflicts`：无法裁决的矛盾，显式列出涉及事件。同权威同时刻的不同终态（`terminal_disagreement`）、跨权威同时刻平权终态矛盾（`authority_tie`）、同身份不同事件本质（`divergent_versions`）都会使包裹状态置为 conflict；冲突事件保留在时间线中作证据，不借输入顺序选赢家。
- `superseded`：被显式更正取代的事件 id。`replaces` 只能替换同权威已存在事件；旧事件保留在 raw 与 superseded，不做原地覆写。
- `isolated`：被隔离的更正记录及原因（`replaces_cycle` 循环/自环、`replaces_missing` 目标不存在或跨权威、`unmapped_code`、`bad_event_at`、`bad_identity`）。隔离记录留在 raw，不参与投影。
- `notifications`：此次新建通知数。
- `raw_count`：状态库中原始记录总数，幂等归档，重抓不重复。

跨权威矛盾按 options.weights（authority -> 数值，缺省 0）裁决，权重高者优先；同一时刻终态优先于在途。事件身份为 authority+id，received_at 只作接收证据，绝不决定履约先后。

## 通知去重与重放

普通采集（replay=false）对派生出的终态（delivered/returned）写通知队列：场景提供 portal_database 时写入该库，否则写入状态库。去重键为 `包裹:终态` 持久化唯一键，首次到达终态仅发一条，重复输入与进程重启（同一状态库再跑）不再发送，第二次运行 notifications 为 0。

replay=true 用于按订单批量重放历史消息验证新映射规则：用 options.mapping_version 指定的映射重新生成独立投影，绝不写通知队列（不触碰 portal 库），也不改变 raw 中的原始接收时点与接收证据（归档为 INSERT OR IGNORE，重放只读已存证据、幂等补缺）。

### 如何选择映射版本重放

映射注册表在 `collector_08/projection.py` 的 `MAPPINGS`：版本 1 是在用规则（TRANSIT/DELIVERED/RETURNED -> transit/delivered/returned），版本 2 是语义相同的样例映射，用于检验版本隔离。验证新规则的流程：

1. 在 `MAPPINGS` 注册新版本号及其代码映射（未注册的版本号会被拒绝）。
2. 场景 options 设 `"replay": true, "mapping_version": 新版本号`，对同一状态库重放历史来源。
3. 比较输出投影与线上版本：投影按 `(parcel, mapping_version)` 隔离持久化在 projections 表，重放版本不覆写线上版本，通知队列与原始接收证据均不变。
4. 确认无误后，把线上场景切到新版本号（replay=false）生效。

## 扩展回归测试

tests/test_projection.py 覆盖 UTC 归一化与排序、接收时间不决定履约、同权威终态冲突、跨权威权重与平权冲突、显式更正与链式更正、更正循环/自环/不存在目标/跨权威隔离、重复扫描合并、同身份分歧版本提示、未映射代码隔离、映射版本注册。tests/test_run.py 覆盖端到端投影与通知、进程内重跑去重、独立子进程重启去重、更正循环场景、重放不写通知且不改原始证据、版本隔离共存、未知映射版本拒绝。

## 验收与正式展示

```sh
python3 acceptance/check.py --contracts-only
python3 acceptance/check.py
```

contracts-only 仅检查材料结构，不能当成功能验收。第二条在初始版本会因尚无 --run 而失败，这是待开发功能的验收入口，不属于基线测试。完成开发后应四个场景均 PASS；每个场景独立临时目录，执行两次、每次进程超时5秒，八次总预算40秒，无固定 sleep。正式展示从干净状态执行基线测试和第二条命令，预算小于90秒；依赖和镜像预先准备，不能用旧日志代替运行。请另加参数变化及边界回归，不修改预期来迁就实现。
