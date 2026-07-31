
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
 if(action==='restart')return ['Validation du node','Redémarrage Docker','Vérification des services'];
 if(action==='recreate')return [...common,'Pull des images','Recréation Docker','Vérification des services'];
 if(action==='delete')return ['Validation et protection de l’AppBox','Arrêt et suppression Docker','Traitement des données persistantes','Mise à jour de l’inventaire','Journal d’audit'];
 return [...common,'Création des conteneurs','Démarrage des services','Vérification HTTP'];
}
function openJobModal(clientId,action,title){
 const dlg=$('#job-progress-modal');
 dlg.dataset.client=clientId;dlg.dataset.action=action;
 $('#job-modal-title').textContent=title;$('#job-modal-client').textContent=clientId.toUpperCase();
 const build=$('#job-ui-build');if(build)build.textContent='UI 1.3.0';
 $('#job-progress-bar').style.width='0%';$('#job-progress-value').textContent='0 %';
 $('#job-current').textContent='Placement dans la file globale…';
 $('#job-log').textContent='Initialisation du workflow persistant…';
 $('#job-steps').innerHTML='<div class="deploy-step active"><span class="step-bullet"><span class="spin"></span></span><span>Chargement des étapes backend</span><small>EN COURS</small></div>';
 $('#job-modal-close').disabled=false;$('#job-modal-close').dataset.redirect='';
 $('#job-progress-modal').dataset.notified='';dlg.dataset.refreshScheduled='';$('#job-modal-footer-close').hidden=true;const details=$('#job-details');if(details)details.open=false;dlg.showModal();
}
function stepLabel(status){
 return ({pending:'EN ATTENTE',running:'EN COURS',success:'TERMINÉ',warning:'AVERTISSEMENT',failed:'ÉCHEC',skipped:'IGNORÉ'})[status]||String(status||'').toUpperCase();
}
function renderJobModal(job){
 const progress=Math.max(0,Math.min(100,Number(job.progress||0)));
 $('#job-progress-bar').style.width=`${progress}%`;
 $('#job-progress-value').textContent=`${progress} %`;
 $('#job-current').textContent=job.status==='success'?'Workflow terminé avec succès':job.status==='error'?'Workflow en échec':job.title||'Workflow';
 $('#job-log').textContent=job.detail||'En attente des logs…';

 const steps=job.steps||[];
 $('#job-steps').innerHTML=steps.length?steps.map((step,i)=>{
   const cls=step.status==='success'?'done':step.status==='running'?'active':step.status==='failed'?'failed':step.status==='warning'?'warning':step.status==='skipped'?'skipped':'';
   const bullet=step.status==='success'?'✓':step.status==='failed'?'!':step.status==='skipped'?'—':step.status==='running'?'<span class="spin"></span>':String(i+1);
   const duration=step.duration_seconds==null?'':` · ${Number(step.duration_seconds).toFixed(2)} s`;
   return `<div class="deploy-step ${cls}">
     <span class="step-bullet">${bullet}</span>
     <div><span>${escapeHtml(step.title)}</span></div>
     <small>${stepLabel(step.status)}${duration}</small>
   </div>`;
 }).join(''):'<div class="empty">Aucune étape enregistrée.</div>';

 const footerClose=$('#job-modal-footer-close');
 const finished=job.status==='success'||job.status==='error';
 footerClose.hidden=!finished;
 $('#job-modal-close').disabled=false;
 if(finished&&!$('#job-progress-modal').dataset.notified){
   $('#job-progress-modal').dataset.notified='1';
   toast(`${job.client_id.toUpperCase()} : ${job.status==='success'?'Terminé':'Échec'}`,job.status==='success'?'success':'error');
 }
}
function escapeHtml(value){
 return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}
