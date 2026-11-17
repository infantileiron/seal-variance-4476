"""拉力曲线采集批：收包、拼接、隔离、封批、发布与恢复。

状态机
------
collecting ──收包(乱序/重复均可)──> collecting
collecting ──封批且校核通过──> sealed ──保存原件与样本──> published
collecting ──封批但存在缺包/不配对/缺预期试条──> quarantined ──撤销封批──> collecting
sealed / pending_recovery ──重试发布──> published 或 pending_recovery

封批与收包对同一批次加数据库行锁与进程内锁，封批之后的收包一律显式拒绝，
既不静默丢弃，也不混入已封批数据。发布按夹持区间唯一键幂等落库：曲线原件或
样本任一步保存失败都会停在 pending_recovery，重试不会重复入样。
"""
import hashlib
import json
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import delete, select

from src.models.entities import (
    AcquisitionAttempt, AcquisitionBatch, AcquisitionPacket, Process, Sample,
)
from src.services import acquisition_curves

ARTIFACT_ROOT = Path(os.getenv('ARTIFACTS_DIR', '/app/artifacts'))

_locks_guard = threading.Lock()
_batch_locks: dict[int, threading.RLock] = {}


def _lock_for(batch_id):
    with _locks_guard:
        return _batch_locks.setdefault(batch_id, threading.RLock())


def _now():
    return datetime.now(timezone.utc).isoformat()


def _packet_rows(db, batch_id):
    rows = db.scalars(
        select(AcquisitionPacket).where(AcquisitionPacket.batch_id == batch_id).order_by(AcquisitionPacket.seq)
    ).all()
    return rows


def _packet_payloads(rows):
    return [
        {'seq': row.seq, 'declared_total': row.declared_total, 'curve': row.curve, 'events': row.events}
        for row in rows
    ]


def _previous_manual_selection(db, batch_id):
    rows = db.scalars(
        select(AcquisitionAttempt).where(
            AcquisitionAttempt.batch_id == batch_id,
            AcquisitionAttempt.selection_mode == 'manual',
        )
    ).all()
    return {(row.specimen_code, round(row.open_t, 9)): (row.selection_note, row.open_t) for row in rows}


def recompute(db, batch):
    """由已收分包重建夹持区间，保留人工选定的夹持依据。"""
    rows = _packet_rows(db, batch.id)
    diagnostics = acquisition_curves.derive(
        _packet_payloads(rows), batch.expected_codes, batch.declared_total, require_expected=False,
    )
    manual = _previous_manual_selection(db, batch.id)

    attempts = diagnostics['attempts']
    by_specimen = defaultdict(list)
    for attempt in attempts:
        by_specimen[attempt['specimen_code']].append(attempt)

    issues = list(diagnostics['issues'])
    invalidated = []
    for attempt in attempts:
        attempt['selected'] = False
        attempt['selection_mode'] = 'auto'
        attempt['selection_note'] = ''
        key = (attempt['specimen_code'], round(attempt['open_t'], 9))
        if key in manual:
            if not attempt['valid']:
                invalidated.append((attempt['specimen_code'], attempt['open_t'], manual[key][0]))
                continue
            attempt['selected'] = True
            attempt['selection_mode'] = 'manual'
            attempt['selection_note'] = manual[key][0]

    # 每个试条默认取第一次有效夹持；重夹保留在明细中供核查。
    for specimen, group in by_specimen.items():
        if any(attempt['selected'] for attempt in group):
            continue
        first_valid = next((attempt for attempt in group if attempt['valid']), None)
        if first_valid is not None:
            first_valid['selected'] = True
            previous = next((item for item in invalidated if item[0] == specimen), None)
            if previous:
                # 原人工选择已随曲线变化失效，留痕但不阻塞封批，工程师可再改选。
                first_valid['selection_mode'] = 'fallback'
                first_valid['selection_note'] = f'原人工选定 t={previous[1]:g}s（{previous[2]}）已失效，回退首次有效夹持'

    db.execute(delete(AcquisitionAttempt).where(AcquisitionAttempt.batch_id == batch.id))
    db.flush()
    for attempt in attempts:
        db.add(AcquisitionAttempt(
            batch_id=batch.id,
            specimen_code=attempt['specimen_code'],
            attempt_no=attempt['attempt_no'],
            attempt_index=attempt['attempt_index'],
            open_t=attempt['open_t'],
            close_t=attempt['close_t'],
            point_count=attempt['point_count'],
            max_force=attempt['max_force'],
            peak_t=attempt['peak_t'],
            valid=attempt['valid'],
            selected=attempt['selected'],
            selection_mode=attempt['selection_mode'],
            selection_note=attempt['selection_note'],
            curve=attempt['curve'],
            created_at=_now(),
        ))
    db.flush()

    diagnostics['issues'] = list(dict.fromkeys(issues))
    batch.issues = diagnostics['issues']
    if diagnostics['declared_total'] is not None:
        batch.declared_total = diagnostics['declared_total']
    return diagnostics


