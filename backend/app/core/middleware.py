import os
from uuid import uuid4,UUID
from starlette.responses import JSONResponse
from starlette.exceptions import HTTPException
from .errors import safe_error

class RequestSecurityMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        headers=dict(scope['headers']);request_id=str(uuid4())
        supplied=headers.get(b'x-request-id',b'').decode('ascii',errors='ignore')
        try:
            if str(UUID(supplied))==supplied:request_id=supplied
        except ValueError:pass
        scope.setdefault('state',{})['request_id']=request_id
        origin=headers.get(b'origin',b'').decode('ascii',errors='ignore')
        allowed=os.environ.get('HRMS_ALLOWED_ORIGINS','http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000').split(',')
        if scope['method'] not in ('GET','HEAD','OPTIONS') and origin and origin not in allowed:
            return await JSONResponse({'error':{'code':'forbidden','message':'Origin kabul edilmedi.'}},status_code=403)(scope,receive,send)
        try:length=int(headers.get(b'content-length',b'0'))
        except ValueError:length=-1
        if length<0 or length>12*1024*1024:
            return await JSONResponse({'error':{'code':'request_too_large','message':'İstek boyutu sınırı aşıldı.'}},status_code=413)(scope,receive,send)
        received=0
        async def limited_receive():
            nonlocal received
            message=await receive()
            received+=len(message.get('body',b''))
            if received>12*1024*1024:raise HTTPException(413,'İstek boyutu sınırı aşıldı.')
            return message
        started=False
        async def secure_send(message):
            nonlocal started
            if message['type']=='http.response.start':
                started=True
                extra=[(b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'no-referrer'),(b'x-request-id',request_id.encode())]
                if not any(k.lower()==b'cache-control' for k,v in message['headers']):extra.append((b'cache-control',b'no-store'))
                message['headers']=list(message['headers'])+extra
            await send(message)
        try:
            await self.app(scope,limited_receive,secure_send)
        except Exception as error:
            safe=safe_error(error)
            if started:raise safe from None
            await JSONResponse({'error':{'code':safe.code,'message':safe.message}},status_code=safe.status_code)(scope,limited_receive,secure_send)
