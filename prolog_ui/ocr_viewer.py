"""Offline image viewer with normalized crop gestures and linked text regions."""

import base64
import streamlit as st

_HTML = """<div class="ov-root"><p class="ov-help"></p><div class="ov-surface"><canvas tabindex="0" aria-label="原本画像と読み取り範囲"></canvas></div><div class="ov-tools"><button class="ov-reset" type="button">ページ全体</button><button class="ov-apply" type="button">選択範囲を適用</button><span class="ov-status" role="status"></span></div></div>"""
_CSS = """.ov-root{font:14px sans-serif;color:var(--st-text-color,#142238)}.ov-help{line-height:1.5;margin:4px 0 10px}.ov-surface{border:1px solid #cbd7e5;border-radius:8px;overflow:hidden;background:#fff}.ov-surface canvas{display:block;width:auto;height:auto;max-width:100%;max-height:70vh;margin:auto;touch-action:none;cursor:crosshair}.ov-tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}.ov-tools button{padding:9px 12px;border:1px solid #a8bdd1;border-radius:7px;background:#fff;color:#142238;font:inherit;cursor:pointer}.ov-apply{background:#075d94!important;color:#fff!important}.ov-status{font-size:12px}"""
_JS = r"""
export default function(component){
 const {parentElement,data,setTriggerValue}=component;
 const root=parentElement.querySelector('.ov-root'),canvas=root.querySelector('canvas'),ctx=canvas.getContext('2d');
 let crop=(data.crop||[0,0,1,1]).slice(),start=null,disposed=false;
 const image=new Image();
 root.querySelector('.ov-help').textContent=data.mode==='crop'?'画像をドラッグして読み取り範囲を指定し、「選択範囲を適用」を押してください。':'文章の領域を選ぶと、右の編集対象へ反映します。黄色は認識スコアが低い箇所の目印です。';
 root.querySelector('.ov-tools').style.display=data.mode==='crop'?'flex':'none';
 root.querySelectorAll('button').forEach(button=>button.disabled=Boolean(data.disabled));
 function draw(){
  if(!image.naturalWidth||disposed)return;
  canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;
  ctx.drawImage(image,0,0);const w=canvas.width,h=canvas.height;
  if(data.mode==='crop'){
   const [x1,y1,x2,y2]=crop;ctx.fillStyle='rgba(13,35,56,.38)';ctx.fillRect(0,0,w,h);
   ctx.save();ctx.beginPath();ctx.rect(x1*w,y1*h,(x2-x1)*w,(y2-y1)*h);ctx.clip();ctx.drawImage(image,0,0);ctx.restore();
   ctx.strokeStyle='#00a8df';ctx.lineWidth=Math.max(2,w/400);ctx.strokeRect(x1*w,y1*h,(x2-x1)*w,(y2-y1)*h);
   root.querySelector('.ov-status').textContent=`左 ${Math.round(x1*100)}%・上 ${Math.round(y1*100)}%・右 ${Math.round(x2*100)}%・下 ${Math.round(y2*100)}%`;
  }else{
   for(const block of data.blocks||[]){const selected=block.id===data.selected,low=block.score!==null&&block.score<.8;
    ctx.beginPath();block.box.forEach(([x,y],i)=>i?ctx.lineTo(x*w,y*h):ctx.moveTo(x*w,y*h));ctx.closePath();
    ctx.strokeStyle=selected?'#009ed7':low?'#d38a00':'#6997b2';ctx.lineWidth=selected?Math.max(4,w/250):Math.max(1,w/800);
    ctx.fillStyle=selected?'rgba(0,169,222,.20)':low?'rgba(255,190,40,.15)':'rgba(50,100,130,.025)';ctx.fill();ctx.stroke();
   }
  }
 }
 function point(event){const r=canvas.getBoundingClientRect();return [Math.min(1,Math.max(0,(event.clientX-r.left)/r.width)),Math.min(1,Math.max(0,(event.clientY-r.top)/r.height))];}
 function emit(value){setTriggerValue('selection',{...value,event_id:crypto.randomUUID(),token:data.token});}
 canvas.addEventListener('pointerdown',event=>{
  if(data.disabled)return;
  const p=point(event);if(data.mode==='crop'){start=p;canvas.setPointerCapture(event.pointerId);return;}
  const hit=(data.blocks||[]).find(block=>{const xs=block.box.map(p=>p[0]),ys=block.box.map(p=>p[1]);return p[0]>=Math.min(...xs)&&p[0]<=Math.max(...xs)&&p[1]>=Math.min(...ys)&&p[1]<=Math.max(...ys);});
  if(hit)emit({block_id:hit.id});
 });
 canvas.addEventListener('pointermove',event=>{if(!start)return;const p=point(event);crop=[Math.min(start[0],p[0]),Math.min(start[1],p[1]),Math.max(start[0],p[0]),Math.max(start[1],p[1])];draw();});
 canvas.addEventListener('pointerup',()=>{start=null;});
 canvas.addEventListener('pointercancel',()=>{start=null;crop=(data.crop||[0,0,1,1]).slice();draw();});
 root.querySelector('.ov-reset').onclick=()=>{crop=[0,0,1,1];draw();};
 root.querySelector('.ov-apply').onclick=()=>{if(data.disabled)return;if(crop[2]-crop[0]<.01||crop[3]-crop[1]<.01){root.querySelector('.ov-status').textContent='縦横1%以上の範囲を指定してください。';return;}emit({crop});};
 image.onload=draw;image.src=data.image;
 return ()=>{disposed=true;image.onload=null;};
}
"""


def render_ocr_viewer(
    png,
    *,
    key,
    token,
    crop=None,
    blocks=None,
    selected=None,
    mode="review",
    disabled=False,
):
    component = st.components.v2.component(
        "proloag_ocr_viewer", html=_HTML, css=_CSS, js=_JS
    )
    result = component(
        key=key,
        data={
            "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            "mode": mode,
            "token": token,
            "crop": crop or [0, 0, 1, 1],
            "blocks": blocks or [],
            "selected": selected,
            "disabled": bool(disabled),
        },
        on_selection_change=lambda: None,
        width="stretch",
    )
    selection = result.get("selection")
    return (
        selection
        if isinstance(selection, dict) and selection.get("token") == token
        else None
    )