async function watchJob(jobId){
 try{
  const r=await fetch(`/api/jobs/${jobId}`,{cache:'no-store'});if(!r.ok)return;
  const job=await r.json();renderJobModal(job);
  if(job.status==='queued'||job.status==='running'){setTimeout(()=>watchJob(jobId),1000)}
  else if(job.action==='delete'&&job.status==='success'){
   $('#job-modal-close').dataset.redirect='/appboxes';
  }else if(job.status==='success'&&['deploy','start','stop','restart','recreate'].includes(job.action)){
   const dlg=$('#job-progress-modal');
   if(!dlg.dataset.refreshScheduled){
    dlg.dataset.refreshScheduled='1';
    $('#job-current').textContent='Action terminée avec succès';
    $('#job-log').textContent='Actualisation des informations et de la réconciliation…';
    $('#job-modal-close').disabled=true;
    const footerClose=$('#job-modal-footer-close');if(footerClose)footerClose.hidden=true;
    toast('Action terminée. Actualisation en cours…','success');
    setTimeout(()=>window.location.reload(),1800);
   }
  }
 }catch(e){setTimeout(()=>watchJob(jobId),1800)}
}
document.addEventListener('submit',async e=>{
 const form=e.target;if(!form.matches('[data-job-form]'))return;
 e.preventDefault();const client=form.dataset.client,action=form.dataset.action,title=form.dataset.title;
 if(action==='delete'){
  const dlg=$('#delete-confirm-modal');
  dlg.dataset.formAction=form.action;dlg.dataset.client=client;dlg.dataset.title=title;dlg.dataset.protected=form.dataset.protected||'false';
  $('#delete-confirm-client').textContent=client.toUpperCase();$('#delete-confirm-node').textContent=(form.dataset.node||'—').toUpperCase();
  const selected=dlg.querySelector('input[name="delete-mode-ui"][value="archive"]');if(selected)selected.checked=true;
  const text=$('#delete-confirm-text');if(text)text.value='';refreshSecureDeleteModal();dlg.showModal();return;
 }
 await submitJobForm(form,client,action,title);
});
async function submitJobForm(form,client,action,title,body=null){
 openJobModal(client,action,title);
 try{
  const r=await fetch(form.action,{method:'POST',body:body||new FormData(form),headers:{'X-Requested-With':'XMLHttpRequest'}});
  if(!r.ok)throw new Error(await r.text());
  const payload=await r.json();setTimeout(()=>watchJob(payload.job_id),350);
 }catch(err){$('#job-current').textContent='Impossible de lancer l’opération';$('#job-log').textContent=String(err);$('#job-modal-close').disabled=false;toast('Erreur lors du lancement','error')}
}
document.addEventListener('click',e=>{
 const claim=e.target.closest('[data-open-claim]');if(claim)openClaim(claim.dataset.openClaim);
 if(e.target.closest('[data-mobile-toggle]'))$('.sidebar')?.classList.toggle('open');
});

