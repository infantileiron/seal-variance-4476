import{request,post}from'./client.js';
export const list=()=>request('/acquisitions');
export const create=data=>post('/acquisitions',data);
export const detail=id=>request('/acquisitions/'+id);
export const receive=(id,data)=>post('/acquisitions/'+id+'/packets',data);
export const seal=id=>post('/acquisitions/'+id+'/seal',{});
export const retry=id=>post('/acquisitions/'+id+'/retry',{});
export const reopen=id=>post('/acquisitions/'+id+'/reopen',{});
export const selectAttempt=(id,data)=>request('/acquisitions/attempts/'+id,{method:'PATCH',body:JSON.stringify(data)});
