'use strict';
const $=s=>document.querySelector(s);
const state={activeId:null,conversations:[],history:[],run:null,view:0,starting:null,controller:null,
  drafts:new Map(),attachments:[],dir:'',entries:[],file:null,streamAssistant:null,toolCards:new Map(),passwordLogin:true,health:null};
try{state.drafts=new Map(JSON.parse(sessionStorage.getItem('agent_drafts')||'[]'));}catch{}
let theme='system';try{theme=localStorage.getItem('agent_theme')||'system';}catch{}
const darkMedia=matchMedia('(prefers-color-scheme:dark)');
function applyTheme(){document.documentElement.dataset.theme=theme==='system'?(darkMedia.matches?'dark':'light'):theme;$('#themeBtn').title='外观：'+({system:'跟随系统',light:'浅色',dark:'深色'}[theme]);}
darkMedia.addEventListener('change',applyTheme);applyTheme();
let toastTimer,conversationVersion=0;
let startIntents=new Map();
try{startIntents=new Map(JSON.parse(sessionStorage.getItem('agent_start_intents')||'[]'));}catch{}
function persistIntents(){try{sessionStorage.setItem('agent_start_intents',JSON.stringify([...startIntents].slice(-20)));}catch{}}
function requestId(){if(globalThis.crypto?.randomUUID)return crypto.randomUUID();const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return [...bytes].map(b=>b.toString(16).padStart(2,'0')).join('');}
async function copyText(text){
 if(navigator.clipboard?.writeText)return navigator.clipboard.writeText(text);
 const area=document.createElement('textarea');area.value=text;area.className='visually-hidden';document.body.append(area);area.select();
 try{if(!document.execCommand('copy'))throw new Error('复制不可用');}finally{area.remove();}
}
const toolNames={read_file:'读取文件',write_file:'写入文件',edit_file:'编辑文件',list_files:'查找文件',
 search_code:'搜索文本',run_command:'查看工作区',web_search:'搜索网页',web_fetch:'读取网页',
 git_status:'查看 Git 状态',git_diff:'查看改动',git_log:'查看提交记录'};
