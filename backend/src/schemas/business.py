from typing import Literal
from pydantic import BaseModel,Field,field_validator
class LoginInput(BaseModel):
    email:str=Field(max_length=120)
    password:str=Field(max_length=200)
class ProcessInput(BaseModel):
    code:str=Field(min_length=1,max_length=50)
    name:str=Field(min_length=2,max_length=100)
    temperature:float=Field(ge=80,le=250,allow_inf_nan=False)
    dwell:float=Field(gt=0,le=10,allow_inf_nan=False)
    pressure:float=Field(gt=0,le=2,allow_inf_nan=False)
    note:str=Field(default='',max_length=2000)
class SampleInput(BaseModel):
    code:str=Field(min_length=1,max_length=50)
    process_id:int=Field(gt=0)
    lot:str=Field(min_length=1,max_length=60)
    force:float=Field(gt=0,le=200,allow_inf_nan=False)
    flagged:bool=False
    note:str=Field(default='',max_length=1000)
class AssessmentInput(BaseModel):
    title:str=Field(min_length=2,max_length=120)
    process_ids:list[int]=Field(min_length=2,max_length=8)
    lot:str=Field(default='全部',min_length=1,max_length=60)
class RecommendationInput(BaseModel):
    selected_process_id:int=Field(gt=0)
    recommendation:str=Field(min_length=20,max_length=3000)

class CurvePoint(BaseModel):
    t:float=Field(ge=-1e6,le=1e6,allow_inf_nan=False)
    f:float=Field(ge=-1e4,le=1e4,allow_inf_nan=False)

class ClampEvent(BaseModel):
    type:Literal['open','close']
    t:float=Field(ge=-1e6,le=1e6,allow_inf_nan=False)
    specimen:str=Field(default='',max_length=50)

class PacketInput(BaseModel):
    seq:int=Field(ge=1,le=100000)
    declared_total:int|None=Field(default=None,ge=1,le=100000)
    curve:list[CurvePoint]=Field(default_factory=list,max_length=200000)
    events:list[ClampEvent]=Field(default_factory=list,max_length=10000)

class AcquisitionBatchInput(BaseModel):
    code:str=Field(min_length=1,max_length=50)
    process_id:int=Field(gt=0)
    lot:str=Field(min_length=1,max_length=60)
    expected_codes:list[str]=Field(min_length=1,max_length=200)
    note:str=Field(default='',max_length=2000)

    @field_validator('expected_codes')
    @classmethod
    def _codes(cls,values):
        cleaned=[value.strip() for value in values]
        if any(not value for value in cleaned):raise ValueError('预期试条编号不能为空')
        if len(set(cleaned))!=len(cleaned):raise ValueError('预期试条编号不能重复')
        return cleaned

class SelectAttemptInput(BaseModel):
    attempt_id:int=Field(gt=0)
    note:str=Field(default='',max_length=1000)
