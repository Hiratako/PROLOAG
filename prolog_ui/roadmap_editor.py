"""Offline v2 schedule editor; pointer moves never contact Python."""

from datetime import date
import streamlit as st

_HTML = """
<div class="rm-editor">
  <div class="rm-tools" role="group" aria-label="日程の操作方法">
    <button type="button" data-tool="auto">移動・端を伸縮</button>
    <button type="button" data-tool="move">工程を移動</button>
    <button type="button" data-tool="start">開始日を調整</button>
    <button type="button" data-tool="end">終了日を調整</button>
  </div>
  <div class="rm-canvas"></div>
  <div class="rm-status" role="status" aria-live="polite"></div>
</div>
"""

_CSS = """
.rm-editor{font:var(--st-font-size,14px) var(--st-font,sans-serif);color:var(--rm-ink);background:var(--rm-card);padding:12px;border:1px solid var(--rm-line);border-radius:10px;box-sizing:border-box;max-width:100%}
.rm-tools{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.rm-tools button{font:inherit;background:var(--rm-card);color:var(--rm-ink);border:1px solid var(--rm-line);border-radius:6px;padding:7px 12px;min-height:40px;cursor:pointer}
.rm-tools button[aria-pressed=true]{background:var(--rm-primary);color:var(--rm-primary-text)}
.rm-tools button:disabled{opacity:.6;cursor:default}
.rm-tools button:focus-visible{outline:2px solid var(--rm-primary);outline-offset:2px}
.rm-canvas{width:100%;min-width:0}
.rm-canvas svg{display:block;width:100%;overflow:hidden;touch-action:pan-y}
.rm-canvas text{fill:var(--rm-ink);font:12px var(--st-font,sans-serif)}
.rm-canvas .rm-grid{stroke:var(--rm-line);stroke-width:1}
.rm-canvas .rm-edge{stroke:var(--rm-muted);stroke-width:1.5;fill:none;pointer-events:none}
.rm-canvas .rm-bar{cursor:grab;touch-action:none}
.rm-canvas .rm-bar:focus-visible{outline:none}
.rm-canvas .rm-bar:focus-visible .rm-span{stroke:var(--rm-ink);stroke-width:2}
.rm-canvas .rm-grip{cursor:col-resize;touch-action:none}
.rm-canvas .rm-locked{cursor:default;opacity:.6}
.rm-status{margin-top:8px;font-size:13px;line-height:1.55;min-height:22px;overflow-wrap:anywhere}
@media(pointer:coarse){.rm-tools button{min-height:44px}.rm-canvas .rm-grip{stroke:transparent;stroke-width:10}}
"""

