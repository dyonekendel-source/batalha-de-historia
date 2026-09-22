import json, random, string, asyncio, base64, io
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import qrcode

ROOT = Path(__file__).parent
DATA = json.loads((ROOT / 'questions.json').read_text(encoding='utf-8'))
app = FastAPI(title='Batalha de História')
app.mount('/static', StaticFiles(directory=ROOT / 'public'), name='static')
rooms = {}

def shuffle(items):
    items = list(items); random.shuffle(items); return items

def pick(topic):
    chosen = random.sample(topic['questions'], 10)
    result = []
    for q in chosen:
        opts = shuffle(q['options'])
        result.append({**q, 'options': opts, 'correctIndex': opts.index(q['correct_answer'])})
    return result

def make_code():
    while True:
        c = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if c not in rooms: return c

@app.get('/')
def home(): return FileResponse(ROOT / 'public/index.html')
@app.get('/teacher.html')
def teacher(): return FileResponse(ROOT / 'public/teacher.html')
@app.get('/student.html')
def student(): return FileResponse(ROOT / 'public/student.html')
@app.get('/api/topics')
def topics():
    return JSONResponse([{'id': t['id'], 'name': t['name'], 'question_count': t['question_count']} for t in DATA['topics']])
@app.get('/api/qr')
def qr(request: Request, room: str):
    host = request.headers.get('host', 'localhost:3000')
    proto = request.headers.get('x-forwarded-proto', request.url.scheme)
    url = f'{proto}://{host}/student.html?room={room.upper()}'
    img = qrcode.make(url)
    b = io.BytesIO(); img.save(b, format='PNG')
    return PlainTextResponse(base64.b64encode(b.getvalue()).decode())

async def send(ws, msg):
    try: await ws.send_json(msg)
    except: pass

def teacher_state(r):
    return {'type':'state','room':r['id'],'topic':r['topic']['name'],'phase':r['phase'],
            'questionNumber':r['current']+1,'total':10,
            'question':r['questions'][r['current']] if r['phase']=='question' else None,
            'timeLeft':r['timeLeft'],
            'players':sorted([{'name':p['name'],'score':p['score'],'answered':p['answered']} for p in r['players'].values()], key=lambda x:x['score'], reverse=True)}

def student_state(r,p):
    q = r['questions'][r['current']] if r['phase']=='question' else None
    return {'type':'state','phase':r['phase'],'questionNumber':r['current']+1,'total':10,
            'question':({'question':q['question'],'options':q['options']} if q else None),
            'timeLeft':r['timeLeft'],'score':p['score'],'answered':p['answered']}

async def broadcast(r):
    if r.get('teacher'): await send(r['teacher'], teacher_state(r))
    for p in list(r['players'].values()): await send(p['ws'], student_state(r,p))

async def timer(r):
    while r['phase']=='question' and r['timeLeft']>0:
        await asyncio.sleep(1); r['timeLeft'] -= 1; await broadcast(r)
    if r['phase']=='question': await finish(r)

async def start_question(r):
    r['phase']='question'; r['current'] += 1; r['timeLeft']=20
    for p in r['players'].values(): p['answered']=False; p['answer']=None
    await broadcast(r); asyncio.create_task(timer(r))

async def finish(r):
    if r['phase']!='question': return
    q=r['questions'][r['current']]; r['phase']='result'
    for p in r['players'].values():
        if p['answer']==q['correctIndex']:
            p['score'] += 1000 + max(0, r['timeLeft'])*50
        p['answered']=False
    await broadcast(r)

@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    await ws.accept(); role=None; room=None; player_id=None
    try:
        while True:
            m=await ws.receive_json(); action=m.get('action')
            if action=='create':
                topic=next((t for t in DATA['topics'] if t['id']==m.get('topicId')), DATA['topics'][0])
                rid=make_code(); room={'id':rid,'topic':topic,'teacher':ws,'players':{},'questions':pick(topic),
                'current':-1,'phase':'lobby','timeLeft':20}; rooms[rid]=room; role='teacher'
                await send(ws, {'type':'created','room':rid,'topic':topic['name']}); await broadcast(room)
            elif action=='join':
                rid=str(m.get('room','')).upper(); r=rooms.get(rid)
                if not r: await send(ws, {'type':'error','message':'Sala não encontrada.'}); continue
                if r['phase']!='lobby': await send(ws, {'type':'error','message':'A partida já começou.'}); continue
                player_id=str(id(ws)); p={'name':str(m.get('name','Aluno'))[:24],'score':0,'answered':False,'answer':None,'ws':ws}
                r['players'][player_id]=p; room=r; role='student'; await send(ws, {'type':'joined','room':rid}); await broadcast(r)
            elif action=='start' and role=='teacher' and room:
                if room['phase']=='lobby' and room['players']: await start_question(room)
            elif action=='answer' and role=='student' and room and room['phase']=='question':
                p=room['players'].get(player_id)
                if p and not p['answered']:
                    p['answer']=int(m.get('index')); p['answered']=True; await broadcast(room)
                    if room['players'] and all(x['answered'] for x in room['players'].values()): await finish(room)
            elif action=='next' and role=='teacher' and room and room['phase']=='result':
                if room['current']>=9: room['phase']='podium'; await broadcast(room)
                else: await start_question(room)
    except WebSocketDisconnect:
        if role=='student' and room and player_id in room['players']:
            del room['players'][player_id]; await broadcast(room)
