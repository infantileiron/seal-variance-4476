import pytest

import src.services.acquisition_curves as curves


def packet(seq, points, events, declared_total=2):
    return {
        'seq': seq,
        'declared_total': declared_total,
        'curve': [{'t': t, 'f': f} for t, f in points],
        'events': [{'type': kind, 't': t, 'specimen': specimen} for kind, t, specimen in events],
    }


def test_cross_packet_peak_uses_both_packets_and_open_close_events():
    packets = [
        packet(1, [(0.0, 1.0), (1.0, 5.0), (2.0, 7.0)], [('open', 0.5, 'S-01')]),
        packet(2, [(3.0, 30.0), (4.0, 9.0)], [('close', 3.5, 'S-01'), ('open', 4.0, 'S-02'), ('close', 4.5, 'S-02')]),
    ]
    result = curves.derive(packets, ['S-01', 'S-02'])
    assert result['missing_seqs'] == []
    assert result['issues'] == []
    assert len(result['attempts']) == 2
    first = result['attempts'][0]
    assert first['specimen_code'] == 'S-01'
    assert first['point_count'] == 3
    assert first['max_force'] == 30.0
    assert first['peak_t'] == 3.0
    assert first['valid'] is True
    assert result['attempts'][1]['max_force'] == 9.0


def test_regrip_keeps_each_attempt_and_marks_second_attempt():
    packets = [packet(
        1,
        [(0, 2), (1, 12), (2, 3), (3, 4), (4, 18), (5, 5)],
        [('open', 0, 'S-01'), ('close', 2, 'S-01'), ('open', 3, 'S-01'), ('close', 5, 'S-01')],
        declared_total=1,
    )]
    result = curves.derive(packets, ['S-01'])
    attempts = result['attempts']
    assert [item['attempt_index'] for item in attempts] == [1, 2]
    assert [item['max_force'] for item in attempts] == [12, 18]
    assert result['issues'] == []


def test_missing_packet_is_quarantined_and_identified():
    packets = [packet(2, [(1, 1), (2, 2)], [('open', 1, 'S-01'), ('close', 2, 'S-01')])]
    result = curves.derive(packets, ['S-01'], require_expected=True)
    assert result['missing_seqs'] == [1]
    assert any('缺少曲线分包序号 1' in issue for issue in result['issues'])


def test_unmatched_events_are_isolated():
    packets = [packet(
        1, [(0, 1), (1, 2), (2, 3)],
        [('close', 0.5, 'S-01'), ('open', 1, 'S-02'), ('open', 1.5, 'S-03'), ('close', 2, 'S-03')],
        declared_total=1,
    )]
    result = curves.derive(packets, ['S-01', 'S-02', 'S-03'])
    joined = '\n'.join(result['issues'])
    assert '缺少配对的开夹事件' in joined
    assert 'S-02' in joined and '没有闭夹事件' in joined
    assert [item['specimen_code'] for item in result['attempts']] == ['S-03']


def test_duplicate_timestamp_with_different_force_is_flagged():
    packets = [packet(1, [(0, 1), (1, 2)], [('open', 0, 'S-01'), ('close', 1, 'S-01')], declared_total=1)]
    packets.append(packet(2, [(1, 99), (2, 3)], [], declared_total=1))
    result = curves.derive(packets, ['S-01'])
    assert '同一时刻' in '\n'.join(result['issues'])


def test_expected_specimen_absent_blocks_publication():
    packets = [packet(1, [(0, 1), (1, 2)], [('open', 0, 'S-01'), ('close', 1, 'S-01')], declared_total=1)]
    result = curves.derive(packets, ['S-01', 'S-02'], require_expected=True)
    assert any('缺少预期试条：S-02' in issue for issue in result['issues'])


def test_out_of_range_peak_marks_attempt_invalid():
    packets = [packet(1, [(0, 1), (1, 250)], [('open', 0, 'S-01'), ('close', 1, 'S-01')], declared_total=1)]
    result = curves.derive(packets, ['S-01'])
    assert result['attempts'][0]['valid'] is False
    assert '超出' in '\n'.join(result['issues'])


def test_packet_checksum_is_canonical_and_order_sensitive_on_values():
    curve = [{'t': 0.0, 'f': 1.0}]
    events = [{'type': 'open', 't': 0.0, 'specimen': 'S'}]
    assert curves.packet_checksum(1, 2, curve, events) == curves.packet_checksum(1, 2, list(curve), list(events))
    assert curves.packet_checksum(1, 2, curve, events) != curves.packet_checksum(2, 2, curve, events)
