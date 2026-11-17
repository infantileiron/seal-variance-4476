from fastapi import APIRouter, Depends
from src.config.database import get_db
from src.utils.security import current_user, editor
from src.schemas.business import AcquisitionBatchInput, PacketInput, SelectAttemptInput
from src.services import acquisition
from src.controllers.business import serialize
router = APIRouter()


@router.get('/acquisitions')
def listing(db=Depends(get_db), user=Depends(current_user)):
    return [acquisition.serialize_summary(db, batch) for batch in acquisition.listing(db)]


@router.post('/acquisitions', status_code=201)
def create(data: AcquisitionBatchInput, db=Depends(get_db), user=Depends(editor)):
    return serialize(acquisition.create_batch(db, data, user))


@router.get('/acquisitions/{batch_id}')
def detail(batch_id: int, db=Depends(get_db), user=Depends(current_user)):
    data, _samples = acquisition.detail(db, batch_id)
    return data


@router.post('/acquisitions/{batch_id}/packets', status_code=201)
def receive(batch_id: int, data: PacketInput, db=Depends(get_db), user=Depends(editor)):
    batch, duplicate = acquisition.receive_packet(db, batch_id, data, user)
    payload = acquisition.serialize_summary(db, batch)
    payload['duplicate'] = duplicate
    payload['status'] = batch.status
    return payload


@router.post('/acquisitions/{batch_id}/seal')
def seal(batch_id: int, db=Depends(get_db), user=Depends(editor)):
    batch = acquisition.seal(db, batch_id, user)
    return acquisition.serialize_summary(db, batch)


@router.post('/acquisitions/{batch_id}/retry')
def retry(batch_id: int, db=Depends(get_db), user=Depends(editor)):
    batch = acquisition.retry_publish(db, batch_id, user)
    return acquisition.serialize_summary(db, batch)


@router.post('/acquisitions/{batch_id}/reopen')
def reopen(batch_id: int, db=Depends(get_db), user=Depends(editor)):
    batch = acquisition.reopen(db, batch_id, user)
    return acquisition.serialize_summary(db, batch)


@router.patch('/acquisitions/attempts/{attempt_id}')
def select(attempt_id: int, data: SelectAttemptInput, db=Depends(get_db), user=Depends(editor)):
    batch = acquisition.select_attempt(db, attempt_id, data, user)
    return acquisition.serialize_summary(db, batch)
