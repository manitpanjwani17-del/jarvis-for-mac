"""Wake-word check with synthetic speakers: says "Hey Jarvis" in many voices, plus near-misses."""
import sys; sys.path.insert(0,'.')
import numpy as np
from scipy import signal
from kokoro_onnx import Kokoro
from jarvis_core.config import Config
from jarvis_core.ears import WakeWord

cfg=Config(); k=Kokoro(str(cfg.voice.model),str(cfg.voice.voices))
V=np.load(cfg.voice.voices)
voices=[n for n in V.files if n[:2] in ('am','af','bm','bf')][:40:2]
POS=["Hey Jarvis.","Hey, Jarvis!"]
NEG=["Hey Travis.","Hey there.","Jarvis is my friend's name.","What time is it?","Hey Siri.","Harvest the garden.","Hey, are you serious?","Okay, it is done."]
def run(text,voice,speed=1.0):
    a,sr=k.create(text,voice=voice,speed=speed,lang='en-us' if voice[0]=='a' else 'en-gb')
    a=signal.resample_poly(a,2,3)
    rng=np.random.default_rng(0)
    pad=np.zeros(16000); x=np.concatenate([pad,a,pad,pad])
    x=x+rng.standard_normal(len(x))*0.003       # light room noise
    x=(np.clip(x,-1,1)*32767).astype(np.int16)
    w=WakeWord(cfg); best=0; fired=False
    for i in range(0,len(x)-1280,1280):
        f=x[i:i+1280]
        s=float(w.model.predict(f)[w.name]); best=max(best,s)
        if w.hear(f) if False else False: pass
    w2=WakeWord(cfg)
    for i in range(0,len(x)-1280,1280):
        if w2.hear(x[i:i+1280]): fired=True
    return best,fired
hits=0;tot=0;fa=0;ntot=0
for v in voices:
    for sp in (0.9,1.1):
        for p in POS:
            b,f=run(p,v,sp); tot+=1; hits+=f
            if not f: print(f'  missed: {v} speed {sp} "{p}" best={b:.2f}')
    for n in NEG:
        b,f=run(n,v); ntot+=1; fa+=f
        if f: print(f'  FALSE WAKE: {v} "{n}" best={b:.2f}')
print(f'Woke on {hits}/{tot} "Hey Jarvis" ({hits/tot*100:.0f}%), falsely woke on {fa}/{ntot} other phrases, voices={len(voices)}')