def _require_editor(user):
    if user.role not in ('admin', 'analyst'):
        raise HTTPException(403, '只读账号不能接收分包、封批或选择夹持')


def create_batch(db, data, user):
    _require_editor(user)
    if db.scalar(select(AcquisitionBatch).where(AcquisitionBatch.code == data.code)):
        raise HTTPException(409, '采集批编号已存在')
    # 先按预期试条建立空批并写入计划，分包到达前的预期集是发布核对基准。
    process = db.get(Process, data.process_id)
    if not process:
        raise HTTPException(404, '工艺方案不存在')
    batch = AcquisitionBatch(
        code=data.code, process_id=data.process_id, lot=data.lot,
        expected_codes=list(data.expected_codes), status='collecting',
        status_reason='', issues=[], declared_total=None,
        note=(data.note or '').strip(),
        created_by=user.email, created_at=_now(),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def listing(db):
    return db.scalars(select(AcquisitionBatch).order_by(AcquisitionBatch.id.desc())).all()


def get(db, batch_id):
    batch = db.get(AcquisitionBatch, batch_id)
    if not batch:
        raise HTTPException(404, '采集批不存在')
    return batch


def _locked(db, batch_id):
    batch = db.scalar(
        select(AcquisitionBatch).where(AcquisitionBatch.id == batch_id).with_for_update()
    )
    if not batch:
        raise HTTPException(404, '采集批不存在')
    return batch


def receive_packet(db, batch_id, data, user):
    _require_editor(user)
    with _lock_for(batch_id):
        batch = _locked(db, batch_id)
        if batch.status not in ('collecting', 'quarantined'):
            db.rollback()
            raise HTTPException(
                409,
                f'采集批当前状态为 {batch.status}，不能接收分包；封批后数据不会混入，如需补传请先撤销封批',
            )
        existing = db.scalar(
            select(AcquisitionPacket).where(
                AcquisitionPacket.batch_id == batch.id, AcquisitionPacket.seq == data.seq,
            )
        )
        curve = [point.model_dump() for point in data.curve]
        events = [event.model_dump() for event in data.events]
        checksum = acquisition_curves.packet_checksum(data.seq, data.declared_total, curve, events)
        duplicate = False
        if existing:
            if existing.checksum != checksum:
                db.rollback()
                raise HTTPException(409, f'分包 {data.seq} 已存在但内容不同，请核对采集序号或先撤销该批')
            # 完全相同的重传是幂等的：不重复收包，也不改写既有诊断。
            duplicate = True
        else:
            if not curve and not events:
                db.rollback()
                raise HTTPException(422, '分包既无曲线点也无夹持事件')
            db.add(AcquisitionPacket(
                batch_id=batch.id, seq=data.seq, declared_total=data.declared_total,
                curve=curve, events=events, checksum=checksum,
                received_by=user.email, received_at=_now(),
            ))
            db.flush()
            recompute(db, batch)
            if batch.status == 'quarantined':
                # 隔离批收到补传后回到收包中，重新校核；未通过封批前不会发布。
                batch.status = 'collecting'
                batch.status_reason = ''
        db.commit()
        db.refresh(batch)
        return batch, duplicate


def select_attempt(db, attempt_id, data, user):
    _require_editor(user)
    attempt = db.get(AcquisitionAttempt, attempt_id)
    if not attempt:
        raise HTTPException(404, '夹持区间不存在')
    with _lock_for(attempt.batch_id):
        batch = _locked(db, attempt.batch_id)
        attempt = db.get(AcquisitionAttempt, attempt_id)
        if not attempt:
            db.rollback()
            raise HTTPException(404, '夹持区间不存在')
        if batch.status == 'published':
            db.rollback()
            raise HTTPException(409, '样本已发布，夹持选择已冻结')
        if batch.status not in ('collecting', 'quarantined'):
            db.rollback()
            raise HTTPException(409, '封批后不能改选夹持；请先撤销封批')
        if not attempt.valid:
            raise HTTPException(409, '该夹持区间无效，不能作为入样依据')
        siblings = db.scalars(
            select(AcquisitionAttempt).where(
                AcquisitionAttempt.batch_id == batch.id,
                AcquisitionAttempt.specimen_code == attempt.specimen_code,
            )
        ).all()
        for sibling in siblings:
            sibling.selected = sibling.id == attempt.id
            sibling.selection_mode = 'manual' if sibling.id == attempt.id else 'auto'
            if sibling.id == attempt.id:
                sibling.selection_note = data.note or '人工选定本次夹持'
        db.flush()
        recompute(db, batch)
        db.commit()
        db.refresh(batch)
        return batch


def _publish_failure(db, batch_id, message):
    db.rollback()
    batch = db.get(AcquisitionBatch, batch_id)
    if batch:
        batch.status = 'pending_recovery'
        batch.status_reason = message
        db.commit()
    raise HTTPException(503, message)


def _archive_payload(batch, rows, attempts):
    return {
        'batch': {
            'id': batch.id, 'code': batch.code, 'process_id': batch.process_id, 'lot': batch.lot,
            'expected_codes': batch.expected_codes, 'declared_total': batch.declared_total,
            'sealed_at': batch.sealed_at, 'created_by': batch.created_by, 'created_at': batch.created_at,
        },
        'packets': [
            {
                'seq': row.seq, 'declared_total': row.declared_total, 'checksum': row.checksum,
                'received_by': row.received_by, 'received_at': row.received_at,
                'curve': row.curve, 'events': row.events,
            }
            for row in rows
        ],
        'attempts': [
            {
                'specimen_code': attempt.specimen_code, 'attempt_no': attempt.attempt_no,
                'attempt_index': attempt.attempt_index, 'open_t': attempt.open_t,
                'close_t': attempt.close_t, 'point_count': attempt.point_count,
                'max_force': attempt.max_force, 'peak_t': attempt.peak_t,
                'selected': attempt.selected, 'selection_mode': attempt.selection_mode,
                'selection_note': attempt.selection_note, 'curve': attempt.curve,
            }
            for attempt in attempts
        ],
    }


def _write_archive(batch, rows, attempts):
    payload = _archive_payload(batch, rows, attempts)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    checksum = hashlib.sha256(raw).hexdigest()
    directory = ARTIFACT_ROOT / 'acquisition'
    directory.mkdir(parents=True, exist_ok=True)
    safe_code = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in batch.code)
    target = directory / f'{batch.id}-{safe_code}.json'
    tmp = target.with_name(f'.{target.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    with open(tmp, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return str(target), checksum


def _validate_ready(db, batch):
    reasons = list(batch.issues or [])
    attempts = db.scalars(
        select(AcquisitionAttempt).where(AcquisitionAttempt.batch_id == batch.id)
    ).all()
    if not batch.expected_codes:
        reasons.append('采集批没有登记预期试条')
    if not batch.declared_total:
        reasons.append('尚未收到声明总包数的分包')
    by_specimen = defaultdict(list)
    for attempt in attempts:
        by_specimen[attempt.specimen_code].append(attempt)
    for code in batch.expected_codes:
        group = by_specimen.get(code, [])
        selected = [attempt for attempt in group if attempt.selected]
        if not group:
            reasons.append(f'缺少预期试条 {code}')
        elif len(selected) != 1:
            reasons.append(f'试条 {code} 需要且只能选定 1 次夹持入样')
        elif not selected[0].valid:
            reasons.append(f'试条 {code} 选定的夹持无效')
    published = db.scalars(select(Sample).where(Sample.acquisition_batch_id == batch.id)).all()
    selected_ids = {attempt.id for attempt in attempts if attempt.selected}
    published_ids = {sample.acquisition_attempt_id for sample in published}
    if published_ids - selected_ids:
        reasons.append('存在与当前选定夹持不一致的样本记录，请先重试发布或撤销封批')
    # 手工样本编号冲突在写库前暴露，避免发布中途失败。
    for code in batch.expected_codes:
        existing = db.scalar(select(Sample).where(Sample.code == code))
        if existing and existing.acquisition_batch_id != batch.id:
            reasons.append(f'样本编号 {code} 已被手工录入占用，请先修订编号')
    return list(dict.fromkeys(reasons)), attempts


def _build_samples(batch, attempts, path, already):
    rows = []
    for attempt in attempts:
        if not attempt.selected or attempt.id in already:
            continue
        attempt_total = sum(1 for item in attempts if item.specimen_code == attempt.specimen_code)
        note = (
            f'采集批 {batch.code}；试条第 {attempt.attempt_index} 次夹持（共 {attempt_total} 次），'
            f'峰值取自 t={attempt.peak_t:g}s；曲线原件 {path}'
        )
        if attempt.selection_mode == 'manual' and attempt.selection_note:
            note += f'；人工选择依据：{attempt.selection_note}'
        rows.append(Sample(
            code=attempt.specimen_code, process_id=batch.process_id, lot=batch.lot,
            force=attempt.max_force, flagged=attempt_total > 1, note=note,
            source='acquisition', acquisition_batch_id=batch.id,
            acquisition_attempt_id=attempt.id, curve_ref=path,
        ))
    return rows


def _publish(db, batch_id):
    """在批次锁内完成原件与样本的落库；任一步失败都停在待恢复。"""
    with _lock_for(batch_id):
        batch = _locked(db, batch_id)
        if batch.status == 'published':
            db.rollback()
            return batch
        if batch.status not in ('sealed', 'pending_recovery'):
            db.rollback()
            raise HTTPException(409, f'采集批状态为 {batch.status}，不能发布')
        rows = _packet_rows(db, batch.id)
        attempts = db.scalars(
            select(AcquisitionAttempt).where(AcquisitionAttempt.batch_id == batch.id).order_by(AcquisitionAttempt.attempt_no)
        ).all()
        try:
            path, checksum = _write_archive(batch, rows, attempts)
        except Exception as error:
            _publish_failure(db, batch.id, f'曲线原件保存失败：{error}')

        # 先落库原件留痕再写样本：样本失败时仍能追溯原件路径与摘要。
        batch.archive_path = path
        batch.archive_sha256 = checksum
        db.commit()
        try:
            # 重新取行锁，避免另一进程在原件落库后抢先发布。
            batch = _locked(db, batch_id)
            if batch.status == 'published':
                db.rollback()
                return batch
            if batch.status not in ('sealed', 'pending_recovery'):
                db.rollback()
                raise HTTPException(409, f'采集批状态为 {batch.status}，不能发布')
            existing = {
                sample.acquisition_attempt_id
                for sample in db.scalars(select(Sample).where(Sample.acquisition_batch_id == batch.id)).all()
            }
            for sample in _build_samples(batch, attempts, path, existing):
                db.add(sample)
            db.flush()
            batch.status = 'published'
            batch.published_at = _now()
            batch.status_reason = ''
            db.commit()
            db.refresh(batch)
            return batch
        except HTTPException:
            raise
        except Exception as error:
            _publish_failure(db, batch.id, f'样本保存失败：{error}')


def seal(db, batch_id, user):
    _require_editor(user)
    with _lock_for(batch_id):
        batch = _locked(db, batch_id)
        if batch.status not in ('collecting', 'quarantined'):
            db.rollback()
            raise HTTPException(409, f'采集批状态为 {batch.status}，不能重复封批')
        batch.sealed_at = None
        diagnostics = recompute(db, batch)
        reasons, _attempts = _validate_ready(db, batch)
        reasons = list(dict.fromkeys(list(diagnostics['issues']) + reasons))
        batch.issues = reasons
        if reasons:
            batch.status = 'quarantined'
            batch.status_reason = '；'.join(reasons)
            db.commit()
            db.refresh(batch)
            return batch
        batch.status = 'sealed'
        batch.sealed_at = _now()
        batch.status_reason = ''
        db.commit()
    return _publish(db, batch_id)


def retry_publish(db, batch_id, user):
    _require_editor(user)
    with _lock_for(batch_id):
        batch = _locked(db, batch_id)
        if batch.status == 'published':
            db.rollback()
            return get(db, batch_id)
        if batch.status not in ('sealed', 'pending_recovery'):
            db.rollback()
            raise HTTPException(409, '只有已封批且未发布成功的批次可以重试发布')
        reasons, _attempts = _validate_ready(db, batch)
        if reasons:
            batch.status = 'quarantined'
            batch.issues = reasons
            batch.status_reason = '；'.join(reasons)
            db.commit()
            db.refresh(batch)
            return batch
        batch.status = 'sealed'
        batch.status_reason = ''
        db.commit()
    return _publish(db, batch_id)


def reopen(db, batch_id, user):
    _require_editor(user)
    with _lock_for(batch_id):
        batch = _locked(db, batch_id)
        if batch.status not in ('quarantined', 'sealed', 'pending_recovery'):
            db.rollback()
            raise HTTPException(409, '只有隔离、已封批或待恢复的批次可以撤销封批')
        published = db.scalars(select(Sample).where(Sample.acquisition_batch_id == batch.id)).all()
        if published:
            db.rollback()
            raise HTTPException(409, '已写入样本的批次不能撤销封批，请改用重试发布补全')
        batch.status = 'collecting'
        batch.sealed_at = None
        batch.published_at = None
        batch.status_reason = ''
        recompute(db, batch)
        db.commit()
        db.refresh(batch)
        return batch


def detail(db, batch_id):
    batch = get(db, batch_id)
    rows = _packet_rows(db, batch.id)
    attempts = db.scalars(
        select(AcquisitionAttempt).where(AcquisitionAttempt.batch_id == batch.id).order_by(AcquisitionAttempt.attempt_no)
    ).all()
    samples = db.scalars(select(Sample).where(Sample.acquisition_batch_id == batch.id).order_by(Sample.code)).all()
    received = [row.seq for row in rows]
    missing = []
    total = batch.declared_total
    if total:
        missing = [seq for seq in range(1, total + 1) if seq not in received]
    data = {column.name: getattr(batch, column.name) for column in batch.__table__.columns}
    data.update({
        'packet_count': len(rows),
        'received_seqs': received,
        'missing_seqs': missing,
        'attempt_count': len(attempts),
        'attempts': [
            {
                'id': attempt.id, 'specimen_code': attempt.specimen_code,
                'attempt_no': attempt.attempt_no, 'attempt_index': attempt.attempt_index,
                'open_t': attempt.open_t, 'close_t': attempt.close_t,
                'point_count': attempt.point_count, 'max_force': attempt.max_force,
                'peak_t': attempt.peak_t, 'valid': attempt.valid, 'selected': attempt.selected,
                'selection_mode': attempt.selection_mode, 'selection_note': attempt.selection_note,
                'curve': attempt.curve,
            }
            for attempt in attempts
        ],
        'packets': [
            {
                'seq': row.seq, 'declared_total': row.declared_total, 'point_count': len(row.curve or []),
                'event_count': len(row.events or []), 'checksum': row.checksum,
                'received_by': row.received_by, 'received_at': row.received_at,
            }
            for row in rows
        ],
        'samples': [
            {
                'id': sample.id, 'code': sample.code, 'force': sample.force,
                'flagged': sample.flagged, 'note': sample.note, 'curve_ref': sample.curve_ref,
                'acquisition_attempt_id': sample.acquisition_attempt_id,
            }
            for sample in samples
        ],
        'actions': {
            'receive': batch.status in ('collecting', 'quarantined'),
            'seal': batch.status in ('collecting', 'quarantined'),
            'retry': batch.status in ('sealed', 'pending_recovery'),
            'reopen': batch.status in ('quarantined', 'sealed', 'pending_recovery') and not samples,
        },
    })
    return data, samples


def serialize_summary(db, batch):
    rows = _packet_rows(db, batch.id)
    received = [row.seq for row in rows]
    total = batch.declared_total
    missing = [seq for seq in range(1, total + 1) if seq not in received] if total else []
    data = {column.name: getattr(batch, column.name) for column in batch.__table__.columns}
    data.update({
        'packet_count': len(rows), 'received_seqs': received, 'missing_seqs': missing,
        'issue_count': len(batch.issues or []),
    })
    return data