const statusNames={running:'正在执行',completed:'本次结束',cancelled:'已停止',failed:'未完成',interrupted:'已中断',limited:'达到本次限制'};
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=name=>`<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const token=()=>localStorage.getItem('agent_token')||'';
function toast(message){clearTimeout(toastTimer);$('#toast').textContent=message;$('#toast').hidden=false;toastTimer=setTimeout(()=>$('#toast').hidden=true,4800);}
function authHeaders(){const headers={};if(token())headers.Authorization='Bearer '+token();return headers;}
async function api(path,{method='GET',body,signal,raw=false}={}){
 const headers=authHeaders();if(body!==undefined)headers['Content-Type']='application/json';
 const response=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),signal});
 if(response.status===401){$('#loginPage').hidden=false;throw new Error('登录已失效，请重新登录');}
 if(!response.ok){let message;try{message=(await response.json()).detail;}catch{}const error=new Error(typeof message==='string'?message:`请求失败（${response.status}）`);error.status=response.status;throw error;}
 return raw?response:response.json();
}
function markdown(text){
 if(!text)return '';
 if(!window.marked||!window.DOMPurify)return escapeHtml(text).replace(/\n/g,'<br>');
 return DOMPurify.sanitize(marked.parse(text,{breaks:true}),{
  ALLOWED_TAGS:['p','br','strong','em','del','a','h1','h2','h3','h4','ul','ol','li','blockquote','pre','code','table','thead','tbody','tr','th','td','hr'],
  ALLOWED_ATTR:['href','title'],ALLOW_DATA_ATTR:false});
}
function nearBottom(){return $('#chatScroll').scrollHeight-$('#chatScroll').scrollTop-$('#chatScroll').clientHeight<100;}
function scrollBottom(force=false){if(force||nearBottom())$('#chatScroll').scrollTop=$('#chatScroll').scrollHeight;}
function message(role,text,partial=false){
 const el=document.createElement('article');el.className='message '+role;
 const body=document.createElement('div');body.className='message-content';
 if(role==='user')body.textContent=text;else body.innerHTML=markdown(text);
 el.append(body);
 if(partial){const note=document.createElement('div');note.className='message-note';note.textContent='这段回复没有完成';el.append(note);}
 if(role==='assistant'){const actions=document.createElement('div');actions.className='message-actions';const copy=document.createElement('button');copy.className='copy-text';copy.textContent='复制';copy.onclick=()=>copyText(text).then(()=>toast('已复制')).catch(()=>toast('复制失败，请手动选择文本'));actions.append(copy);el.append(actions);}
 $('#messages').append(el);return el;
}
function toolCard(data,status='running'){
 const card=document.createElement('section');card.className='tool-card';card.dataset.state=status;
 const args=data.arguments||{};const hint=args.path||args.command||args.query||args.url||args.pattern||'';
 card.innerHTML=`<div class="tool-header"><span class="tool-dot"></span><span class="tool-name">${escapeHtml(toolNames[data.tool_name]||data.tool_name||'工具调用')}</span><span class="tool-status"></span></div>${hint?`<div class="tool-hint">${escapeHtml(hint)}</div>`:''}<details class="tool-detail"><summary>查看过程</summary><pre class="tool-arguments"></pre><pre class="tool-output"></pre></details>`;
 card.querySelector('.tool-arguments').textContent=Object.keys(args).length?JSON.stringify(args,null,2):'';
 updateTool(card,data.result||'',status);return card;
}
function updateTool(card,result,status){
 if(!status)status=/^\[(ERROR|BLOCKED|TIMEOUT)\]/.test(result)?'error':/^\[(CANCELLED|INTERRUPTED|LIMIT)\]/.test(result)?'cancelled':'complete';
 card.dataset.state=status;
 card.querySelector('.tool-status').textContent={running:'执行中',complete:'完成',error:'失败',cancelled:'未完成',unknown:'未确认'}[status]||'未确认';
 card.querySelector('.tool-output').textContent=result||(status==='running'?'等待工具返回结果。':'尚未记录到结果，请先核对实际文件。');
 if(status==='error')card.querySelector('details').open=true;
}
function systemNote(text,error=false){const el=document.createElement('div');el.className='system-note'+(error?' error':'');el.textContent=text;$('#messages').append(el);}
function emptyState(){
 $('#messages').innerHTML=`<section class="empty-state"><div class="empty-kicker">${icon('work')}个人工作台</div><h1>从一个文件开始。</h1><p>上传文件，说明你想做什么。</p><div class="suggestions"><button class="suggestion" data-upload>${icon('upload')}<strong>上传一份资料</strong><small>文档、笔记或代码</small></button><button class="suggestion" data-prompt="先看看工作区有哪些文件，简要说明它们的用途。">${icon('folder')}<strong>看看工作区</strong><small>从已有文件接着做</small></button><button class="suggestion" data-prompt="新建 notes.md，帮我整理一份简短的项目待办清单。">${icon('file')}<strong>整理一份待办</strong><small>把想法落到文件里</small></button></div><div class="empty-footnote">${icon('check')}执行过程会保留，刷新后可以接着看。</div></section>`;
 $('#messages [data-upload]').onclick=()=>$('#fileInput').click();
 $('#messages').querySelectorAll('[data-prompt]').forEach(button=>button.onclick=()=>{setInput(button.dataset.prompt);$('#userInput').focus();});
}
function renderHistory(items){
 $('#messages').replaceChildren();state.toolCards.clear();state.streamAssistant=null;
 for(const item of items){
  if(item.role==='tool'){const card=toolCard(item,item.status||'unknown');state.toolCards.set(item.call_id,card);$('#messages').append(card);}
  else if(item.content)message(item.role,item.content,item.partial);
 }
 if(!items.length)emptyState();
 scrollBottom(true);
}
function renderConversations(){
 const term=$('#historySearch').value.toLocaleLowerCase();const items=state.conversations.filter(c=>c.title.toLocaleLowerCase().includes(term));
 $('#convList').innerHTML=items.length?items.map(c=>`<div class="conv-item ${c.id===state.activeId?'active':''}"><button class="conv-select" data-id="${escapeHtml(c.id)}">${c.last_run_status==='running'?'<i></i>':''}<span>${escapeHtml(c.title)}</span></button><button class="conv-delete" aria-label="删除 ${escapeHtml(c.title)}" data-delete="${escapeHtml(c.id)}">×</button></div>`).join(''):`<div class="conv-empty">${term?'没有匹配的对话':'还没有对话'}</div>`;
 $('#convList').querySelectorAll('[data-id]').forEach(button=>button.onclick=()=>selectConversation(button.dataset.id));
 $('#convList').querySelectorAll('[data-delete]').forEach(button=>button.onclick=async()=>{
  const conv=state.conversations.find(c=>c.id===button.dataset.delete);
  if(!confirm(`删除“${conv.title}”？对话记录将被移除。`))return;
  try{await api('/api/conversations/'+conv.id,{method:'DELETE'});if(state.activeId===conv.id)newConversation();await loadConversations();}catch(error){toast(error.message);}
 });
}
async function loadConversations(){const version=++conversationVersion;const data=await api('/api/conversations');if(version!==conversationVersion)return;state.conversations=data.conversations;renderConversations();}
function saveDraft(){state.drafts.set(state.activeId||'new',{text:$('#userInput').value,attachments:[...state.attachments]});try{sessionStorage.setItem('agent_drafts',JSON.stringify([...state.drafts].slice(-30)));}catch{}}
function syncConversationUrl(){const url=new URL(location.href);url.searchParams.set('conversation',state.activeId||'new');history.replaceState(null,'',url);}
function setInput(text){$('#userInput').value=text;resizeInput();updateComposer();}
function restoreDraft(){const draft=state.drafts.get(state.activeId||'new')||{text:'',attachments:[]};state.attachments=[...draft.attachments];setInput(draft.text);renderAttachments();}
function detach(){state.view++;if(state.controller)state.controller.abort();state.controller=null;state.streamAssistant=null;state.run=null;state.lastStatus=null;state.starting=null;$('#connectionNote').hidden=true;}
function newConversation(){saveDraft();detach();state.activeId=null;syncConversationUrl();state.history=[];$('#chatTitle').textContent='新对话';renderHistory([]);restoreDraft();renderConversations();updateComposer();closeDrawers();$('#userInput').focus();}
async function selectConversation(id,preserveCurrent=true){
 if(preserveCurrent)saveDraft();detach();const view=state.view;state.activeId=id;syncConversationUrl();renderConversations();closeDrawers();$('#chatTitle').textContent='加载中…';$('#messages').replaceChildren();
 try{const data=await api('/api/conversations/'+id);if(view!==state.view)return;
  state.history=data.messages;state.lastStatus=data.run?.status||null;state.run=data.run?.status==='running'?data.run:null;
  const visible=state.run&&data.run_start_index!==null?data.messages.slice(0,data.run_start_index+1):data.messages;
  renderHistory(visible);$('#chatTitle').textContent=data.title;restoreDraft();updateComposer();
  if(state.run)watchRun(state.run,view);
  if(!state.run&&data.run)loadRunReport(data.run.id,view);
  if(!state.run&&data.run&&data.run.status!=='completed')systemNote({cancelled:'任务已停止，已完成的文件改动保留。',failed:'这次任务没有完成，可展开执行记录核对。',interrupted:'服务重启后任务已中断，请先核对文件。',limited:'本次调用达到限制，请先查看执行结果。'}[data.run.status]||'任务已结束。');
 }catch(error){if(view===state.view){systemNote(error.message,true);toast(error.message);}}
}
function resizeInput(){const input=$('#userInput');input.style.height='auto';input.style.height=Math.min(input.scrollHeight,180)+'px';}
function updateComposer(){
 const busy=state.run?.status==='running';const starting=state.starting===state.view;
 $('#sendBtn').hidden=busy;$('#stopBtn').hidden=!busy;$('#sendBtn').disabled=starting||!$('#userInput').value.trim();
 $('#userInput').disabled=starting;$('#stopBtn').disabled=!!state.run?.cancelling;
 $('#stopBtn').lastChild.textContent=state.run?.cancelling?'正在停止':'停止';
 $('#composerHint').textContent=busy?(state.run.phaseText||'正在处理任务'):matchMedia('(pointer:coarse)').matches?'点击箭头发送':'Enter 发送，Shift + Enter 换行';
 const label=$('#runLabel');label.textContent=busy?(state.run.phaseText||'正在执行'):starting?'正在提交':statusNames[state.lastStatus]||'准备开始';label.className='run-label'+(busy?' running':state.lastStatus==='failed'?' error':'');
}
function renderAttachments(){
 $('#attachmentList').innerHTML=state.attachments.map((path,i)=>{const document=path.endsWith('.text.md'),original=document?path.slice(0,-8):path;return `<span class="attachment-chip" title="${escapeHtml(original)}">${icon('file')}<button class="attachment-name" data-open="${escapeHtml(original)}">${escapeHtml(original.split('/').at(-1))}</button>${document?'<small>正文</small>':''}<button aria-label="移除附件" data-index="${i}">×</button></span>`;}).join('');
 $('#attachmentList').querySelectorAll('[data-open]').forEach(button=>button.onclick=()=>openFile(button.dataset.open));
 $('#attachmentList').querySelectorAll('[data-index]').forEach(button=>button.onclick=()=>{state.attachments.splice(Number(button.dataset.index),1);renderAttachments();saveDraft();});
}
async function submitTask(){
 if(state.run?.status==='running'||state.starting===state.view)return;
 const text=$('#userInput').value.trim();if(!text)return;
 const view=++state.view;if(state.controller)state.controller.abort();state.controller=null;state.streamAssistant=null;const convId=state.activeId;const attached=[...state.attachments];
 const content=attached.length?text+'\n\n附加工作区文件：\n'+attached.map(p=>'- '+p).join('\n'):text;
 if(content.length>12000){toast('消息和文件路径合计请控制在 12000 字以内');return;}
 const intentKey=JSON.stringify([convId,content]);
 let intent=startIntents.get(intentKey);if(!intent){intent={conversation_id:convId,message:content,request_id:requestId()};startIntents.set(intentKey,intent);persistIntents();}
 state.starting=view;updateComposer();
 try{
  const run=await api('/api/runs',{method:'POST',body:intent});
  startIntents.delete(intentKey);persistIntents();state.drafts.delete(convId||'new');
  loadConversations().catch(()=>{});if(view!==state.view)return;
  state.activeId=run.conversation_id;syncConversationUrl();state.run=run;state.starting=null;state.attachments=[];
  if($('#messages .empty-state'))$('#messages').replaceChildren();
  $('#runReceipt')?.remove();
  message('user',content);setInput('');renderAttachments();saveDraft();$('#chatTitle').textContent=text.slice(0,50);renderConversations();updateComposer();scrollBottom(true);watchRun(run,view);
 }catch(error){
  if(error.status&&error.status<500){startIntents.delete(intentKey);persistIntents();}
  loadConversations().catch(()=>{});
  if(view===state.view){state.starting=null;updateComposer();toast(error.status?error.message:'提交状态未确认。可查看最近对话；再次发送同一内容会核对原请求。');}
 }
}
async function watchRun(run,view){
 const controller=new AbortController();state.controller=controller;let lastSequence=0,gotDone=false;
 try{
  const response=await api('/api/runs/'+run.id+'/events',{signal:controller.signal,raw:true});
  if(!response.headers.get('content-type')?.includes('text/event-stream'))throw new Error('执行记录暂时不可用');
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
  async function frame(value){
   let event='message',id=null;const lines=[];
   for(const line of value.split(/\r?\n/)){if(line.startsWith('event:'))event=line.slice(6).trim();else if(line.startsWith('id:'))id=Number(line.slice(3).trim());else if(line.startsWith('data:'))lines.push(line.slice(5).trimStart());}
   if(!lines.length||view!==state.view)return;
   const data=JSON.parse(lines.join('\n'));if(id!==null&&id<=lastSequence)return;
   const shouldScroll=nearBottom();
   if(event==='phase'){run.phaseText=data.phase==='tool'?(toolNames[data.tool_name]||'正在调用工具'):'正在处理';updateComposer();}
   if(event==='delta'){
    if(!state.streamAssistant){const el=message('assistant','');state.streamAssistant={el,text:''};}
    state.streamAssistant.text+=data.content||'';
    const item=state.streamAssistant;item.el.querySelector('.message-content').innerHTML=markdown(item.text);
    item.el.querySelector('.copy-text').onclick=()=>copyText(item.text).then(()=>toast('已复制')).catch(()=>toast('复制失败'));
   }
   if(event==='tool_call'){
    state.streamAssistant=null;const card=toolCard(data);state.toolCards.set(data.call_id,card);$('#messages').append(card);
   }
   if(event==='tool_result'){
    const card=state.toolCards.get(data.call_id);if(card)updateTool(card,data.result||'');
    if(['write_file','edit_file'].includes(data.tool_name))loadFiles().catch(()=>{});
   }
   if(event==='error')systemNote(data.message||'这次任务没有完成',true);
   if(event==='done'){
    gotDone=true;run.status=data.status;state.lastStatus=data.status;state.run=null;state.streamAssistant=null;updateComposer();
    loadConversations().catch(()=>{});if(view!==state.view)return;
    const dataHistory=await api('/api/conversations/'+run.conversation_id);if(view!==state.view)return;
    state.history=dataHistory.messages;renderHistory(state.history);updateComposer();loadFiles().catch(()=>{});
    loadRunReport(run.id,view);
    if(data.status!=='completed')systemNote({cancelled:'任务已停止，已完成的文件改动保留。',failed:'任务没有完成，请展开执行记录核对。',interrupted:'任务已中断，请核对工作区结果。',limited:'已达到本次调用限制，请先查看结果。'}[data.status]||'任务已结束。');
   }
   if(id!==null)lastSequence=id;
   if(shouldScroll)scrollBottom(true);
  }
  try{
   while(true){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});
    if(buffer.length>2*1024*1024)throw new Error('执行记录过长，请重新连接');
    let boundary;while((boundary=buffer.search(/\r?\n\r?\n/))>=0){const match=buffer.slice(boundary).match(/^\r?\n\r?\n/)[0];const value=buffer.slice(0,boundary);buffer=buffer.slice(boundary+match.length);await frame(value);}
   }
  }finally{await reader.cancel().catch(()=>{});}
  if(!gotDone&&view===state.view)throw new Error('连接已断开，任务可能仍在执行');
 }catch(error){
  if(error.name!=='AbortError'&&view===state.view){$('#connectionNote span').textContent=gotDone?'任务已结束，记录没能刷新。可重新打开对话。':error.message;$('#connectionNote').hidden=false;}
 }finally{if(view===state.view&&state.controller===controller){state.controller=null;updateComposer();}}
}
async function cancelTask(){
 if(!state.run)return;
 const run=state.run,view=state.view;run.cancelling=true;updateComposer();
 try{await api('/api/runs/'+run.id+'/cancel',{method:'POST',body:{}});
  if(view===state.view&&!state.controller)selectConversation(state.activeId);
 }catch(error){toast(error.message);if(view===state.view){run.cancelling=false;updateComposer();}}
}
const fileChecks={matches:'内容与记录一致',changed:'后来有改动',missing:'文件已移除',unconfirmed:'需要核对',unavailable:'暂时无法读取'};
async function loadRunReport(runId,view){
 try{const report=await api('/api/runs/'+runId+'/report');if(view!==state.view)return;
  const wasNear=nearBottom();$('#runReceipt')?.remove();
  const card=document.createElement('section');card.id='runReceipt';card.className='run-receipt';card.setAttribute('aria-label','本次执行记录');
  const counts=report.tool_counts;
  card.innerHTML=`<header><div><span class="section-caption">本次执行</span><h3>${report.files.length?report.files.length+' 个文件有改动记录':report.file_tracking_enabled?'没有文件改动记录':'此任务没有启用文件改动记录'}</h3></div><button class="receipt-export" aria-label="下载执行记录">${icon('download')}</button></header><div class="receipt-stats"><span>${report.duration_seconds} 秒</span><span>${counts.calls} 次工具调用</span>${counts.failed?`<span class="receipt-warning">${counts.failed} 次失败</span>`:''}${counts.unconfirmed?`<span class="receipt-warning">${counts.unconfirmed} 次结果未确认</span>`:''}</div>${report.files.length?'<div class="receipt-files">'+report.files.map(file=>`<div class="receipt-file"><span class="receipt-file-icon">${icon('file')}</span><div><button class="receipt-open" data-path="${escapeHtml(file.path)}" ${file.exists?'':'disabled'}>${escapeHtml(file.path)}</button><small class="${file.current_status==='matches'?'':'receipt-warning'}">${fileChecks[file.current_status]||'需要核对'}</small></div><button class="receipt-diff" data-diff="${escapeHtml(file.path)}">查看改动</button></div>`).join('')+'</div>':''}<footer>记录来自文件工具，文件状态以本次检查为准。<button class="receipt-refresh">重新核对</button></footer>`;
  card.querySelectorAll('[data-path]').forEach(button=>button.onclick=()=>openFile(button.dataset.path));
  card.querySelectorAll('[data-diff]').forEach(button=>button.onclick=()=>openChanges(runId,button.dataset.diff,view));
  card.querySelector('.receipt-export').onclick=async()=>{try{const latest=await api('/api/runs/'+runId+'/report');downloadBlob(new Blob([JSON.stringify(latest,null,2)],{type:'application/json;charset=utf-8'}),'执行记录-'+runId.slice(0,8)+'.json');}catch(error){toast(error.message);}};
  card.querySelector('.receipt-refresh').onclick=()=>loadRunReport(runId,view);
  $('#messages').append(card);if(wasNear)scrollBottom(true);
 }catch(error){if(view===state.view)toast('执行记录暂时无法读取，可重新打开对话核对。');}
}
async function openChanges(runId,path,view){
 try{const report=await api('/api/runs/'+runId+'/report');if(view!==state.view)return;
  const file=report.files.find(f=>f.path===path);if(!file)throw new Error('没有找到改动记录');
  $('#changeTitle').textContent=path;$('#changeStatus').textContent=fileChecks[file.current_status]||'需要核对';
  const steps=report.changes.filter(c=>c.path===path&&c.status!=='unchanged');
  $('#changeDiff').innerHTML=steps.map((change,i)=>`<section class="diff-step"><div class="diff-step-heading"><span>第 ${i+1} 次写入${change.status==='pending'?' · 结果未确认':''}</span><small>${change.before_size===null?'新文件':bytes(change.before_size)} → ${change.status==='pending'?'未确认':change.after_size===null?'文件不存在':bytes(change.after_size)}</small></div>${change.note?'<p>'+escapeHtml(change.note)+'</p>':''}<pre>${change.diff?change.diff.split('\n').map(line=>`<span class="${line.startsWith('+')?'diff-add':line.startsWith('-')?'diff-remove':line.startsWith('@@')?'diff-range':''}">${escapeHtml(line)}</span>`).join(''):escapeHtml(change.status==='pending'?'操作开始后没有保存最终结果，请核对当前文件。':'此项没有可展示的文本差异。')}</pre></section>`).join('');
  $('#changeCurrentBtn').hidden=!file.exists;$('#changeCurrentBtn').onclick=()=>{$('#changeDialog').close();openFile(path);};
  $('#changeDialog').showModal();
 }catch(error){toast(error.message);}
}
let drawerReturnFocus=null;
function closeDrawers(){const wasOpen=!$('#drawerShade').hidden;$('#sidebar').classList.remove('open');$('#workspacePanel').classList.remove('open');$('#drawerShade').hidden=true;$('.main').inert=false;$('#sidebar').inert=false;$('#workspacePanel').inert=false;if(wasOpen&&drawerReturnFocus?.isConnected)drawerReturnFocus.focus();}
function openDrawer(selector){closeDrawers();drawerReturnFocus=document.activeElement;$(selector).classList.add('open');$('#drawerShade').hidden=false;$('.main').inert=true;$(selector==='#sidebar'?'#workspacePanel':'#sidebar').inert=true;$(selector).querySelector('button,input').focus();}
function bytes(value){return value<1024?value+' B':value<1024*1024?(value/1024).toFixed(1)+' KB':(value/1024/1024).toFixed(1)+' MB';}
let filesVersion=0;
async function loadFiles(path=state.dir){
 const version=++filesVersion;
 try{const data=await api('/api/files?path='+encodeURIComponent(path));if(version!==filesVersion)return;
  if(data.type!=='dir')return openFile(data.path);
  if(state.dir!==(data.path==='.'?'':data.path))$('#fileSearch').value='';
  state.dir=data.path==='.'?'':data.path;state.entries=data.entries;
  const parts=state.dir?state.dir.split('/'):[];
  $('#breadcrumbs').innerHTML='<button data-dir="">工作区</button>'+parts.map((name,i)=>`<span>/</span><button data-dir="${escapeHtml(parts.slice(0,i+1).join('/'))}">${escapeHtml(name)}</button>`).join('');
  $('#breadcrumbs').querySelectorAll('[data-dir]').forEach(button=>button.onclick=()=>loadFiles(button.dataset.dir));
  renderFiles();
 }catch(error){if(version===filesVersion)$('#fileList').innerHTML=`<div class="file-empty">${escapeHtml(error.message)}</div>`;}
}
function renderFiles(){
 const term=$('#fileSearch').value.toLocaleLowerCase();const paths=new Set(state.entries.map(e=>e.path));
 const entries=state.entries.filter(e=>!(e.path.endsWith('.text.md')&&paths.has(e.path.slice(0,-8)))).filter(e=>e.name.toLocaleLowerCase().includes(term));
 $('#fileCount').textContent=entries.length;
 $('#fileList').innerHTML=entries.length?entries.map(entry=>`<button class="file-row" title="${escapeHtml(entry.name)}" data-path="${escapeHtml(entry.path)}" data-dir="${entry.is_dir}">${icon(entry.is_dir?'folder':'file')}<span>${escapeHtml(entry.name)}</span><small>${entry.is_dir?'':bytes(entry.size)}</small></button>`).join(''):`<div class="file-empty">${term?'没有匹配的文件。':'这里还没有文件。<br>上传资料，或新建一份笔记。'}</div>`;
 $('#fileList').querySelectorAll('[data-path]').forEach(button=>button.onclick=()=>button.dataset.dir==='true'?loadFiles(button.dataset.path):openFile(button.dataset.path));
}
function fileView(preview=false){$('#fileContent').hidden=preview;$('#filePreview').hidden=!preview;$('#sourceTab').setAttribute('aria-pressed',String(!preview));$('#previewTab').setAttribute('aria-pressed',String(preview));if(preview)$('#filePreview').innerHTML=markdown($('#fileContent').value||state.file?.notice||'');}
async function openFile(path){
 try{const data=await api('/api/files?path='+encodeURIComponent(path));state.file={...data,original:data.content,new:false};
  $('#fileTitle').textContent=data.path;$('#fileMode').textContent='文件预览';$('#fileName').hidden=true;
  if(data.editable===false)$('#fileMode').textContent='文档正文 · 原文件可下载';$('#sourceTab').textContent=data.editable===false?'提取文本':'原文';
  $('#fileContent').value=data.content;$('#fileContent').readOnly=true;$('#saveFileBtn').hidden=true;$('#editFileBtn').hidden=data.editable===false;
  $('#fileViewTabs').hidden=!/\.(md|markdown|pdf|docx)$/i.test(path);fileView(/\.(pdf|docx)$/i.test(path));
  $('#referenceFileBtn').hidden=data.readable===false;
  $('#deleteFileBtn').hidden=false;$('#downloadFileBtn').hidden=false;$('#fileStatus').textContent=data.notice||bytes(data.size);$('#fileDialog').showModal();
 }catch(error){toast(error.message);}
}
function createFile(){state.file={new:true,path:'',original:''};$('#fileTitle').textContent='新建文件';$('#fileMode').textContent='工作区';$('#fileName').hidden=false;$('#fileName').value=state.dir?state.dir+'/':'';$('#fileContent').value='';$('#fileContent').readOnly=false;$('#fileViewTabs').hidden=true;fileView();$('#saveFileBtn').hidden=false;$('#editFileBtn').hidden=true;$('#deleteFileBtn').hidden=true;$('#downloadFileBtn').hidden=true;$('#referenceFileBtn').hidden=true;$('#fileStatus').textContent='';$('#fileDialog').showModal();$('#fileName').focus();}
async function saveFile(){
 const path=state.file.new?$('#fileName').value.trim():state.file.path;if(!path){toast('先输入文件名');return;}
 $('#saveFileBtn').disabled=true;
 try{await api('/api/files',{method:'POST',body:{path,content:$('#fileContent').value,expected_revision:state.file.revision}});
  state.file.original=$('#fileContent').value;$('#fileDialog').close();toast('已保存');loadFiles();
 }catch(error){toast(error.message);}finally{$('#saveFileBtn').disabled=false;}
}
const uploads=[];let uploading=false;
function renderUploads(){
 $('#uploadQueue').innerHTML=uploads.slice(-10).map(item=>`<div class="upload-item ${item.status}" data-upload-id="${item.id}"><div><span title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span><button class="upload-action" aria-label="${['queued','uploading'].includes(item.status)?'取消上传':'移除上传记录'}">×</button></div><small>${escapeHtml(item.label)}</small>${item.status==='uploading'?`<progress max="100" value="${item.percent}" aria-label="${escapeHtml(item.name)} 上传进度"></progress>`:''}${item.status==='complete'&&item.path?'<button class="upload-open">打开文件</button>':''}</div>`).join('');
 $('#uploadQueue').querySelectorAll('[data-upload-id]').forEach(row=>{const item=uploads.find(u=>u.id===row.dataset.uploadId);row.querySelector('.upload-action').onclick=()=>{if(item.status==='uploading'){item.status='cancelled';item.label='已取消传输；已传完的文件请在工作区核对。';item.xhr?.abort();}else{item.status='cancelled';uploads.splice(uploads.indexOf(item),1);}renderUploads();};const open=row.querySelector('.upload-open');if(open)open.onclick=()=>openFile(item.path);});
}
function sendUpload(item){return new Promise((resolve,reject)=>{
 const xhr=new XMLHttpRequest();item.xhr=xhr;xhr.open('POST','/api/files/upload?path='+encodeURIComponent(item.destination));xhr.timeout=45000;
 for(const [name,value] of Object.entries(authHeaders()))xhr.setRequestHeader(name,value);
 xhr.setRequestHeader('Content-Type','application/octet-stream');
 xhr.upload.onprogress=event=>{item.percent=event.lengthComputable?Math.round(event.loaded/event.total*100):0;item.label=item.percent===100?'正在检查并提取正文…':`正在上传 · ${item.percent}%`;renderUploads();};
 xhr.onload=()=>{let data;try{data=JSON.parse(xhr.responseText);}catch{}if(xhr.status>=200&&xhr.status<300)resolve(data);else{if(xhr.status===401)$('#loginPage').hidden=false;reject(new Error(data?.detail||'上传失败（'+xhr.status+'）'));}};
 xhr.onerror=()=>reject(new Error('连接中断，请刷新工作区核对文件后重试。'));xhr.ontimeout=()=>reject(new Error('上传超时，请刷新工作区核对文件后重试。'));xhr.onabort=()=>reject(new Error('已取消；已传完的文件可在工作区核对。'));
 xhr.send(item.file);
 });}
async function upload(files){
 if(files.length>10){toast('一次最多上传 10 个文件，请分批选择');$('#fileInput').value='';return;}
 if(uploads.filter(u=>['queued','uploading'].includes(u.status)).length+files.length>10){toast('已有文件正在上传，请等这一批完成');return;}
 const origin=state.activeId,view=state.view;
 for(const file of files){const item={id:requestId(),file,name:file.name,destination:(state.dir?state.dir+'/':'')+file.name,origin,view,status:'queued',label:'等待上传',percent:0};
  if(file.size>10*1024*1024){item.status='error';item.label='超过 10 MB，请精简后上传';}else if(!file.size){item.status='error';item.label='空文件，请选择有内容的文件';}uploads.push(item);
 }
 renderUploads();$('#fileInput').value='';if(files.length&&matchMedia('(max-width:1199px)').matches)openDrawer('#workspacePanel');if(!files.length||uploading)return;uploading=true;
 try{
  while(uploads.some(u=>u.status==='queued')){
   const item=uploads.find(u=>u.status==='queued');item.status='uploading';item.label='正在上传';renderUploads();
   try{const data=await sendUpload(item);item.status='complete';item.path=data.path;item.name=data.path.split('/').at(-1);item.label=data.notice||(data.renamed?'同名文件已另存':'已上传 · 已加入对话');
    if(data.reference_path){if(state.activeId===item.origin&&state.view===item.view){if(!state.attachments.includes(data.reference_path))state.attachments.push(data.reference_path);renderAttachments();saveDraft();}else{const draft=state.drafts.get(item.origin||'new')||{text:'',attachments:[]};if(!draft.attachments.includes(data.reference_path))draft.attachments.push(data.reference_path);state.drafts.set(item.origin||'new',draft);try{sessionStorage.setItem('agent_drafts',JSON.stringify([...state.drafts].slice(-30)));}catch{}item.label='已上传 · 已加入原对话草稿';}}
    else item.label=data.notice||'已上传，未提取到正文';
    loadFiles();
   }catch(error){if(item.status!=='cancelled'){item.status='error';item.label=error.message;}}
   finally{item.xhr=null;item.file=null;if(item.status==='cancelled')loadFiles();renderUploads();}
  }
 }finally{uploading=false;while(uploads.length>20)uploads.shift();renderUploads();}
}
function downloadBlob(blob,name){const link=document.createElement('a');const url=URL.createObjectURL(blob);link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function init(){
 try{const status=await fetch('/api/auth/status').then(r=>r.json());state.passwordLogin=status.password_login!==false;
  $('#loginLabel').textContent=state.passwordLogin?'口令':'访问令牌';
  if(status.auth_required){try{await api('/api/auth/check');}catch{$('#loginPage').hidden=false;return;}}
  $('#loginPage').hidden=true;$('#logoutBtn').hidden=!status.auth_required;
  state.health=await api('/api/health');$('#modelLabel').textContent=state.health.model_configured?state.health.model:'模型尚未配置';
  await loadConversations();await loadFiles();
  const requested=new URL(location.href).searchParams.get('conversation');
  const active=requested==='new'?null:requested||state.conversations[0]?.id;
  if(active)await selectConversation(active,false);else{emptyState();restoreDraft();updateComposer();}
 }catch(error){toast(error.message);emptyState();}
}
$('#composer').onsubmit=event=>{event.preventDefault();submitTask();};
$('#userInput').addEventListener('input',()=>{resizeInput();updateComposer();saveDraft();});
$('#userInput').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&event.keyCode!==229&&(!matchMedia('(pointer:coarse)').matches||event.ctrlKey||event.metaKey)){event.preventDefault();submitTask();}});
$('#stopBtn').onclick=cancelTask;$('#newChatBtn').onclick=newConversation;$('#historySearch').oninput=renderConversations;
$('#menuBtn').onclick=()=>openDrawer('#sidebar');$('#filesBtn').onclick=()=>{if(matchMedia('(max-width:1199px)').matches)openDrawer('#workspacePanel');loadFiles();};$('#closeFilesBtn').onclick=closeDrawers;$('#drawerShade').onclick=closeDrawers;
$('#refreshFilesBtn').onclick=()=>loadFiles();$('#createFileBtn').onclick=createFile;
$('#uploadFilesBtn').onclick=$('#attachBtn').onclick=()=>$('#fileInput').click();$('#fileInput').onchange=()=>upload([...$('#fileInput').files]);
$('#editFileBtn').onclick=()=>{fileView();$('#fileContent').readOnly=false;$('#fileMode').textContent='编辑文件';$('#saveFileBtn').hidden=false;$('#editFileBtn').hidden=true;$('#fileContent').focus();};$('#saveFileBtn').onclick=saveFile;
$('#fileContent').oninput=()=>{$('#fileStatus').textContent=$('#fileContent').value!==state.file.original?'有未保存的修改':'';};
$('#fileDialog').addEventListener('cancel',event=>{if(state.file&&$('#fileContent').value!==state.file.original&&!confirm('放弃未保存的修改？'))event.preventDefault();});
$('#fileDialog form').onsubmit=event=>{if(state.file&&$('#fileContent').value!==state.file.original&&!confirm('放弃未保存的修改？'))event.preventDefault();};
$('#fileContent').onkeydown=event=>{if((event.ctrlKey||event.metaKey)&&event.key==='s'){event.preventDefault();if(!$('#fileContent').readOnly)saveFile();}};
$('#downloadFileBtn').onclick=async()=>{try{const response=await api('/api/files/download?path='+encodeURIComponent(state.file.path),{raw:true});downloadBlob(await response.blob(),state.file.name);}catch(error){toast(error.message);}};
$('#deleteFileBtn').onclick=async()=>{if(!confirm('删除 '+state.file.path+'？'))return;try{await api('/api/files?path='+encodeURIComponent(state.file.path)+'&revision='+encodeURIComponent(state.file.revision||''),{method:'DELETE'});$('#fileDialog').close();loadFiles();toast('已删除');}catch(error){toast(error.message);}};
$('#exportBtn').onclick=async()=>{if(!state.activeId){toast('还没有可导出的对话');return;}try{const data=await api('/api/conversations/'+state.activeId);const text='# '+data.title+'\n\n'+data.messages.map(m=>m.role==='tool'?`### ${toolNames[m.tool_name]||m.tool_name}\n\n${m.result||'尚未记录到结果'}`:`### ${m.role==='user'?'我':'助手'}\n\n${m.content}`).join('\n\n');downloadBlob(new Blob([text],{type:'text/markdown;charset=utf-8'}),data.title.replace(/[\\/:*?"<>|]/g,'_')+'.md');}catch(error){toast(error.message);}};
$('#reconnectBtn').onclick=()=>state.activeId&&selectConversation(state.activeId);
$('#logoutBtn').onclick=()=>{localStorage.removeItem('agent_token');sessionStorage.removeItem('agent_start_intents');detach();location.reload();};
$('#loginForm').onsubmit=async event=>{event.preventDefault();$('#loginBtn').disabled=true;$('#loginErr').textContent='';try{if(state.passwordLogin){const data=await api('/api/auth/login',{method:'POST',body:{password:$('#loginPw').value}});localStorage.setItem('agent_token',data.token);}else{localStorage.setItem('agent_token',$('#loginPw').value);await api('/api/auth/check');}$('#loginPw').value='';await init();}catch(error){$('#loginErr').textContent=error.message;localStorage.removeItem('agent_token');}finally{$('#loginBtn').disabled=false;}};
$('#sourceTab').onclick=()=>fileView();$('#previewTab').onclick=()=>fileView(true);$('#fileSearch').oninput=renderFiles;
$('#referenceFileBtn').onclick=()=>{const path=state.file.reference_path||state.file.path;if(!state.attachments.includes(path))state.attachments.push(path);renderAttachments();saveDraft();$('#fileDialog').close();closeDrawers();$('#userInput').focus();toast('已加入对话');};
$('#themeBtn').onclick=()=>{theme={system:'light',light:'dark',dark:'system'}[theme]||'system';try{localStorage.setItem('agent_theme',theme);}catch{}applyTheme();toast('外观：'+{system:'跟随系统',light:'浅色',dark:'深色'}[theme]);};
$('#chatScroll').addEventListener('scroll',()=>{$('#latestBtn').hidden=nearBottom()||!!$('#messages .empty-state');});$('#latestBtn').onclick=()=>scrollBottom(true);
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('#drawerShade').hidden)closeDrawers();if(event.key==='Tab'&&!$('#drawerShade').hidden){const drawer=document.querySelector('.sidebar.open,.workspace-panel.open');const items=[...drawer.querySelectorAll('button:not([hidden]),input,a[href]')].filter(e=>!e.disabled);const first=items[0],last=items.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}});
window.addEventListener('resize',()=>{if(!matchMedia('(max-width:1199px)').matches||(!matchMedia('(max-width:700px)').matches&&$('#sidebar').classList.contains('open')))closeDrawers();});
let dragDepth=0;
document.addEventListener('dragenter',event=>{if([...event.dataTransfer?.types||[]].includes('Files')){event.preventDefault();dragDepth++;$('#dropOverlay').hidden=false;}});
document.addEventListener('dragover',event=>{if([...event.dataTransfer?.types||[]].includes('Files')){event.preventDefault();event.dataTransfer.dropEffect='copy';}});
document.addEventListener('dragleave',()=>{if(--dragDepth<=0){dragDepth=0;$('#dropOverlay').hidden=true;}});
document.addEventListener('drop',event=>{if([...event.dataTransfer?.types||[]].includes('Files')){event.preventDefault();dragDepth=0;$('#dropOverlay').hidden=true;upload([...event.dataTransfer.files]);}});
window.addEventListener('blur',()=>{dragDepth=0;$('#dropOverlay').hidden=true;});
setInterval(()=>{if(state.conversations.some(c=>c.last_run_status==='running')&&$('#loginPage').hidden)loadConversations().catch(()=>{});},4000);
init();
