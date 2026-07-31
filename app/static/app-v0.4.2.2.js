
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const icon=(name)=>{
 const map={
  check:'<svg viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>',
  box:'<svg viewBox="0 0 24 24"><path d="m21 8-9 5-9-5 9-5 9 5Z"/><path d="m3 8 9 5 9-5v8l-9 5-9-5V8Z"/></svg>',
  server:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/></svg>'
 };return map[name]||map.box;
};
function toast(message,type='success'){
 const wrap=$('#toast-wrap'),el=document.createElement('div');
 el.className=`toast ${type}`;el.textContent=message;wrap.appendChild(el);setTimeout(()=>el.remove(),4500);
}
function formatRate(value){let v=Number(value||0),u='o/s';if(v>=1073741824){v/=1073741824;u='Go/s'}else if(v>=1048576){v/=1048576;u='Mo/s'}else if(v>=1024){v/=1024;u='Ko/s'}return `${v.toFixed(1)} ${u}`}
function setNodeField(field,value){$$(`[data-node-field="${field}"]`).forEach(el=>el.textContent=value)}
function setMeter(field,value){$$(`[data-meter="${field}"]`).forEach(el=>el.style.width=`${Math.max(0,Math.min(100,Number(value||0)))}%`)}
async function pollNode(id){
 try{
  const r=await fetch(`/api/nodes/${id}/status`,{cache:'no-store'});if(!r.ok)return;
  const p=await r.json(),m=p.metrics||{};
  setNodeField('cpu',`${Number(m.cpu_percent||0).toFixed(1)} %`);setMeter('cpu',m.cpu_percent);
  setNodeField('ram',`${Number(m.ram_percent||0).toFixed(1)} %`);setMeter('ram',m.ram_percent);
  setNodeField('disk',`${Number(m.disk_percent||0).toFixed(1)} %`);setMeter('disk',m.disk_percent);
  setNodeField('read',formatRate(m.disk_read_bps));setNodeField('write',formatRate(m.disk_write_bps));
  setNodeField('rx',formatRate(m.net_rx_bps));setNodeField('tx',formatRate(m.net_tx_bps));
  setNodeField('containers',`${m.running_containers||0} / ${m.docker_containers||0}`);
  setNodeField('queue',`${p.running_jobs||0} en cours · ${p.queued_jobs||0} en attente`);
 }catch(e){console.warn(e)}
 setTimeout(()=>pollNode(id),5000);
}
function openClaim(id){$(`#claim-${id}`)?.showModal()} function closeClaim(id){$(`#claim-${id}`)?.close()}
function jobLabel(s){return({queued:'En attente',running:'En cours',success:'Terminé',error:'Échec'})[s]||s}
function jobClass(s){return s==='success'?'ok':s==='error'?'bad':s==='running'||s==='queued'?'warn':''}
function deploySteps(action){
 const common=['Validation du node','Validation des ports','Préparation du Compose'];
 if(action==='stop')return ['Validation de l’AppBox','Arrêt des services','Vérification finale'];
 if(action==='recreate')return [...common,'Pull des images','Recréation Docker','Vérification des services'];
 if(action==='delete')return ['Validation de l’AppBox','Arrêt des services','Suppression des conteneurs','Nettoyage des fichiers','Retrait de l’inventaire'];
 return [...common,'Création des conteneurs','Démarrage des services','Vérification HTTP'];
}
function openJobModal(clientId,action,title){
 const dlg=$('#job-progress-modal'),steps=deploySteps(action);
 dlg.dataset.client=clientId;dlg.dataset.action=action;
 $('#job-modal-title').textContent=title;$('#job-modal-client').textContent=clientId.toUpperCase();const build=$('#job-ui-build');if(build)build.textContent='UI 0.4.2.2';
 $('#job-progress-bar').style.width='0%';$('#job-progress-value').textContent='0 %';
 $('#job-current').textContent='Placement dans la file globale…';$('#job-log').textContent='Initialisation de l’opération…';
 $('#job-steps').innerHTML=steps.map((s,i)=>`<div class="deploy-step" data-step="${i}"><span class="step-bullet">${i+1}</span><span>${s}</span><small>EN ATTENTE</small></div>`).join('');
 $('#job-modal-close').disabled=false;$('#job-modal-close').dataset.redirect='';dlg.showModal();
}
function renderJobModal(job){
 const progress=Math.max(0,Math.min(100,Number(job.progress||0)));
 const finished=job.status==='success'||job.status==='error';
 $('#job-progress-bar').style.width=`${progress}%`;
 $('#job-progress-value').textContent=`${progress} %`;
 $('#job-current').textContent=job.title||'Opération';
 $('#job-log').textContent=job.detail||'En attente des logs…';

 const steps=$$('.deploy-step','#job-steps');
 const completedCount=(job.status==='success'||progress>=100)
   ? steps.length
   : Math.min(steps.length,Math.floor((progress/100)*steps.length));
 const activeIndex=job.status==='running'
   ? Math.min(steps.length-1,completedCount)
   : -1;

 steps.forEach((el,i)=>{
   const isDone=i<completedCount || job.status==='success' || progress>=100;
   const isActive=i===activeIndex && !isDone;
   el.classList.toggle('done',isDone);
   el.classList.toggle('active',isActive);

   const label=$('small',el);
   const bullet=$('.step-bullet',el);
   if(isDone){
     label.textContent='TERMINÉ';
     bullet.textContent='✓';
   }else if(isActive){
     label.textContent='EN COURS';
     bullet.innerHTML='<span class="spin"></span>';
   }else if(job.status==='error' && i===completedCount){
     label.textContent='ÉCHEC';
     bullet.textContent='!';
   }else{
     label.textContent='EN ATTENTE';
     bullet.textContent=String(i+1);
   }
 });

 if(job.status==='success'){
   $('#job-current').textContent='Opération terminée avec succès';
   $('#job-modal-close').disabled=false;
   $('#job-modal-close').textContent='Fermer';
   toast(`${job.client_id.toUpperCase()} : Terminé`,'success');
 }else if(job.status==='error'){
   $('#job-current').textContent='Échec de l’opération';
   $('#job-modal-close').disabled=false;
   $('#job-modal-close').textContent='Fermer';
   toast(`${job.client_id.toUpperCase()} : Échec`,'error');
 }else{
   $('#job-modal-close').disabled=false;
 }
}
async function watchJob(jobId){
 try{
  const r=await fetch(`/api/jobs/${jobId}`,{cache:'no-store'});if(!r.ok)return;
  const job=await r.json();renderJobModal(job);
  if(job.status==='queued'||job.status==='running'){setTimeout(()=>watchJob(jobId),1000)}
  else if(job.action==='delete'&&job.status==='success'){
   $('#job-modal-close').dataset.redirect='/appboxes';
  }
 }catch(e){setTimeout(()=>watchJob(jobId),1800)}
}
document.addEventListener('submit',async e=>{
 const form=e.target;if(!form.matches('[data-job-form]'))return;
 e.preventDefault();const client=form.dataset.client,action=form.dataset.action,title=form.dataset.title;
 if(action==='delete'&&!confirm(`Supprimer définitivement ${client.toUpperCase()} et ses conteneurs ?`))return;
 openJobModal(client,action,title);
 try{
  const r=await fetch(form.action,{method:'POST',body:new FormData(form),headers:{'X-Requested-With':'XMLHttpRequest'}});
  if(!r.ok)throw new Error(await r.text());
  const payload=await r.json();setTimeout(()=>watchJob(payload.job_id),350);
 }catch(err){$('#job-current').textContent='Impossible de lancer l’opération';$('#job-log').textContent=String(err);$('#job-modal-close').disabled=false;toast('Erreur lors du lancement','error')}
});
document.addEventListener('click',e=>{
 const claim=e.target.closest('[data-open-claim]');if(claim)openClaim(claim.dataset.openClaim);
 if(e.target.closest('[data-mobile-toggle]'))$('.sidebar')?.classList.toggle('open');
});

