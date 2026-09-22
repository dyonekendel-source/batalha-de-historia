import base64, io, json, random, string
from pathlib import Path
import qrcode
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
ROOT=Path(__file__).parent
DATA=json.loads((ROOT/'questions.json').read_text(encoding='utf-8'))
app=FastAPI(title='Batalha de História')
app.mount('/static',StaticFiles(directory=ROOT/'public'),name='static')
rooms={}
def pick(topic):
    out=[]
    for q in random.sample(topic['questions'],10):
        opts=list(q['options']); random.shuffle(opts)
        out.append({**q,'options':opts,'correctIndex':opts.index(q['correct_answer'])})
    return out
def code():
    while True:
        c=''.join(random.choices(string.ascii_uppercase+string.digits,k=4))
        if c not in rooms:return c
def ranking(r):
    return sorted([{'name':p['name'],'score':p['score'],'answered':p['answered']} for p in r['players'].values()],key=lambda x:x['score'],reverse=True)
def podium(r):return ranking(r)[:3]
@app.get('/')
def home():return FileResponse(ROOT/'public/index.html')
@app.get('/teacher.html')
def teacher():return FileResponse(ROOT/'public/teacher.html')
@app.get('/student.html')
def student():return FileResponse(ROOT/'public/student.html')
@app.get('/api/topics')
def topics():return JSONResponse([{'id':t['id'],'name':t['name'],'question_count':t['question_count']} for t in DATA['topics']])
@app.get('/api/qr')
def qr(request:Request,room:str):
    host=request.headers.get('host','localhost:3000'); proto=request.headers.get('x-forwarded-proto',request.url.scheme)
    img=qrcode.make(f'{proto}://{host}/student.html?room={room.upper()}'); b=io.BytesIO(); img.save(b,format='PNG')
    return PlainTextResponse(base64.b64encode(b.getvalue()).decode())
async def send(ws,msg):
    try:await ws.send_json(msg)
    except Exception:pass
def tstate(r):
    q=r['questions'][r['current']] if r['current']>=0 else None
    question=None; result=None
    if r['phase']=='question' and q: question={'question':q['question'],'options':q['options'],'difficulty':q.get('difficulty'),'subtopic':q.get('subtopic')}
    if r['phase']=='result' and q: result={'correctIndex':q['correctIndex'],'correctAnswer':q['correct_answer'],'answeredCount':sum(1 for p in r['players'].values() if p['last_answered'])}
    return {'type':'state','room':r['id'],'topic':r['topic']['name'],'phase':r['phase'],'questionNumber':r['current']+1,'total':10,'question':question,'result':result,'players':ranking(r),'podium':podium(r)}
def sstate(r,p):
    q=r['questions'][r['current']] if r['current']>=0 else None; question=None; result=None
    if r['phase']=='question' and q:question={'question':q['question'],'options':q['options']}
    if r['phase']=='result' and q:result={'correctIndex':q['correctIndex'],'correctAnswer':q['correct_answer'],'answeredCount':sum(1 for x in r['players'].values() if x['last_answered'])}
    return {'type':'state','phase':r['phase'],'questionNumber':r['current']+1,'total':10,'question':question,'result':result,'score':p['score'],'answered':p['answered'],'podium':podium(r)}
async def broadcast(r):
    if r.get('teacher'):await send(r['teacher'],tstate(r))
    for p in list(r['players'].values()):await send(p['ws'],sstate(r,p))
async def start(r):
    r['phase']='question';r['current']+=1
    for p in r['players'].values():p['answered']=False;p['answer']=None;p['last_answered']=False
    await broadcast(r)
async def finish(r):
    if r['phase']!='question':return
    q=r['questions'][r['current']];r['phase']='result'
    for p in r['players'].values():
        p['last_answered']=p['answered']
        if p['answered'] and p['answer']==q['correctIndex']:p['score']+=1000
        p['answered']=False
    await broadcast(r)
@app.websocket('/ws')
async def ws_endpoint(ws:WebSocket):
    await ws.accept();role=None;room=None;pid=None
    try:
        while True:
            m=await ws.receive_json();a=m.get('action')
            if a=='create':
                topic=next((t for t in DATA['topics'] if t['id']==m.get('topicId')),DATA['topics'][0]);rid=code()
                room={'id':rid,'topic':topic,'teacher':ws,'players':{},'questions':pick(topic),'current':-1,'phase':'lobby'};rooms[rid]=room;role='teacher'
                await send(ws,{'type':'created','room':rid,'topic':topic['name']});await broadcast(room)
            elif a=='join':
                rid=str(m.get('room','')).upper();r=rooms.get(rid)
                if not r:await send(ws,{'type':'error','message':'Sala não encontrada.'});continue
                if r['phase']!='lobby':await send(ws,{'type':'error','message':'A partida já começou.'});continue
                pid=str(id(ws));r['players'][pid]={'name':str(m.get('name','Aluno')).strip()[:24] or 'Aluno','score':0,'answered':False,'answer':None,'last_answered':False,'ws':ws};room=r;role='student'
                await send(ws,{'type':'joined','room':rid});await broadcast(r)
            elif a=='start' and role=='teacher' and room and room['phase']=='lobby' and room['players']:await start(room)
            elif a=='answer' and role=='student' and room:
                p=room['players'].get(pid)
                if p and room['phase']=='question' and not p['answered']:
                    try:i=int(m.get('index'))
                    except (TypeError,ValueError):continue
                    if 0<=i<=3:p['answer']=i;p['answered']=True;await broadcast(room)
            elif a=='finish' and role=='teacher' and room:await finish(room)
            elif a=='next' and role=='teacher' and room and room['phase']=='result':
                if room['current']>=9:room['phase']='podium';await broadcast(room)
                else:await start(room)
            elif a=='finalize' and role=='teacher' and room and room['phase']=='podium':room['phase']='final';await broadcast(room)
    except WebSocketDisconnect:
        if role=='student' and room and pid in room['players']:del room['players'][pid];await broadcast(room)
        elif role=='teacher' and room:room['teacher']=None
