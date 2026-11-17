"""曲线分包拼接与夹持区间提取。

本模块保持纯函数：输入为按序排好的分包内容，输出夹持区间、峰值与隔离原因，
不接触数据库，便于对跨包峰值、重夹与缺包规则做单元测试。
"""
import hashlib
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict

FORCE_LIMIT = 200.0


def packet_checksum(seq, declared_total, curve, events):
    """计算分包内容摘要；curve/events 为规范化后的基础类型列表。"""
    canonical = json.dumps(
        {'seq': seq, 'declared_total': declared_total, 'curve': curve, 'events': events},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _merge_curve(packets, issues):
    """按时间拼接所有分包的曲线点，输出 [(t, f)] 与时间轴。

    同一时刻重复出现同值视为分包重叠，去重；不同值则记录隔离原因并保留较早分包的值。
    """
    raw = []
    for packet in packets:
        for index, point in enumerate(packet['curve']):
            raw.append((point['t'], packet['seq'], index, point['f']))
    raw.sort(key=lambda item: (item[0], item[1], item[2]))

    merged = []
    conflict = False
    for t, _seq, _index, force in raw:
        if merged and merged[-1][0] == t:
            if merged[-1][1] != force:
                conflict = True
            continue
        merged.append((t, force))
    if conflict:
        issues.append('跨包同一时刻存在不同拉力读数，已保留较早分包的值，请核对原始曲线')
    return merged, [point[0] for point in merged]


def _pair_events(events, issues):
    """把开夹/闭夹事件配成夹持区间。事件已按时间稳定排序。"""
    intervals = []
    pending = None
    for t, seq, index, kind, specimen in events:
        if kind == 'open':
            if pending is not None:
                issues.append(
                    f'开夹事件在 t={t:g}s 处与上一次夹持重叠：试条 {pending["specimen"] or "（未编号）"} '
                    f'自 t={pending["t"]:g}s 起没有闭夹事件（分包 {pending["seq"]}）'
                )
            if not specimen:
                issues.append(f'开夹事件在 t={t:g}s 处缺少试条编号，无法归属入样（分包 {seq}）')
            pending = {'t': t, 'specimen': specimen, 'seq': seq}
            continue
        if pending is None:
            issues.append(f'闭夹事件在 t={t:g}s 处缺少配对的开夹事件（分包 {seq}）')
            continue
        opened = pending
        pending = None
        if not opened['specimen']:
            # 已在开夹时记录原因，此区间无法命名试条，直接丢弃。
            continue
        if specimen and specimen != opened['specimen']:
            issues.append(
                f'开闭夹试条编号不一致：{opened["specimen"]} / {specimen}，区间 t={opened["t"]:g}–{t:g}s'
            )
        intervals.append({
            'specimen': opened['specimen'], 'open_t': opened['t'], 'close_t': t,
            'open_seq': opened['seq'], 'close_seq': seq,
        })
    if pending is not None:
        issues.append(
            f'试条 {pending["specimen"] or "（未编号）"} 的开夹事件 t={pending["t"]:g}s 后没有闭夹事件（分包 {pending["seq"]}）'
        )
    return intervals


def _attempt_points(times, merged, open_t, close_t):
    lo = bisect_left(times, open_t)
    hi = bisect_right(times, close_t)
    return [{'t': merged[i][0], 'f': merged[i][1]} for i in range(lo, hi)]


def derive(packets, expected_codes, batch_total=None, require_expected=False):
    """由分包推导每次夹持区间。

    packets: [{seq, declared_total, curve:[{t,f}], events:[{type,t,specimen}]}]
    返回 issues / attempts / missing_seqs / declared_total。
    """
    issues = []
    packets = sorted(packets, key=lambda packet: packet['seq'])

    totals = {packet['declared_total'] for packet in packets if packet['declared_total'] is not None}
    if batch_total is not None:
        totals.add(batch_total)
    if len(totals) > 1:
        issues.append('各分包声明的总包数不一致：' + '、'.join(str(value) for value in sorted(totals)))
    declared_total = batch_total
    if declared_total is None and packets:
        declared_total = packets[0]['declared_total']

    received = {packet['seq'] for packet in packets}
    missing = []
    if declared_total is not None:
        missing = [seq for seq in range(1, declared_total + 1) if seq not in received]
        extra = sorted(seq for seq in received if seq > declared_total)
        if extra:
            issues.append('收到超出批次声明总包数的分包序号：' + '、'.join(str(seq) for seq in extra))
    if missing:
        issues.append('缺少曲线分包序号 ' + '、'.join(str(seq) for seq in missing) + '，请补传后重新校核')

    merged, times = _merge_curve(packets, issues)

    events = []
    for packet in packets:
        for index, event in enumerate(packet['events']):
            events.append((
                event['t'], packet['seq'], index, event['type'], event.get('specimen', '') or '',
            ))
    events.sort(key=lambda item: (item[0], item[1], item[2]))

    intervals = _pair_events(events, issues)
    expected = set(expected_codes or [])
    seen_expected = set()
    attempts = []
    attempt_no = 0
    per_specimen = defaultdict(int)

    for interval in intervals:
        points = _attempt_points(times, merged, interval['open_t'], interval['close_t'])
        per_specimen[interval['specimen']] += 1
        attempt_no += 1
        attempt = {
            'attempt_no': attempt_no,
            'specimen_code': interval['specimen'],
            'attempt_index': per_specimen[interval['specimen']],
            'open_t': interval['open_t'],
            'close_t': interval['close_t'],
            'point_count': len(points),
            'max_force': None,
            'peak_t': None,
            'valid': False,
            'curve': points,
        }
        if not points:
            issues.append(
                f'试条 {interval["specimen"]} 第{attempt["attempt_index"]}次夹持区间 t={interval["open_t"]:g}–{interval["close_t"]:g}s 内没有曲线点'
            )
        else:
            peak = max(points, key=lambda point: point['f'])
            attempt['max_force'] = peak['f']
            attempt['peak_t'] = peak['t']
            if peak['f'] <= 0 or peak['f'] > FORCE_LIMIT:
                issues.append(
                    f'试条 {interval["specimen"]} 第{attempt["attempt_index"]}次夹持峰值 {peak["f"]:g}N 超出 0–{FORCE_LIMIT:g}N 录入范围'
                )
            else:
                attempt['valid'] = True
        if interval['specimen'] in expected:
            seen_expected.add(interval['specimen'])
        elif expected:
            issues.append(f'出现预期外试条 {interval["specimen"]}，请核对取样计划后再发布')
        attempts.append(attempt)

    if require_expected:
        absent = [code for code in (expected_codes or []) if code not in seen_expected]
        if absent:
            issues.append('缺少预期试条：' + '、'.join(absent) + '，全齐后才能发布样本')
        if not packets:
            issues.append('尚未收到任何曲线分包')

    # 去重并保持顺序，便于页面稳定展示。
    deduped = list(dict.fromkeys(issues))
    return {
        'issues': deduped,
        'attempts': attempts,
        'missing_seqs': missing,
        'declared_total': declared_total,
        'expected_codes': list(expected_codes or []),
    }