function chartColor(alpha=1){return `rgba(255,38,53,${alpha})`}
function drawChart(canvas,values){
 const ctx=canvas.getContext('2d'),dpr=window.devicePixelRatio||1;
 const rect=canvas.getBoundingClientRect();canvas.width=Math.max(1,rect.width*dpr);canvas.height=Math.max(1,rect.height*dpr);ctx.scale(dpr,dpr);
 const w=rect.width,h=rect.height,p=5;ctx.clearRect(0,0,w,h);
 const data=values.length?values:[0,0],max=Math.max(...data,1),min=Math.min(...data,0),range=Math.max(1,max-min);
 const pts=data.map((v,i)=>[p+(i/(Math.max(1,data.length-1)))*(w-p*2),h-p-((v-min)/range)*(h-p*2)]);
 const grad=ctx.createLinearGradient(0,0,0,h);grad.addColorStop(0,chartColor(.28));grad.addColorStop(1,chartColor(0));
 ctx.beginPath();ctx.moveTo(pts[0][0],h-p);pts.forEach(pt=>ctx.lineTo(pt[0],pt[1]));ctx.lineTo(pts[pts.length-1][0],h-p);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
 ctx.beginPath();pts.forEach((pt,i)=>i?ctx.lineTo(pt[0],pt[1]):ctx.moveTo(pt[0],pt[1]));ctx.strokeStyle=chartColor(1);ctx.lineWidth=2;ctx.stroke();
}
async function loadNodeCharts(nodeId){
 try{
  const r=await fetch(`/api/nodes/${nodeId}/metrics?hours=1`,{cache:'no-store'});if(!r.ok)return;
  const p=await r.json(),rows=p.metrics||[];
  const series={
   cpu:rows.map(x=>Number(x.cpu_percent||0)),
   ram:rows.map(x=>Number(x.ram_percent||0)),
   disk:rows.map(x=>Number(x.disk_percent||0)),
   network:rows.map(x=>Number(x.net_rx_bps||0)+Number(x.net_tx_bps||0)),
   io:rows.map(x=>Number(x.disk_read_bps||0)+Number(x.disk_write_bps||0))
  };
  $$(`canvas[data-node-chart][data-node="${nodeId}"]`).forEach(c=>drawChart(c,series[c.dataset.nodeChart]||[]));
 }catch(e){console.warn('charts',e)}
 setTimeout(()=>loadNodeCharts(nodeId),10000);
}

document.addEventListener('DOMContentLoaded',()=>{
 $$('[data-node-poll]').forEach(el=>pollNode(el.dataset.nodePoll));[...new Set($$('[data-node-chart]').map(c=>c.dataset.node))].forEach(loadNodeCharts);
 $('#job-modal-close')?.addEventListener('click',e=>{const target=e.currentTarget.dataset.redirect;$('#job-progress-modal').close();if(target)location.href=target});
 $$('[data-close-dialog]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
 const params=new URLSearchParams(location.search);if(params.get('job')){const client=location.pathname.split('/').pop();openJobModal(client,'deploy','Opération AppBox');watchJob(params.get('job'))}
});
