"""Wake word + Whisper second check, on the same synthetic speakers."""
import sys, time; sys.path.insert(0,'.')
import numpy as np
from scipy import signal
from kokoro_onnx import Kokoro
from jarvis_core.config import Config
from jarvis_core.ears import WakeWord, Hearing

cfg=Config(); k=Kokoro(str(cfg.voice.model),str(cfg.voice.voices)); H=Hearing(cfg)
V=np.load(cfg.voice.voices)
voices=[n for n in V.files if n[:2] in ('am','af','bm','bf')][:40:2]
POS=["Hey Jarvis.","Hey, Jarvis!"]
NEG=["Hey Travis.","Hey there.","What time is it?","Hey Siri.","Harvest the garden.","Hey, are you serious?","Okay, it is done.","Hey Marvin."]
times=[]
def run(text,voice,speed=1.0):
    a,_=k.create(text,voice=voice,speed=speed,lang='en-us' if voice[0]=='a' else 'en-gb')
    a=signal.resample_poly(a,2,3); pad=np.zeros(16000)
    x=np.concatenate([pad,a,pad,pad])+np.random.default_rng(0).standard_normal(len(a)+48000)*0.003
    x=(np.clip(x,-1,1)*32767).astype(np.int16)
    w=WakeWord(cfg); ring=[]
    for i in range(0,len(x)-1280,1280):
        f=x[i:i+1280]; ring=(ring+[f])[-25:]
        if w.hear(f):
            t=time.time(); ok=H.heard_name(np.concatenate(ring).astype(np.float32)/32768); times.append(time.time()-t)
            return ok
    return False
hits=tot=fa=nt=0
for v in voices:
    for sp in (0.9,1.1):
        for p in POS:
            f=run(p,v,sp); tot+=1; hits+=f
            if not f: print('  missed',v,sp,p)
    for n in NEG:
        f=run(n,v); nt+=1; fa+=f
        if f: print('  FALSE WAKE',v,n)
print(f'Woke {hits}/{tot} ({hits/tot*100:.0f}%), false wakes {fa}/{nt}, second check takes {np.mean(times):.2f}s avg / {np.max(times):.2f}s max')
