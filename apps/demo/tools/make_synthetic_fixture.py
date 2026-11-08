"""Create fictional vector-like frames and video. No camera or dataset inputs are read.
Regeneration requires: python -m pip install pillow av
"""
from pathlib import Path
import json
from PIL import Image, ImageDraw
import av

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'
(DATA/'synthetic').mkdir(parents=True,exist_ok=True)

def frame(t):
    im=Image.new('RGB',(480,320),'#eaf3f6');d=ImageDraw.Draw(im)
    d.rectangle((20,75,225,280),fill='#cedfe3',outline='#35576c',width=3)
    d.rectangle((255,75,460,280),fill='#e4ddd0',outline='#35576c',width=3)
    d.text((32,88),'FICTIONAL SURFACE A',fill='#173040')
    d.text((267,88),'FICTIONAL SURFACE B',fill='#173040')
    d.rounded_rectangle((312,180,396,230),radius=8,fill='#b76535')
    d.text((329,197),'WALLET',fill='white')
    if t<5:
        x,y=100,205
    elif t<10:
        x,y=115+int((t-5)*15),145
        d.ellipse((x-35,y-20,x+60,y+45),fill='#a4c9d2',outline='#176a8a',width=3)
        d.text((x-25,y+27),'HOLDING',fill='#173040')
    else:
        x=y=None
    if x is not None:
        d.ellipse((x-13,y-13,x+13,y+13),outline='#c39420',width=5)
        d.line((x+10,y+9,x+45,y+34),fill='#c39420',width=7)
        d.line((x+35,y+27,x+29,y+35),fill='#c39420',width=5)
    d.rectangle((0,0,480,56),fill='#173040')
    d.text((14,12),'SYNTHETIC FIXTURE - NO REAL RECORDING',fill='white')
    d.text((14,31),f'FICTIONAL TIME {t:04.1f}s / generated geometric shapes',fill='#c9e4ef')
    d.text((23,294),'Programmed observations test retrieval; not model predictions.',fill='#173040')
    return im

for name,t in [('keys-surface-a',3),('keys-held',7),('keys-unseen',12),('wallet-surface-b',14)]:
    frame(t).save(DATA/'synthetic'/f'{name}.png')
video=DATA/'synthetic'/'fictional_sequence.mp4'
with av.open(str(video),'w') as out:
    out.metadata.update({'title':'SYNTHETIC FIXTURE - no real recording','artist':'RoboMemo synthetic generator','comment':'Only generated geometric shapes; no audio or personal media.'})
    stream=out.add_stream('libx264',rate=5);stream.width=480;stream.height=320;stream.pix_fmt='yuv420p'
    for i in range(100):
        fr=av.VideoFrame.from_image(frame(i/5))
        for packet in stream.encode(fr):out.mux(packet)
    for packet in stream.encode():out.mux(packet)
objects=[{'id':'keys','label_zh':'钥匙','aliases':['钥匙','鑰匙','key','keys']},
         {'id':'wallet','label_zh':'钱包','aliases':['钱包','錢包','wallet']}]
clip={'id':'synthetic-sequence','label':'合成序列（虚构场景）','started_at':'2000-01-01T00:00:00Z',
      'duration_seconds':20,'video_file':'data/synthetic/fictional_sequence.mp4',
      'poster_file':'data/synthetic/keys-surface-a.png','quality_note':'完全合成的图形，非真实画面，也非模型输出。'}
def obs(id,obj,t,state,location,text,visible=True,usable=True):
    return {'id':id,'object_id':obj,'clip_id':clip['id'],'output_pts_seconds':t,'state':state,
            'location_zh':location,'observation_zh':text,'is_object_visible':visible,'use_for_last_seen':usable,
            'usable_as_clear_evidence':usable,'evidence_file':f'data/synthetic/{id}.png',
            'provenance':'synthetic_fixture','review_status':'synthetic',
            'limitations':['合成测试样例；不代表真实物品或实验效果。']}
index={'schema_version':1,'title':'RoboMemo · 合成样例','provenance':'synthetic_fixture',
       'indexed_at':'2000-01-01T00:00:00Z','objects':objects,'clips':[clip],
       'limitations':['全部视频、图片和观察均为程序生成的虚构示例。','仅用于检验检索、时间排序和不确定性显示。'],
       'observations':[
          obs('keys-surface-a','keys',3,'placed','测试台 A 上','合成图形：钥匙位于虚构测试台 A。'),
          obs('keys-held','keys',7,'held','虚构持有区域','合成图形：钥匙处于持有状态；后续落点未展示。'),
          obs('keys-unseen','keys',12,'uncertain','去向未知','合成图形：钥匙已经离开图示，不能补全去向。',False,False),
          obs('wallet-surface-b','wallet',14,'visible','测试台 B 上','合成图形：钱包位于虚构测试台 B。'),
       ]}
(DATA/'index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('SYNTHETIC_DATA_GENERATED',len(index['observations']))