_JS = r"""
const instances = new Map();
const NS='http://www.w3.org/2000/svg', DAY=86400000;
const numberDay = value => {const p=value.split('-').map(Number);return Date.UTC(p[0],p[1]-1,p[2])/DAY;};
const iso = value => new Date(value*DAY).toISOString().slice(0,10);
const MIN=numberDay('1900-01-01'), MAX=numberDay('2199-12-31');

export default function(component){
 const {parentElement,key,data}=component, doc=parentElement.ownerDocument, win=doc.defaultView;
 let state=instances.get(key);
 if(!state||state.disposed){state={tool:'auto',width:0,drag:null,request:null,clientPending:false,disposed:false};instances.set(key,state);}
 state.component=component;state.data=data;
 const root=parentElement.querySelector('.rm-editor'),canvas=root.querySelector('.rm-canvas'),status=root.querySelector('.rm-status');
 const palette=data.palette||{};
 const defaults={ink:'#152238',card:'#ffffff',line:'#d9e2ec',primary:'#003b71',muted:'#667085',cyan:'#1f6f9f',red:'#b54747',purple:'#7357a8'};
 Object.entries(defaults).forEach(([name,fallback])=>root.style.setProperty('--rm-'+name,/^#[0-9a-f]{6}$/i.test(palette[name]||'')?palette[name]:fallback));
 root.style.setProperty('--rm-primary-text',palette.mode==='dark'?'#06111f':'#ffffff');
 if(state.request && data.ack_event_id===state.request.event_id)state.clientPending=false;
 const disabled=()=>Boolean(data.pending||state.clientPending);
 const svgNode=(tag,attrs={},text)=>{const node=doc.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>node.setAttribute(k,String(v)));if(text!==undefined)node.textContent=text;return node;};
 let svg,edgeLayer,records,geometry;
 function say(message){status.textContent=message;}
 function updateTools(){root.querySelectorAll('[data-tool]').forEach(button=>{button.disabled=disabled();button.setAttribute('aria-pressed',String(button.dataset.tool===state.tool));});}
 root.querySelectorAll('[data-tool]').forEach(button=>button.onclick=()=>{state.tool=button.dataset.tool;updateTools();});
 function dates(item,delta,mode){
   let a=numberDay(item.start_date),b=numberDay(item.end_date);
   if(mode==='move'){delta=Math.max(MIN-a,Math.min(MAX-b,delta));a+=delta;b+=delta;}
   else if(mode==='start')a=Math.max(MIN,Math.min(b,a+delta));
   else b=Math.min(MAX,Math.max(a,b+delta));
   return {start_date:iso(a),end_date:iso(b)};
 }
 function position(record,change){
   const value=change||record.item;
   const x=geometry.x(numberDay(value.start_date)),end=geometry.x(numberDay(value.end_date)+1),width=Math.max(2,end-x);
   record.span.setAttribute('x',x);record.span.setAttribute('width',width);
   record.fill.setAttribute('x',x);record.fill.setAttribute('width',width*record.progress);
   record.left.setAttribute('x',x-4);record.right.setAttribute('x',end-4);
   record.text.setAttribute('x',Math.max(geometry.left+18,Math.min(geometry.width-30,x+width/2)));
   record.text.style.display=width<38?'none':'';
   record.current=value;
 }
 function drawEdges(){
   edgeLayer.replaceChildren();
   for(const record of records.values())for(const parentId of record.item.predecessor_ids||[]){
     const parent=records.get(parentId);if(!parent)continue;
     const a=geometry.x(numberDay(parent.current.end_date)+1),b=geometry.x(numberDay(record.current.start_date));
     const y1=parent.y+15,y2=record.y+15,mid=(a+b)/2;
     edgeLayer.append(svgNode('path',{d:`M${a},${y1} L${mid},${y1} L${mid},${y2} L${b},${y2}`,class:'rm-edge','marker-end':`url(#${geometry.marker})`}));
   }
 }
 function emit(item,change){
   if(change.start_date===item.start_date&&change.end_date===item.end_date){say('日程は変更されていません。');return;}
   const event_id=win.crypto.randomUUID().replaceAll('-','');
   const request={event_id,item_id:item.id,old_start:item.start_date,old_end:item.end_date,...change,source_revision:data.source_revision};
   state.request=request;state.clientPending=true;updateTools();
   say(`${item.title}：${change.start_date} 〜 ${change.end_date}。変更内容を確認しています。`);
   component.setTriggerValue('schedule_change',request);
 }
 function finish(event,cancel=false){
   const drag=state.drag;if(!drag)return;
   state.drag=null;
   try{if(drag.node.hasPointerCapture(drag.pointerId))drag.node.releasePointerCapture(drag.pointerId);}catch{}
   if(cancel){position(drag.record);drawEdges();say('移動を取り消しました。');return;}
   emit(drag.record.item,drag.change||{start_date:drag.record.item.start_date,end_date:drag.record.item.end_date});
 }
 function begin(event,record){
   if(disabled()||record.item.is_archived||record.item.status==='done'||event.button!==0)return;
   event.preventDefault();event.stopPropagation();
   const mode=state.tool==='auto'?(event.target.dataset.mode||'move'):state.tool;
   state.drag={record,node:record.group,pointerId:event.pointerId,startX:event.clientX,mode,change:null};
   record.group.setPointerCapture(event.pointerId);record.group.focus();
   say(`${record.item.title}：左右へ動かしてください。離すと変更案を確認できます。`);
 }
 function move(event){
   const drag=state.drag;if(!drag||event.pointerId!==drag.pointerId)return;
   event.preventDefault();
   const delta=Math.round((event.clientX-drag.startX)/geometry.dayWidth);
   drag.change=dates(drag.record.item,delta,drag.mode);position(drag.record,drag.change);drawEdges();
   say(`${drag.record.item.title}：${drag.change.start_date} 〜 ${drag.change.end_date}`);
 }
 function draw(){
   if(state.drag)return;
   const width=Math.max(260,canvas.getBoundingClientRect().width),mobile=width<560,left=mobile?8:Math.min(210,Math.max(135,width*.25)),right=16;
   const start=numberDay(data.start_date),end=numberDay(data.end_date)+1,dayWidth=(width-left-right)/(end-start),rowHeight=mobile?78:58,top=(data.milestones||[]).length?72:36;
   const items=data.items||[],height=top+items.length*rowHeight+22;
   geometry={width,left,dayWidth,x:day=>left+(day-start)*dayWidth,marker:'rm-arrow-'+String(key).replace(/[^a-z0-9]/gi,'')};
   svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,height,role:'group','aria-label':'ドラッグで学習工程の日程を変更。移動は保存前に確認します。'});
   const defs=svgNode('defs'),marker=svgNode('marker',{id:geometry.marker,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});
   marker.append(svgNode('path',{d:'M0 0 L10 5 L0 10 z',fill:palette.muted||defaults.muted}));defs.append(marker);
   const clipId=geometry.marker+'-clip',clip=svgNode('clipPath',{id:clipId});clip.append(svgNode('rect',{x:left,y:top-6,width:width-left-right,height:height-top+6}));defs.append(clip);svg.append(defs);
   const plot=svgNode('g',{'clip-path':`url(#${clipId})`});
   for(const milestone of data.milestones||[]){
     const day=numberDay(milestone.date);if(day<start||day>=end)continue;
     const x=geometry.x(day),y=45,node=svgNode('path',{d:`M${x} ${y-7} L${x+7} ${y} L${x} ${y+7} L${x-7} ${y} Z`,fill:palette.purple||defaults.purple});
     node.append(svgNode('title',{},milestone.title+' '+milestone.date));svg.append(node);
   }
   const ticks=width<420?3:5;
   for(let index=0;index<ticks;index++){
     const day=Math.round(start+(end-start-1)*index/(ticks-1)),x=geometry.x(day);
     svg.append(svgNode('text',{x,y:18,'text-anchor':index===0?'start':index===ticks-1?'end':'middle'},iso(day).slice(5).replace('-','/')));
     plot.append(svgNode('line',{x1:x,x2:x,y1:top-5,y2:height-10,class:'rm-grid'}));
   }
   const today=numberDay(data.today);
   if(today>=start&&today<end){plot.append(svgNode('line',{x1:geometry.x(today),x2:geometry.x(today),y1:top-5,y2:height-10,stroke:palette.red||defaults.red,'stroke-dasharray':'4 4'}));}
   edgeLayer=svgNode('g');plot.append(edgeLayer);records=new Map();
   items.forEach((item,index)=>{
     const y=top+index*rowHeight+(mobile?27:8),locked=item.is_archived||item.status==='done';
     const label=svgNode('text',{x:mobile?8:left-10,y:mobile?y-8:y+19,'text-anchor':mobile?'start':'end'},item.title.length>(mobile?36:15)?item.title.slice(0,mobile?35:14)+'…':item.title);
     label.append(svgNode('title',{},item.title));svg.append(label);
     const group=svgNode('g',{class:'rm-bar'+(locked?' rm-locked':''),tabindex:locked?-1:0,role:'button','aria-label':`${item.title}、${item.start_date}から${item.end_date}。矢印キーで日程変更`,'aria-disabled':String(locked||disabled()),'data-item-id':item.id});
     if(data.show_baseline&&(item.start_date!==item.baseline_start_date||item.end_date!==item.baseline_end_date)){
       plot.append(svgNode('line',{x1:geometry.x(numberDay(item.baseline_start_date)),x2:geometry.x(numberDay(item.baseline_end_date)+1),y1:y+38,y2:y+38,stroke:palette.muted||defaults.muted,'stroke-width':2,'stroke-dasharray':'3 3'}));
     }
     const span=svgNode('rect',{y,width:2,height:32,rx:5,fill:palette.cyan||defaults.cyan,opacity:.22,class:'rm-span'});
     const fill=svgNode('rect',{y,width:2,height:32,rx:5,fill:palette.cyan||defaults.cyan,opacity:.65,'pointer-events':'none'});
     const text=svgNode('text',{y:y+21,'text-anchor':'middle','pointer-events':'none'},Math.round(item.completed/item.total*100)+'%');
     const leftHandle=svgNode('rect',{y:y+2,width:8,height:28,rx:2,fill:palette.ink||defaults.ink,opacity:locked?0:.5,class:'rm-grip','data-mode':'start'});
     const rightHandle=svgNode('rect',{y:y+2,width:8,height:28,rx:2,fill:palette.ink||defaults.ink,opacity:locked?0:.5,class:'rm-grip','data-mode':'end'});
     group.append(span,fill,text,leftHandle,rightHandle);
     const record={item,group,span,fill,text,left:leftHandle,right:rightHandle,y,progress:item.completed/item.total,current:item};records.set(item.id,record);position(record);
     group.addEventListener('pointerdown',event=>begin(event,record));group.addEventListener('pointermove',move);
     group.addEventListener('pointerup',event=>finish(event));group.addEventListener('pointercancel',event=>finish(event,true));
     group.addEventListener('lostpointercapture',event=>{if(state.drag)finish(event,true);});
     group.addEventListener('keydown',event=>{
       if(event.key==='Escape'){finish(event,true);return;}
       if(locked||disabled()||event.repeat||!['ArrowLeft','ArrowRight'].includes(event.key))return;
       event.preventDefault();const delta=(event.key==='ArrowRight'?1:-1)*(event.shiftKey?7:1);
       const change=dates(item,delta,state.tool==='auto'?'move':state.tool);position(record,change);drawEdges();emit(item,change);
     });
     plot.append(group);
     if(item.deadline){const x=geometry.x(numberDay(item.deadline)),cy=y+16;const deadline=svgNode('path',{d:`M${x} ${cy-6} L${x+6} ${cy} L${x} ${cy+6} L${x-6} ${cy} Z`,fill:'none',stroke:palette.red||defaults.red,'pointer-events':'none'});deadline.append(svgNode('title',{},'確定期限 '+item.deadline));plot.append(deadline);}
   });
   drawEdges();svg.append(plot);canvas.replaceChildren(svg);state.width=width;
 }
 if(state.observer)state.observer.disconnect();
 state.observer=new win.ResizeObserver(()=>{if(Math.abs(canvas.getBoundingClientRect().width-state.width)>1)draw();});state.observer.observe(canvas);
 updateTools();draw();
 say(data.pending?'変更案を下で確認し、「日程変更を保存」または「取り消す」を選んでください。':state.clientPending?'変更内容を確認しています。':'棒を移動、または左右の端で期間を変更。矢印キーは1日、Shift併用で7日調整できます。');
 return ()=>{state.disposed=true;state.drag=null;if(state.observer)state.observer.disconnect();instances.delete(key);};
}
"""


def render_roadmap_editor(
    items: list,
    start: date,
    end: date,
    *,
    today: date,
    source_revision,
    palette: dict,
    key: str,
    pending=False,
    ack_event_id="",
    milestones=None,
    show_baseline=True,
) -> dict | None:
    component = st.components.v2.component(
        "proloag_roadmap_editor", html=_HTML, css=_CSS, js=_JS
    )
    fields = (
        "id",
        "title",
        "start_date",
        "end_date",
        "completed",
        "total",
        "status",
        "is_archived",
        "predecessor_ids",
        "deadline",
        "baseline_start_date",
        "baseline_end_date",
    )
    values = [
        {
            name: row.get(name, []) if name == "predecessor_ids" else row[name]
            for name in fields
        }
        for row in items
    ]
    result = component(
        key=key,
        data={
            "items": values,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "today": today.isoformat(),
            "source_revision": source_revision,
            "palette": palette,
            "pending": bool(pending),
            "ack_event_id": ack_event_id,
            "milestones": milestones or [],
            "show_baseline": bool(show_baseline),
        },
        on_schedule_change=lambda: None,
        width="stretch",
    )
    request = result.get("schedule_change")
    return request if isinstance(request, dict) else None
