"""采集批 API 集成测试：用 SQLite 临时库与临时产物目录，覆盖业务关键路径。"""
import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

_tmp = tempfile.mkdtemp(prefix='seal-acq-test-')
os.environ['DATABASE_URL'] = 'sqlite+pysqlite:///' + Path(_tmp, 'test.db').as_posix()
os.environ['REDIS_URL'] = 'redis://localhost:6379/15'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['ARTIFACTS_DIR'] = str(Path(_tmp, 'artifacts'))
os.environ['SEED_DEMO'] = 'false'

from src.config.database import Base, SessionLocal, engine  # noqa: E402
from src.models.entities import AcquisitionBatch, AcquisitionPacket, Process, Sample, User  # noqa: E402
from src.main import app  # noqa: E402
from src.services import acquisition  # noqa: E402
from src.utils.security import hash_password, token_for  # noqa: E402


@pytest.fixture(scope='function')
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=f'{role}@demo.local', role=role, password_hash=hash_password('Demo123!')))
        db.add(Process(code='HC-T1', name='测试工艺', temperature=160, dwell=0.8, pressure=0.32, note=''))
        db.commit()
    with TestClient(app) as test_client:
        yield test_client


def auth(client, role='analyst'):
    response = client.post('/auth/login', json={'email': f'{role}@demo.local', 'password': 'Demo123!'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


def make_batch(client, headers, codes=('T-01', 'T-02'), code='B-001'):
    response = client.post('/acquisitions', headers=headers, json={
        'code': code, 'process_id': 1, 'lot': 'HC-T', 'expected_codes': list(codes), 'note': '',
    })
    assert response.status_code == 201, response.text
    return response.json()['id']


def send(client, headers, batch_id, seq, curve, events, total=2):
    return client.post(f'/acquisitions/{batch_id}/packets', headers=headers, json={
        'seq': seq, 'declared_total': total,
        'curve': [{'t': t, 'f': f} for t, f in curve],
        'events': [{'type': kind, 't': t, 'specimen': specimen} for kind, t, specimen in events],
    })


def detail(client, headers, batch_id):
    response = client.get(f'/acquisitions/{batch_id}', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_cross_packet_peak_and_publication(client):
    headers = auth(client)
    batch_id = make_batch(client, headers)
    assert send(client, headers, batch_id, 1,
                [(0, 1), (1, 6), (2, 8)],
                [('open', 0, 'T-01')]).status_code == 201
    assert send(client, headers, batch_id, 2,
                [(3, 31), (4, 30), (5, 9)],
                [('close', 3.5, 'T-01')]).status_code == 201
    data = detail(client, headers, batch_id)
    attempts = data['attempts']
    assert len(attempts) == 1
    assert attempts[0]['max_force'] == 31
    # 区间 [0, 3.5] 覆盖分包 1 的 3 个点与分包 2 的前 1 个点。
    assert attempts[0]['point_count'] == 4

    # 缺第二条试条，封批必须隔离而不是发布。
    sealed = client.post(f'/acquisitions/{batch_id}/seal', headers=headers)
    assert sealed.status_code == 200
    assert sealed.json()['status'] == 'quarantined'
    assert 'T-02' in sealed.json()['status_reason']


def test_missing_packet_can_be_resent_then_sealed(client):
    headers = auth(client)
    batch_id = make_batch(client, headers)
    send(client, headers, batch_id, 2, [(3, 20), (4, 2)], [('open', 3, 'T-02'), ('close', 4, 'T-02')])
    assert detail(client, headers, batch_id)['missing_seqs'] == [1]
    sealed = client.post(f'/acquisitions/{batch_id}/seal', headers=headers).json()
    assert sealed['status'] == 'quarantined' and '缺少曲线分包序号 1' in sealed['status_reason']

    assert client.post(f'/acquisitions/{batch_id}/reopen', headers=headers).json()['status'] == 'collecting'
    send(client, headers, batch_id, 1, [(0, 1), (1, 9)], [('open', 0, 'T-01'), ('close', 1, 'T-01')], total=2)
    published = client.post(f'/acquisitions/{batch_id}/seal', headers=headers)
    assert published.status_code == 200, published.text
    assert published.json()['status'] == 'published'
    samples = {s['code']: s for s in detail(client, headers, batch_id)['samples']}
    assert samples['T-01']['force'] == 9
    assert samples['T-02']['force'] == 20


def test_quarantined_batch_can_reseal_without_reopen(client):
    headers = auth(client)
    batch_id = make_batch(client, headers, codes=('T-01',), code='B-RESEAL')
    send(client, headers, batch_id, 1, [(0, 1), (1, 9)], [('open', 0, 'T-01')], total=2)
    first = client.post(f'/acquisitions/{batch_id}/seal', headers=headers).json()
    assert first['status'] == 'quarantined' and first['sealed_at'] is None
    assert '缺少曲线分包序号 2' in first['status_reason']

    # 隔离批可直接补传，补传后回到收包中再封批。
    supplement = send(client, headers, batch_id, 2, [(2, 12), (3, 3)], [('close', 2, 'T-01')], total=2)
    assert supplement.status_code == 201
    resealed = client.post(f'/acquisitions/{batch_id}/seal', headers=headers)
    assert resealed.status_code == 200, resealed.text
    assert resealed.json()['status'] == 'published'
    assert resealed.json()['sealed_at']
    data = detail(client, headers, batch_id)
    assert data['attempts'][0]['max_force'] == 12
    assert len(data['samples']) == 1


def test_regrip_keeps_all_attempts_and_selected_one_feeds_sample(client):
    headers = auth(client)
    batch_id = make_batch(client, headers, codes=('T-01',), code='B-REG')
    send(client, headers, batch_id, 1,
         [(0, 2), (1, 12), (2, 3), (3, 4), (4, 18), (5, 5)],
         [('open', 0, 'T-01'), ('close', 2, 'T-01'), ('open', 3, 'T-01'), ('close', 5, 'T-01')], total=1)
    data = detail(client, headers, batch_id)
    attempts = data['attempts']
    assert [a['attempt_index'] for a in attempts] == [1, 2]
    assert [a['max_force'] for a in attempts] == [12, 18]
    assert [a['selected'] for a in attempts] == [True, False]

    # 人工改选第二次夹持，依据要留痕。
    second = attempts[1]['id']
    patched = client.patch(f'/acquisitions/attempts/{second}', headers=headers,
                           json={'attempt_id': second, 'note': '首次夹具打滑，采用第二次完整夹持'})
    assert patched.status_code == 200, patched.text
    data = detail(client, headers, batch_id)
    selected = [a for a in data['attempts'] if a['selected']]
    assert len(selected) == 1 and selected[0]['attempt_index'] == 2
    assert '夹具打滑' in selected[0]['selection_note']

    published = client.post(f'/acquisitions/{batch_id}/seal', headers=headers).json()
    assert published['status'] == 'published'
    samples = detail(client, headers, batch_id)['samples']
    assert len(samples) == 1
    assert samples[0]['force'] == 18
    assert samples[0]['flagged'] is True
    assert '第 2 次夹持' in samples[0]['note']

    # 发布后夹持选择冻结。
    frozen = client.patch(f'/acquisitions/attempts/{attempts[0]["id"]}', headers=headers,
                          json={'attempt_id': attempts[0]['id'], 'note': 'x'})
    assert frozen.status_code == 409


def test_out_of_order_packets_and_duplicate_are_idempotent(client):
    headers = auth(client)
    batch_id = make_batch(client, headers, codes=('T-01',), code='B-OOO')
    send(client, headers, batch_id, 2, [(2, 25), (3, 4)], [('close', 2, 'T-01')], total=2)
    first = send(client, headers, batch_id, 1, [(0, 1), (1, 5)], [('open', 0, 'T-01')], total=2)
    assert first.status_code == 201
    assert first.json()['duplicate'] is False
    # 乱序补齐后峰值仍跨包。
    assert detail(client, headers, batch_id)['attempts'][0]['max_force'] == 25

    repeat = send(client, headers, batch_id, 1, [(0, 1), (1, 5)], [('open', 0, 'T-01')], total=2)
    assert repeat.json()['duplicate'] is True
    with SessionLocal() as db:
        assert db.scalar(select(AcquisitionPacket).where(AcquisitionPacket.batch_id == batch_id).where(AcquisitionPacket.seq == 1)) is not None
    assert detail(client, headers, batch_id)['packet_count'] == 2

    conflict = send(client, headers, batch_id, 1, [(0, 1)], [], total=2)
    assert conflict.status_code == 409


def test_sealed_batch_without_publication_can_be_reopened(client, monkeypatch):
    headers = auth(client)
    batch_id = make_batch(client, headers, codes=('T-01',), code='B-REOPEN')
    send(client, headers, batch_id, 1, [(0, 1), (1, 9)], [('open', 0, 'T-01'), ('close', 1, 'T-01')], total=1)

    original = acquisition._write_archive
    monkeypatch.setattr(acquisition, '_write_archive', lambda *a, **k: (_ for _ in ()).throw(OSError('卷离线')))
    assert client.post(f'/acquisitions/{batch_id}/seal', headers=headers).status_code == 503

    reopened = client.post(f'/acquisitions/{batch_id}/reopen', headers=headers).json()
    assert reopened['status'] == 'collecting'
    assert reopened['sealed_at'] is None
    assert reopened['published_at'] is None

    monkeypatch.setattr(acquisition, '_write_archive', original)
    assert client.post(f'/acquisitions/{batch_id}/seal', headers=headers).json()['status'] == 'published'


def test_packet_after_seal_is_rejected_not_absorbed(client):
    headers = auth(client)
    batch_id = make_batch(client, headers, codes=('T-01',), code='B-LOCK')
    send(client, headers, batch_id, 1, [(0, 1), (1, 9)], [('open', 0, 'T-01'), ('close', 1, 'T-01')], total=1)
    assert client.post(f'/acquisitions/{batch_id}/seal', headers=headers).json()['status'] == 'published'
    late = send(client, headers, batch_id, 2, [(2, 10)], [], total=1)
    assert late.status_code == 409
    data = detail(client, headers, batch_id)
    assert data['packet_count'] == 1
    assert len(data['samples']) == 1


def test_viewer_cannot_receive_or_seal(client):
    headers = auth(client, 'viewer')
    with SessionLocal() as db:
        analyst = db.scalar(select(User).where(User.role == 'analyst'))
        editor_headers = {'Authorization': 'Bearer ' + token_for(analyst)}
    batch_id = make_batch(client, editor_headers, codes=('T-01',), code='B-RO')
    assert send(client, headers, batch_id, 1, [(0, 1)], [('open', 0, 'T-01')], total=1).status_code == 403
    assert client.post(f'/acquisitions/{batch_id}/seal', headers=headers).status_code == 403


def test_archive_failure_parks_batch_and_retry_does_not_duplicate(client, monkeypatch):
    headers = auth(client)
    batch_id = make_batch(client, headers, codes=('T-01',), code='B-FAIL')
    send(client, headers, batch_id, 1, [(0, 1), (1, 15)], [('open', 0, 'T-01'), ('close', 1, 'T-01')], total=1)

    original = acquisition._write_archive

    def boom(*args, **kwargs):
        raise OSError('磁盘只读')

    monkeypatch.setattr(acquisition, '_write_archive', boom)
    failed = client.post(f'/acquisitions/{batch_id}/seal', headers=headers)
    assert failed.status_code == 503
    data = detail(client, headers, batch_id)
    assert data['status'] == 'pending_recovery'
    assert '曲线原件保存失败' in data['status_reason']
    assert data['samples'] == []

    monkeypatch.setattr(acquisition, '_write_archive', original)
    recovered = client.post(f'/acquisitions/{batch_id}/retry', headers=headers)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['status'] == 'published'
    data = detail(client, headers, batch_id)
    assert len(data['samples']) == 1

    # 再次重试不新增样本。
    again = client.post(f'/acquisitions/{batch_id}/retry', headers=headers)
    assert again.status_code == 200
    with SessionLocal() as db:
        rows = db.scalars(select(Sample).where(Sample.acquisition_batch_id == batch_id)).all()
        assert len(rows) == 1
        archive = Path(db.get(AcquisitionBatch, batch_id).archive_path)
        payload = json.loads(archive.read_text())
    assert len(payload['packets']) == 1
    assert any(attempt['selected'] for attempt in payload['attempts'])


def test_sample_save_failure_parks_batch_then_retry_does_not_duplicate(client, monkeypatch):
    """原件已保存但样本落库失败时，同样停在待恢复；重试只补样本、不重复入样。"""
    headers = auth(client)
    batch_id = make_batch(client, headers, codes=('T-01',), code='B-SFAIL')
    send(client, headers, batch_id, 1, [(0, 1), (1, 15)], [('open', 0, 'T-01'), ('close', 1, 'T-01')], total=1)

    original = acquisition._build_samples
    calls = {'n': 0}

    def flaky(batch, attempts, path, already):
        calls['n'] += 1
        rows = original(batch, attempts, path, already)
        if calls['n'] == 1:
            raise RuntimeError('样本表暂时不可写')
        return rows

    monkeypatch.setattr(acquisition, '_build_samples', flaky)
    failed = client.post(f'/acquisitions/{batch_id}/seal', headers=headers)
    assert failed.status_code == 503
    data = detail(client, headers, batch_id)
    assert data['status'] == 'pending_recovery'
    assert '样本保存失败' in data['status_reason']
    assert data['samples'] == []
    assert data['archive_path'], '原件应已保存成功并留痕'

    recovered = client.post(f'/acquisitions/{batch_id}/retry', headers=headers)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['status'] == 'published'
    with SessionLocal() as db:
        rows = db.scalars(select(Sample).where(Sample.acquisition_batch_id == batch_id)).all()
        assert len(rows) == 1
    again = client.post(f'/acquisitions/{batch_id}/retry', headers=headers)
    assert again.status_code == 200
    with SessionLocal() as db:
        assert len(db.scalars(select(Sample).where(Sample.acquisition_batch_id == batch_id)).all()) == 1


def test_sample_code_conflict_is_quarantined_before_writing(client):
    headers = auth(client)
    batch_id = make_batch(client, headers, codes=('T-01',), code='B-CONFLICT')
    send(client, headers, batch_id, 1, [(0, 1), (1, 15)], [('open', 0, 'T-01'), ('close', 1, 'T-01')], total=1)
    with SessionLocal() as db:
        db.add(Sample(code='T-01', process_id=1, lot='手工', force=1.0, flagged=False, note='手工录入'))
        db.commit()
    failed = client.post(f'/acquisitions/{batch_id}/seal', headers=headers)
    assert failed.status_code == 200
    assert failed.json()['status'] == 'quarantined'
    assert '已被手工录入占用' in failed.json()['status_reason']
    assert detail(client, headers, batch_id)['samples'] == []


def test_manual_samples_still_work_and_acquisition_does_not_mix(client):
    headers = auth(client)
    manual = client.post('/samples', headers=headers, json={
        'code': 'M-01', 'process_id': 1, 'lot': 'HC-T', 'force': 20.5, 'flagged': False, 'note': '手工抄录',
    })
    assert manual.status_code == 201
    batch_id = make_batch(client, headers, codes=('T-01',), code='B-MIX')
    send(client, headers, batch_id, 1, [(0, 1), (1, 21)], [('open', 0, 'T-01'), ('close', 1, 'T-01')], total=1)
    client.post(f'/acquisitions/{batch_id}/seal', headers=headers)
    samples = client.get('/samples', headers=headers).json()
    assert {s['code'] for s in samples} == {'M-01', 'T-01'}
    sources = {s['code']: s['source'] for s in samples}
    assert sources == {'M-01': 'manual', 'T-01': 'acquisition'}


def test_concurrent_seal_and_receive_never_loses_or_mixes(client):
    """封批与收包并发时结果必须是可解释的三种之一，且不会混入封批后数据。"""
    import threading

    def run_once(code, include_second_packet):
        headers = auth(client)
        batch_id = make_batch(client, headers, codes=('T-01',), code=code)
        send(client, headers, batch_id, 1, [(0, 1), (1, 5)], [('open', 0, 'T-01')], total=2)
        outcome = {}
        threads = [threading.Thread(target=lambda: outcome.__setitem__(
            'seal', client.post(f'/acquisitions/{batch_id}/seal', headers=headers)))]
        if include_second_packet:
            threads.append(threading.Thread(target=lambda: outcome.__setitem__(
                'receive', send(client, headers, batch_id, 2, [(2, 25)], [('close', 2, 'T-01')], total=2))))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return batch_id, headers, outcome

    for attempt in range(6):
        batch_id, headers, outcome = run_once(f'B-RACE-{attempt}', True)
        data = detail(client, headers, batch_id)
        seal_response = outcome['seal']
        receive_response = outcome.get('receive')
        assert seal_response.status_code in (200, 503)
        if receive_response is not None:
            assert receive_response.status_code in (201, 409)
        if receive_response is not None and receive_response.status_code == 201:
            # 收包落库在封批观察之前：两包都必须参与拼接。
            assert data['packet_count'] == 2
            assert data['attempts'][0]['max_force'] == 25
        else:
            # 封批先完成或补传被拒：批次不能包含第二包。
            assert data['packet_count'] == 1
            assert data['attempts'][0]['max_force'] == 5
        # 只有已发布批次才会产出样本；任何发布都必须与收到的分包一致。
        if data['status'] == 'published':
            assert data['packet_count'] == 2
            assert len(data['samples']) == 1
        else:
            assert data['samples'] == []

    # 无并发竞争时，封批后到达的分包必须被拒绝，且原批次不变。
    headers = auth(client)
    batch_id = make_batch(client, headers, codes=('T-09',), code='B-AFTER')
    send(client, headers, batch_id, 1, [(0, 1), (1, 9)], [('open', 0, 'T-09'), ('close', 1, 'T-09')], total=1)
    assert client.post(f'/acquisitions/{batch_id}/seal', headers=headers).json()['status'] == 'published'
    late = send(client, headers, batch_id, 2, [(2, 10)], [], total=1)
    assert late.status_code == 409
    data = detail(client, headers, batch_id)
    assert data['packet_count'] == 1 and len(data['samples']) == 1