function chartColor(alpha=1){return `rgba(255,38,53,${alpha})`}
function drawChart(canvas,values){
 const ctx=canvas.getContext('2d'),dpr=window.devicePixelRatio||1;
 const rect=canvas.getBoundingClientRect();const css=getComputedStyle(canvas);const rw=rect.width||canvas.parentElement?.clientWidth||320;const rh=rect.height||parseFloat(css.height)||82;canvas.width=Math.max(1,Math.round(rw*dpr));canvas.height=Math.max(1,Math.round(rh*dpr));ctx.setTransform(dpr,0,0,dpr,0,0);
 const w=rw,h=rh,p=5;ctx.clearRect(0,0,w,h);
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


async function loadNodeLogs(nodeId,source){
 const output=$('#node-log-output'),meta=$('#node-log-meta');
 if(!output)return;
 output.textContent='Chargement des logs…';
 try{
  const r=await fetch(`/api/nodes/${nodeId}/logs?source=${encodeURIComponent(source)}&lines=300`,{cache:'no-store'});
  const p=await r.json();
  if(!r.ok)throw new Error(p.detail||'Erreur de chargement');
  output.textContent=p.logs||'Aucun log disponible.';
  meta.textContent=`${p.label} · actualisé ${new Date(p.generated_at).toLocaleString('fr-FR')}`;
  output.scrollTop=output.scrollHeight;
 }catch(e){
  output.textContent=`Impossible de charger les logs : ${e}`;
 }
}

function refreshSecureDeleteModal(){
 const dlg=$('#delete-confirm-modal');if(!dlg)return;
 const mode=dlg.querySelector('input[name="delete-mode-ui"]:checked')?.value||'archive';
 const protectedBox=dlg.dataset.protected==='true';
 const strong=protectedBox||mode==='purge';
 const wrap=$('#delete-confirm-strong'),input=$('#delete-confirm-text'),submit=$('#delete-confirm-submit'),warning=$('#delete-mode-warning');
 if(wrap)wrap.hidden=!strong;
 if(warning)warning.textContent=mode==='archive'?'Les données de configuration seront conservées sur le node.':mode==='delete'?'Le dossier persistant de l’AppBox sera supprimé. Les médias RDAD ne sont jamais touchés.':'La purge retire aussi les références techniques associées. Cette opération est irréversible.';
 if(submit)submit.textContent=mode==='archive'?'Archiver l’AppBox':mode==='delete'?'Supprimer l’AppBox':'Purger définitivement';
 if(submit)submit.disabled=strong&&String(input?.value||'').trim().toUpperCase()!=='SUPPRIMER';
}

document.addEventListener('DOMContentLoaded',()=>{
 $$('[data-node-poll]').forEach(el=>pollNode(el.dataset.nodePoll));[...new Set($$('[data-node-chart]').map(c=>c.dataset.node))].forEach(loadNodeCharts);
 const closeJobModal=()=>{const button=$('#job-modal-close');const target=button?.dataset.redirect;$('#job-progress-modal')?.close();if(target)location.href=target};
 $('#job-modal-close')?.addEventListener('click',closeJobModal);
 $('#job-modal-footer-close')?.addEventListener('click',closeJobModal);
 $$('[data-log-source]').forEach(button=>button.addEventListener('click',()=>{
   $$('[data-log-source]').forEach(b=>b.classList.remove('active'));
   button.classList.add('active');
   loadNodeLogs(button.dataset.node,button.dataset.logSource);
 }));
 const initialLog=$('[data-log-source].active');if(initialLog)loadNodeLogs(initialLog.dataset.node,initialLog.dataset.logSource);
 $$('[data-close-dialog]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
 const deleteDialog=$('#delete-confirm-modal');
 $$('[data-delete-cancel]').forEach(button=>button.addEventListener('click',()=>deleteDialog?.close()));
 deleteDialog?.querySelectorAll('input[name="delete-mode-ui"]').forEach(input=>input.addEventListener('change',refreshSecureDeleteModal));
 $('#delete-confirm-text')?.addEventListener('input',refreshSecureDeleteModal);
 $('#delete-confirm-submit')?.addEventListener('click',async()=>{
   const action=deleteDialog?.dataset.formAction,client=deleteDialog?.dataset.client,title=deleteDialog?.dataset.title;
   const mode=deleteDialog?.querySelector('input[name="delete-mode-ui"]:checked')?.value||'archive';
   const confirmation=$('#delete-confirm-text')?.value||'';
   if(!action||!client)return;
   const protectedBox=deleteDialog.dataset.protected==='true';
   if((protectedBox||mode==='purge')&&confirmation.trim().toUpperCase()!=='SUPPRIMER')return;
   deleteDialog.close();
   const form=document.querySelector(`form[data-job-form][data-action="delete"][data-client="${CSS.escape(client)}"]`);
   if(form){const data=new FormData(form);data.set('deletion_mode',mode);data.set('confirmation',confirmation);await submitJobForm(form,client,'delete',title||`Suppression de ${client.toUpperCase()}`,data);}
 });
 const params=new URLSearchParams(location.search);if(params.get('job')){const client=location.pathname.split('/').pop();openJobModal(client,'deploy','Opération AppBox');watchJob(params.get('job'))}
});

document.addEventListener('DOMContentLoaded',()=>{
 const tabs=document.querySelector('[data-inventory-tabs]');
 if(tabs){
   tabs.addEventListener('click',event=>{
     const button=event.target.closest('button[data-target]');
     if(!button)return;
     tabs.querySelectorAll('button').forEach(item=>item.classList.toggle('active',item===button));
     document.querySelectorAll('.inventory-panel').forEach(panel=>{
       panel.classList.toggle('active',panel.dataset.panel===button.dataset.target);
     });
   });
 }
 const syncButton=document.querySelector('#inventory-sync-button');
 if(syncButton){
   syncButton.addEventListener('click',async()=>{
     syncButton.disabled=true;
     const original=syncButton.textContent;
     syncButton.textContent='Synchronisation…';
     try{
       const response=await fetch('/api/inventory/sync',{method:'POST'});
       const data=await response.json();
       if(!response.ok)throw new Error(data.detail||'Synchronisation impossible');
       toast(`Inventaire : ${data.containers} conteneur(s), ${data.networks} réseau(x), ${data.volumes} volume(s)`,'success');
       setTimeout(()=>location.reload(),700);
     }catch(error){
       toast(error.message||String(error),'error');
       syncButton.disabled=false;
       syncButton.textContent=original;
     }
   });
 }
});

document.addEventListener('DOMContentLoaded',()=>{document.querySelectorAll('[data-appbox-create-form]').forEach(form=>{const type=form.querySelector('[data-media-type]');const opt=form.querySelector('[data-tautulli-option]');if(!type||!opt)return;const refresh=()=>{const plex=type.value==='plex';opt.hidden=!plex;const cb=opt.querySelector('input');if(cb&&!plex)cb.checked=false;};type.addEventListener('change',refresh);refresh();});});

document.addEventListener('DOMContentLoaded',()=>{
 document.querySelectorAll('[data-appbox-create-form]').forEach(form=>{
   const type=form.querySelector('[data-media-type]');
   const profile=form.querySelector('[data-profile-select]');
   const snapshot=form.querySelector('[data-snapshot-select]');
   const portMode=form.querySelector('[data-port-mode]');
   const manual=form.querySelector('[data-manual-port]');
   const filterOptions=select=>{
     if(!select||!type)return;
     [...select.options].forEach(option=>{
       const optionType=option.dataset.type;
       option.hidden=Boolean(optionType&&optionType!==type.value);
       if(option.selected&&option.hidden)select.value='';
     });
   };
   const refresh=()=>{
     filterOptions(profile);filterOptions(snapshot);
     if(manual&&portMode)manual.hidden=portMode.value!=='manual';
   };
   type?.addEventListener('change',refresh);
   portMode?.addEventListener('change',refresh);
   refresh();
 });
});

document.addEventListener('DOMContentLoaded',()=>{
 document.querySelectorAll('[data-open-dialog]').forEach(button=>{
   button.addEventListener('click',()=>{
     const dialog=document.getElementById(button.dataset.openDialog);
     if(dialog)dialog.showModal();
   });
 });
 document.querySelectorAll('[data-close-dialog]').forEach(button=>{
   button.addEventListener('click',()=>button.closest('dialog')?.close());
 });
 document.querySelectorAll('dialog').forEach(dialog=>{
   dialog.addEventListener('click',event=>{
     if(event.target===dialog)dialog.close();
   });
 });
 const tabs=document.querySelector('[data-resource-tabs]');
 if(tabs){
   tabs.addEventListener('click',event=>{
     const button=event.target.closest('button[data-target]');
     if(!button)return;
     tabs.querySelectorAll('button').forEach(item=>item.classList.toggle('active',item===button));
     document.querySelectorAll('.resource-panel').forEach(panel=>{
       panel.classList.toggle('active',panel.dataset.panel===button.dataset.target);
     });
   });
 }
});

document.addEventListener('DOMContentLoaded',()=>{
 document.querySelectorAll('[data-appbox-create-form]').forEach(form=>{
   const type=form.querySelector('[data-media-type]');
   const profile=form.querySelector('[data-profile-select]');
   const reference=form.querySelector('[data-reference-select]');
   const refreshReferenceOptions=()=>{
     if(!type)return;
     [profile,reference].forEach(select=>{
       if(!select)return;
       [...select.options].forEach(option=>{
         const optionType=option.dataset.type;
         option.hidden=Boolean(optionType&&optionType!==type.value);
       });
       if(select.selectedOptions[0]?.hidden){
         const firstVisible=[...select.options].find(option=>!option.hidden&&!option.disabled);
         if(firstVisible)select.value=firstVisible.value;
       }
     });
   };
   type?.addEventListener('change',refreshReferenceOptions);
   refreshReferenceOptions();
 });
});

document.addEventListener('DOMContentLoaded',()=>{
 document.querySelectorAll('[data-appbox-create-form]').forEach(form=>{
   const mode=form.querySelector('[data-placement-mode]');
   const target=form.querySelector('[data-target-node]');
   const confirm=form.querySelector('[data-bare-metal-confirm]');
   const preview=form.querySelector('[data-placement-preview]');
   const refresh=async()=>{
     if(!mode)return;
     const automatic=mode.value==='automatic';
     if(target)target.hidden=automatic;
     if(confirm)confirm.hidden=automatic;
     if(preview){
       preview.textContent=automatic
         ? 'Le Control Plane choisira uniquement un node AppBox-Node, jamais un Bare-Metal.'
         : 'Le node est imposé manuellement. Un Bare-Metal exige une confirmation explicite.';
     }
   };
   mode?.addEventListener('change',refresh);
   refresh();
 });
});

document.addEventListener('DOMContentLoaded',()=>{
 const dialog=document.getElementById('agent-token-dialog');
 const command=document.querySelector('[data-token-command]');
 const name=document.querySelector('[data-token-node-name]');
 document.querySelectorAll('[data-token-node]').forEach(button=>{
   button.addEventListener('click',async()=>{
     if(name)name.textContent=button.dataset.tokenName||button.dataset.tokenNode;
     if(command)command.textContent='Génération…';
     dialog?.showModal();
     const body=new URLSearchParams({label:'installation-ui'});
     try{
       const response=await fetch(`/nodes/${button.dataset.tokenNode}/agent-token-json`,{
         method:'POST',
         headers:{'Content-Type':'application/x-www-form-urlencoded'},
         body
       });
       const data=await response.json();
       if(!response.ok)throw new Error(data.detail||'Erreur');
       const url=data.control_plane_url||location.origin;
       const installer=data.installer_url||`${url}/downloads/install-agent.sh`;
       command.textContent=`curl -fsSL '${installer}' | sudo bash -s -- '${data.node_id}' '${url}' '${data.token}'`;
     }catch(error){
       command.textContent=`Erreur : ${error.message}`;
     }
   });
 });
 document.querySelector('[data-copy-token]')?.addEventListener('click',async()=>{
   if(command)await navigator.clipboard.writeText(command.textContent);
 });
});
