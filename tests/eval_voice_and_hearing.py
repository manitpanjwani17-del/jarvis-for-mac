"""Benchmark: voice clarity (via Whisper round trip) and hearing speed/accuracy."""
import re, sys, time
import numpy as np
from scipy import signal
sys.path.insert(0, '.')
from jarvis_core.config import Config
from jarvis_core.voice import Voice, SR
from faster_whisper import WhisperModel

LINES = ["Good evening, Sir. All systems are online.",
         "Shall I send the message to your mother?",
         "The weather in Mumbai is thirty one degrees and sunny.",
         "I have taken the liberty of opening Spotify for you, Sir.",
         "Your meeting with Rahul begins in fifteen minutes."]
def norm(s): return re.sub(r"[^a-z0-9 ]","",s.lower().replace("31","thirty one").replace("15","fifteen")).split()
def wer(ref, hyp):
    r,h=norm(ref),norm(hyp); d=np.arange(len(h)+1)
    for i in range(1,len(r)+1):
        prev=d.copy(); d[0]=i
        for j in range(1,len(h)+1): d[j]=min(prev[j]+1,d[j-1]+1,prev[j-1]+(r[i-1]!=h[j-1]))
    return d[-1]/len(r)
to16=lambda a: signal.resample_poly(a,2,3).astype(np.float32)

models={}
import os
for name in [n for n in ['faster-whisper-small.en','faster-distil-whisper-large-v3'] if os.path.exists(f'models/hearing/{n}/model.bin')]:
    t=time.time(); models[name]=WhisperModel(f'models/hearing/{name}',device='cpu',compute_type='int8'); print(name,'load',round(time.time()-t,2))

blends={'george':{'bm_george':1},'fable':{'bm_fable':1},'lewis':{'bm_lewis':1},'daniel':{'bm_daniel':1},
 'george+fable':{'bm_george':.6,'bm_fable':.4},'george+lewis':{'bm_george':.7,'bm_lewis':.3},'fable+lewis':{'bm_fable':.5,'bm_lewis':.5}}
cfg=Config()
for bname,bl in blends.items():
  for pol in (0,1):
    cfg.voice.blend=bl; cfg.voice.polish=pol; v=Voice(cfg.voice)
    ws=[];syn=0
    for L in LINES:
        t=time.time(); a=v.render(L,use_cache=False); syn+=time.time()-t
        segs,_=models[list(models)[-1]].transcribe(to16(a),language='en',beam_size=1)
        ws.append(wer(L,' '.join(s.text for s in segs)))
    print(f'{bname:13s} polish={pol}  WER={np.mean(ws)*100:5.1f}%  synth/line={syn/len(LINES):.2f}s')

cfg.voice.blend={'bm_george':.6,'bm_fable':.4}; cfg.voice.polish=1; v=Voice(cfg.voice)
for name,m in models.items():
    ws=[];tt=0
    for L in LINES:
        a=to16(v.render(L,use_cache=False))
        t=time.time(); segs,_=m.transcribe(a,language='en',beam_size=1); txt=' '.join(s.text for s in segs); tt+=time.time()-t
        ws.append(wer(L,txt))
    print(f'HEARING {name}: WER={np.mean(ws)*100:.1f}%  time/command={tt/len(LINES):.2f}s')
