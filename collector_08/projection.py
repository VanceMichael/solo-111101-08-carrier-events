"""多来源轨迹投影：时间线归并、履约状态派生、冲突与更正裁决。

裁决规则（与 README 来源协议一致）：
- 轨迹身份为 authority+id；event_at 统一 UTC 排序，received_at 只作接收证据。
- 相同事件时间和权威的不同终态必须 conflict，不借输入顺序选赢家。
- 不同权威按配置权重裁决；平权且终态相矛盾时同样 conflict。
- replaces 是显式更正，只能替换同权威已存在事件；循环或不存在目标的更正记录被隔离。
- 旧事件保留在 raw 与 superseded，不做原地覆写。
"""
from dataclasses import dataclass
from datetime import datetime, timezone

# 映射注册表：mapping_version -> 承运商代码到履约状态的映射。
# 版本 2 是样例映射，与版本 1 语义相同，用于检验版本隔离。
MAPPINGS = {
    1: {"TRANSIT": "transit", "DELIVERED": "delivered", "RETURNED": "returned"},
    2: {"TRANSIT": "transit", "DELIVERED": "delivered", "RETURNED": "returned"},
}

TERMINAL_STATES = frozenset({"delivered", "returned"})

_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def parse_instant(value):
    """ISO-8601 字符串 -> 带时区的 UTC datetime；缺失时区按 UTC 处理。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("时间为空")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"时间不是 ISO-8601: {text!r}") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_instant(moment):
    """UTC datetime -> ISO-8601 字符串（Z 后缀）。"""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ProjectionEvent:
    authority: str
    event_id: str
    parcel: str
    code: str
    event_at: str
    received_at: str
    replaces: str
    source: str
    rowid: int
    moment: datetime = None
    state: str = None

    @property
    def identity(self):
        return (self.authority, self.event_id)


def _isolated_entry(record, reason):
    return {"parcel": record.parcel, "event": record.event_id, "authority": record.authority, "reason": reason}


def _received_key(record):
    try:
        return parse_instant(record.received_at)
    except ValueError:
        return _FAR_FUTURE


def _cycle_identities(edges):
    """返回处于 replaces 环上的 identity 集合（含自环）。"""
    cyclic = set()
    color = {}

    def visit(node, stack):
        color[node] = 1
        stack.append(node)
        for target in edges.get(node, ()):
            state = color.get(target, 0)
            if state == 1:
                cyclic.update(stack[stack.index(target):])
            elif state == 0:
                visit(target, stack)
        stack.pop()
        color[node] = 2

    for node in list(edges):
        if color.get(node, 0) == 0:
            visit(node, [])
    return cyclic


def build_projection(rows, mapping_version, weights=None):
    """把 raw 记录行投影为可审计时间线与派生状态。

    rows 为 dict 序列，键：rowid、source、authority、event_id、parcel、code、
    event_at、received_at、replaces。weights 为 authority -> 数值权重。
    """
    if mapping_version not in MAPPINGS:
        raise ValueError(f"未注册的 mapping_version: {mapping_version!r}")
    mapping = MAPPINGS[mapping_version]
    weights = dict(weights or {})

    isolated = []
    events = []
    for row in rows:
        record = ProjectionEvent(
            authority=str(row.get("authority") or "").strip(),
            event_id=str(row.get("event_id") or "").strip(),
            parcel=str(row.get("parcel") or "").strip(),
            code=str(row.get("code") or "").strip(),
            event_at=str(row.get("event_at") or "").strip(),
            received_at=str(row.get("received_at") or "").strip(),
            replaces=str(row.get("replaces") or "").strip(),
            source=str(row.get("source") or ""),
            rowid=int(row.get("rowid") or 0),
        )
        problem = None
        if not record.authority or not record.event_id or not record.parcel:
            problem = "bad_identity"
        else:
            try:
                record.moment = parse_instant(record.event_at)
            except ValueError:
                problem = "bad_event_at"
            else:
                state = mapping.get(record.code)
                if state is None:
                    problem = "unmapped_code"
                else:
                    record.state = state
        if problem:
            isolated.append(_isolated_entry(record, problem))
        else:
            events.append(record)

    # 更正校验：只能替换同权威已存在事件；循环或不存在目标的更正记录被隔离。
    identities = {event.identity for event in events}
    edges = {}
    for event in events:
        if event.replaces:
            edges.setdefault(event.identity, set()).add((event.authority, event.replaces))
    cyclic = _cycle_identities(edges)
    kept = []
    for event in events:
        if event.replaces:
            if (event.authority, event.replaces) not in identities:
                isolated.append(_isolated_entry(event, "replaces_missing"))
                continue
            if event.identity in cyclic:
                isolated.append(_isolated_entry(event, "replaces_cycle"))
                continue
        kept.append(event)
    events = kept

    # 有效更正生效：目标身份整体 superseded（旧事件保留在 raw，不参与投影）。
    superseded_identities = {(event.authority, event.replaces) for event in events if event.replaces}
    active = [event for event in events if event.identity not in superseded_identities]

    # 重复扫描合并：相同身份+事件本质（parcel/code/UTC 时刻）只保留一条，
    # 接收证据取最早 received_at；同身份但本质分歧的版本全部保留并显式提示。
    conflicts = []
    by_identity = {}
    for event in active:
        by_identity.setdefault(event.identity, []).append(event)
    merged = []
    for (authority, event_id), versions in sorted(by_identity.items()):
        groups = {}
        for event in versions:
            essence = (event.parcel, event.code, format_instant(event.moment))
            groups.setdefault(essence, []).append(event)
        if len(groups) > 1:
            conflicts.append({
                "parcel": versions[0].parcel,
                "authority": authority,
                "events": sorted({event_id}),
                "reason": "divergent_versions",
            })
        for copies in groups.values():
            copies.sort(key=lambda event: (_received_key(event), event.rowid))
            merged.append((copies[0], len(copies)))
    active = [head for head, _ in merged]

    by_parcel = {}
    for event in active:
        by_parcel.setdefault(event.parcel, []).append(event)

    # 终态矛盾裁决：同权威同时刻的不同终态必须冲突；跨权威同时刻平权且终态矛盾同样冲突。
    for parcel, parcel_events in sorted(by_parcel.items()):
        by_instant = {}
        for event in parcel_events:
            by_instant.setdefault((event.authority, event.moment), []).append(event)
        for (authority, moment), group in sorted(by_instant.items(), key=lambda item: (item[0][1], item[0][0])):
            terminals = [event for event in group if event.state in TERMINAL_STATES]
            if len({event.state for event in terminals}) > 1:
                conflicts.append({
                    "parcel": parcel,
                    "authority": authority,
                    "event_at": format_instant(moment),
                    "events": sorted({event.event_id for event in terminals}),
                    "reason": "terminal_disagreement",
                })
        by_moment = {}
        for event in parcel_events:
            by_moment.setdefault(event.moment, []).append(event)
        for moment, group in sorted(by_moment.items()):
            terminals = [event for event in group if event.state in TERMINAL_STATES]
            if len({event.state for event in terminals}) <= 1:
                continue
            best = max(weights.get(event.authority, 0) for event in terminals)
            top = [event for event in terminals if weights.get(event.authority, 0) == best]
            if len({event.state for event in top}) > 1 and len({event.authority for event in top}) > 1:
                conflicts.append({
                    "parcel": parcel,
                    "event_at": format_instant(moment),
                    "events": sorted({event.event_id for event in top}),
                    "reason": "authority_tie",
                })

    conflicted = {conflict["parcel"] for conflict in conflicts}
    parcels = []
    for parcel, parcel_events in sorted(by_parcel.items()):
        if parcel in conflicted:
            state = "conflict"
        else:
            latest = max(event.moment for event in parcel_events)
            candidates = [event for event in parcel_events if event.moment == latest]
            best = max(weights.get(event.authority, 0) for event in candidates)
            top = [event for event in candidates if weights.get(event.authority, 0) == best]
            states = {event.state for event in top}
            if len(states) == 1:
                state = states.pop()
            else:
                terminal = states & TERMINAL_STATES
                if terminal:
                    # 同一时刻终态优先于在途；不同终态并存已在上面判为冲突。
                    state = sorted(terminal)[0]
                else:
                    conflicts.append({
                        "parcel": parcel,
                        "events": sorted({event.event_id for event in top}),
                        "reason": "unresolvable_states",
                    })
                    state = "conflict"
        parcels.append({"id": parcel, "state": state})

    timeline = []
    for head, copies in merged:
        entry = {
            "parcel": head.parcel,
            "event": head.event_id,
            "authority": head.authority,
            "state": head.state,
            "event_at": format_instant(head.moment),
            "received_at": head.received_at,
            "weight": weights.get(head.authority, 0),
            "source": head.source,
        }
        if copies > 1:
            entry["copies"] = copies
        timeline.append((head, entry))
    timeline.sort(key=lambda pair: (pair[0].moment, pair[0].event_id, pair[0].authority))

    conflicts.sort(key=lambda conflict: (conflict["parcel"], conflict["reason"], conflict["events"]))
    isolated.sort(key=lambda entry: (entry["parcel"], entry["event"], entry["reason"]))
    return {
        "parcels": parcels,
        "timeline": [entry for _, entry in timeline],
        "conflicts": conflicts,
        "superseded": sorted({identity[1] for identity in superseded_identities}),
        "isolated": isolated,
    }
