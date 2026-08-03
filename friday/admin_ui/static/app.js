'use strict';
const state={token:sessionStorage.getItem('jericho_api_token')||'',view:'dashboard',users:[],usersTotal:0,presets:[],capabilities:[],knowledge:[],knowledgeTag:'',knowledgeQuery:'',conflictStatus:'suggested',relationStatus:'suggested',knowledgeSince:'',knowledgeUntil:'',timelineSince:'',timelineUntil:'',inbox:[],inboxGroups:[],inboxAxis:'extension',entities:[],containers:[],resolutions:[],relationCandidates:[],conflicts:[],lifecycle:[],cleanup:[],audit:[],inspectedKnowledge:null,userId:'',activity:[],activitySummary:null,activitySince:'',activityUntil:'',activityOffset:0,activityFound:null,conversationsOffset:0,auditOffset:0,auditAnchor:null,inboxOffset:0,knowledgeOffset:0,entitiesOffset:0,relationsOffset:0,conflictsOffset:0,resolutionsOffset:0,filesOffset:0,cleanupOffset:0,lifecycleOffset:0,chatFeed:[],chatPerson:null,chatMessages:[]};
const views=[['dashboard','Обзор','◈'],['chats','Переписка','✉'],['inbox','Inbox','▣'],['knowledge','Знания','◇'],['timeline','Хроника','◴'],['graph','Граф','⌘'],['quality','Качество','◎'],['cleanup','Ревизия','⌫'],['users','Пользователи','♙'],['activity','Активность','◷'],['conversations','Диалоги','◌'],['files','Файлы','▤'],['backups','Резервирование','⬡'],['audit','Аудит','≋'],['diagnostics','Диагностика','⚙']];
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const q=v=>encodeURIComponent(v??'').replace(/'/g,'%27').replace(/%20/g,'+');
const fmtDate=v=>v?new Date(v).toLocaleString('ru-RU'):'—';
const short=(v,n=90)=>{v=String(v??'');return v.length>n?v.slice(0,n)+'…':v};
const parse=(v,fallback={})=>{if(typeof v!=='string')return v??fallback;try{return JSON.parse(v)}catch{return fallback}};
const modeName=v=>({dialogue:'Диалог',knowledge_work:'Работа со знаниями',research:'Исследование'}[v]||v||'Диалог');
// CSP-safe event wiring: no inline handlers. Rendered HTML carries a
// data-call/data-change attribute with a JSON [action, ...args] payload and a
// single delegated listener dispatches into the explicit `actions` registry.
const actions={};
const call=(fn,...args)=>`data-call="${esc(JSON.stringify([fn,...args]))}"`;
const chg=(fn,...args)=>`data-change="${esc(JSON.stringify([fn,...args]))}"`;
function dispatch(spec,extra){let parsed;try{parsed=JSON.parse(spec)}catch{return}if(!Array.isArray(parsed)||!parsed.length)return;const[fn,...args]=parsed;if(extra!==undefined)args.push(extra);if(typeof actions[fn]==='function')actions[fn](...args)}
document.addEventListener('click',e=>{const el=e.target.closest('[data-call]');if(el)dispatch(el.getAttribute('data-call'))});
document.addEventListener('change',e=>{const el=e.target.closest('[data-change]');if(el)dispatch(el.getAttribute('data-change'),el.value)});
function toast(message,bad=false){const el=document.getElementById('toast');el.textContent=message;el.classList.toggle('bad',bad);el.classList.add('show');setTimeout(()=>el.classList.remove('show'),3300)}
async function api(path,options={}){const headers=new Headers(options.headers||{});if(state.token)headers.set('Authorization','Bearer '+state.token);if(options.body&&!headers.has('Content-Type')&&!(options.body instanceof FormData))headers.set('Content-Type','application/json');const res=await fetch(path,{...options,headers});if(res.status===401){openTokenDialog('Ключ отсутствует или неверен');throw new Error('Требуется авторизация')}let data=null;const type=res.headers.get('content-type')||'';if(type.includes('json'))data=await res.json();else data=await res.text();if(!res.ok)throw new Error(data?.detail||data||`HTTP ${res.status}`);return data}
async function download(path,filename){try{const headers={};if(state.token)headers.Authorization='Bearer '+state.token;const res=await fetch(path,{headers});if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||`HTTP ${res.status}`);const blob=await res.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;const disposition=res.headers.get('content-disposition')||'';const match=disposition.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);a.download=filename||(match?decodeURIComponent(match[1].replace(/"/g,'')):'download');a.click();setTimeout(()=>URL.revokeObjectURL(url),5000)}catch(e){toast(e.message,true)}}
function renderNav(){document.getElementById('nav').innerHTML=views.map(([id,label,icon])=>`<button class="${state.view===id?'active':''}" ${call('navigate',id)}><span aria-hidden="true">${icon}</span>${label}</button>`).join('')}
const PAGE=100;
// Списки рисовали первую страницу и молчали о том, что она первая: `count` в ответе —
// это len(items), то есть на полной странице он равен лимиту и неотличим от «это всё».
// Пейджер показывается там, где сервер отдаёт НАСТОЯЩИЙ total; где его нет — ниже
// ставится честная отметка об обрезке, а не тишина.
const pager=(action,offset,shown,total)=>{const from=offset+1,to=offset+shown;return `<div class="toolbar"><button class="btn small" ${call(action,-1)} ${offset===0?'disabled':''}>← Назад</button><span class="muted">${shown?`${from}–${to}`:'0'} из ${Number(total||0).toLocaleString('ru')}</span><button class="btn small" ${call(action,1)} ${to>=Number(total||0)?'disabled':''}>Вперёд →</button></div>`};
// Смещение принадлежит паре (раздел, пользователь). Не сбросив его при смене
// аккаунта, экран показывал «Диалогов пока нет» тому, у кого диалоги есть, —
// ровно та ложь о размере, против которой затевалась пагинация.
const resetPages=()=>{state.conversationsOffset=0;state.auditOffset=0;state.auditAnchor=null;state.activityOffset=0;state.inboxOffset=0;state.knowledgeOffset=0;state.entitiesOffset=0;state.relationsOffset=0;state.conflictsOffset=0;state.filesOffset=0;state.cleanupOffset=0;state.lifecycleOffset=0};
// Бейдж у заголовка показывал длину СТРАНИЦЫ (100), пока пейджер строкой ниже
// честно писал «1–100 из 1533». Одно число на экране противоречило другому.
// Когда общее известно — показываем его, иначе то, что есть.
const countBadge=(shown,total)=>{const n=Number(total);return `<span class="badge">${Number.isFinite(n)&&n>0?n.toLocaleString('ru'):shown}</span>`};
const pageStep=(key,direction)=>{state[key]=Math.max(0,(state[key]||0)+direction*PAGE);return refresh()};
actions.conversationsPage=direction=>pageStep('conversationsOffset',direction);
actions.auditPage=direction=>pageStep('auditOffset',direction);
actions.inboxPage=direction=>pageStep('inboxOffset',direction);
actions.knowledgePage=direction=>pageStep('knowledgeOffset',direction);
actions.entitiesPage=direction=>pageStep('entitiesOffset',direction);
actions.relationsPage=direction=>pageStep('relationsOffset',direction);
actions.resolutionsPage=direction=>pageStep('resolutionsOffset',direction);
actions.conflictsPage=direction=>pageStep('conflictsOffset',direction);
actions.filesPage=direction=>pageStep('filesOffset',direction);
actions.cleanupPage=direction=>pageStep('cleanupOffset',direction);
actions.lifecyclePage=direction=>pageStep('lifecycleOffset',direction);
// Вкладка живёт в адресе. Обновление страницы возвращало на «Обзор», и человек,
// работавший в переписке или в графе, каждый раз начинал заново; адрес заодно
// становится ссылкой, которой можно поделиться и вернуться.
function navigate(view,{push=true}={}){state.view=view;resetPages();if(view==='chats')startLiveChats();else stopLiveChats();renderNav();document.getElementById('pageTitle').textContent=views.find(v=>v[0]===view)?.[1]||view;document.getElementById('sidebar').classList.remove('open');if(push)rememberView(view);refresh()}
function rememberView(view){try{sessionStorage.setItem('jericho_admin_view',view)}catch(e){}const hash='#'+view;if(location.hash!==hash)history.replaceState(null,'',location.pathname+location.search+hash)}
// Что открыть при загрузке: сначала адрес (им делятся и его правят руками),
// потом последняя вкладка этой сессии, и лишь затем «Обзор».
function startingView(){const fromHash=(location.hash||'').replace(/^#/,'').split('&')[0];if(views.some(v=>v[0]===fromHash))return fromHash;let saved='';try{saved=sessionStorage.getItem('jericho_admin_view')||''}catch(e){}return views.some(v=>v[0]===saved)?saved:'dashboard'}
function toggleMenu(){document.getElementById('sidebar').classList.toggle('open')}
function selectedUser(){return state.userId||state.users[0]?.id||''}
async function loadUsers(gen){try{const [users,presets,caps]=await Promise.all([api('/api/admin/users'),api('/api/admin/presets'),api('/api/admin/capabilities')]);if(gen!==undefined&&gen!==renderGen)return;state.users=users.items||[];state.usersTotal=Number(users.total||(users.items||[]).length);state.presets=presets.items||[];state.capabilities=caps.items||[];if(!state.userId||!state.users.some(u=>u.id===state.userId))state.userId=state.users[0]?.id||'';const select=document.getElementById('userSelect');select.innerHTML=state.users.map(u=>`<option value="${esc(u.id)}">${esc(u.display_name||u.username||u.id)} · ${esc(u.preset_key)}</option>`).join('');select.value=state.userId;select.onchange=()=>{state.userId=select.value;resetPages();refresh()}}catch(e){if(state.token)toast(e.message,true)}}
function openModal(title,body,footer=''){document.getElementById('modalTitle').textContent=title;document.getElementById('modalBody').innerHTML=body;document.getElementById('modalFoot').innerHTML=footer;document.getElementById('modal').showModal()}
function closeModal(){document.getElementById('modal').close()}
function openTokenDialog(message='Админ-панель передаёт ключ только в заголовке Authorization и хранит его до закрытия вкладки.'){openModal('Доступ к Admin API',`<div class="form"><div class="notice">${esc(message)}</div><label>API-ключ<input id="tokenInput" type="password" autocomplete="off" value="${esc(state.token)}" placeholder="минимум 32 случайных символа"></label></div>`,`<button class="btn" ${call('clearToken')}>Убрать из списка</button><button class="btn primary" ${call('saveToken')}>Сохранить</button>`);setTimeout(()=>document.getElementById('tokenInput')?.focus(),0)}
function saveToken(){state.token=document.getElementById('tokenInput').value.trim();sessionStorage.setItem('jericho_api_token',state.token);closeModal();bootstrap()}
function clearToken(){state.token='';sessionStorage.removeItem('jericho_api_token');closeModal();openTokenDialog('Ключ удалён. Введите новый для доступа к данным.')}
function table(headers,rows){return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
function empty(text){return `<div class="empty">${esc(text)}</div>`}
// Навигация не отменяет уже начатый запрос. Раздел пишет в #app ПОСЛЕ своего await,
// поэтому медленный раздел дорисовывался поверх того, на который пользователь уже
// ушёл: подсвеченный пункт меню и заголовок называли одно, таблица показывала другое.
// Счётчик поколений, а не сверка с state.view: кнопка ↻, смена пользователя и десятки
// действий зовут refresh() для ТОГО ЖЕ вида, и сверка по виду такую гонку не ловит.
let renderGen=0;
const setApp=(gen,html)=>{if(gen===renderGen)document.getElementById('app').innerHTML=html};
function errorView(e,gen){setApp(gen,`<div class="card error"><b>Не удалось загрузить раздел</b><div>${esc(e.message)}</div></div>`)}
async function refresh(){renderNav();const fn=renderers[state.view];if(!fn)return;const gen=++renderGen;document.getElementById('app').innerHTML='<div class="card"><div class="empty">Загрузка…</div></div>';try{await fn(gen)}catch(e){errorView(e,gen)}}
const renderers={};
renderers.dashboard=async gen=>{const data=await api('/api/admin/overview');const c=data.counts||{};const tips=Array.isArray(data.bootstrap_suggestions)?data.bootstrap_suggestions:[];const tipsBlock=tips.length?`<div class="notice"><b>База знаний пока пуста. С чего начать:</b><ul class="tips">${tips.map(t=>`<li>${esc(t)}</li>`).join('')}</ul></div>`:'';setApp(gen,`${tipsBlock}<div class="grid stats">${[['Знаний',c.knowledge_objects],['Сущностей',c.entities],['Inbox',data.pending_inbox],['Пользователей',c.users],['Диалогов',c.conversations],['Сообщений',c.messages]].map(([l,v])=>`<div class="card stat"><div class="value">${Number(v||0).toLocaleString('ru')}</div><div class="label">${l}</div></div>`).join('')}</div><div class="grid two"><section class="card"><h2>Состояние хранилища</h2><div class="kv"><div>Целостность</div><div><span class="badge ${data.database?.ok?'ok':'bad'}">${esc(data.database?.integrity_check)}</span></div><div>Схема</div><div>${esc(data.database?.schema_version)}</div><div>Размер БД</div><div>${Number(data.database?.database_size_bytes||0).toLocaleString('ru')} байт</div><div>FTS5</div><div>${data.database?.fts_available?'включён':'недоступен'}</div></div></section><section class="card"><h2>Последние резервные копии</h2>${(data.backups||[]).length?data.backups.map(b=>`<div class="toolbar"><span class="badge ok">${esc(b.integrity_check)}</span><span class="mono">${esc(b.database)}</span><span class="grow"></span><span class="muted">${fmtDate(b.created_at)}</span></div>`).join(''):empty('Резервных копий пока нет')}</section></div>`)};
actions.groupInbox=async axis=>{state.inboxAxis=axis;await refresh()};
actions.dismissGroup=async(key,status)=>{const g=state.inboxGroups.find(x=>x.key===key);if(!g){toast('Группа не найдена',true);return}
  const verb=status==='ignored'?'игнорировать':'архивировать';
  if(!confirm(`${verb[0].toUpperCase()+verb.slice(1)} ${g.inbox_ids.length} из ${g.total} материалов группы «${key}»?`))return;
  await bulkApply(g.inbox_ids,batch=>api('/api/admin/inbox/bulk',{method:'POST',body:JSON.stringify({user_id:selectedUser(),inbox_ids:batch,status,promote:false,notes:`групповой разбор: ${state.inboxAxis}=${key}`})}))};
// Мир глазами Пятницы: кто ей писал и что скидывал, в виде списка чатов, с
// возможностью ответить. Сводки по людям в системе не было — были разговоры по
// одному человеку и активность по одному человеку, поэтому «кто вообще писал
// сегодня» собиралось перебором учёток.
renderers.chats = async gen => {
  const data = await api('/api/admin/chats?limit=100');
  if (gen !== renderGen) return;
  state.chatFeed = data.items || [];
  const active = state.chatPerson;
  const rows = state.chatFeed.map(p => {
    const when = p.last_at ? fmtDate(p.last_at) : '—';
    const who = p.last_role === 'assistant' ? 'Пятница' : (p.display_name || p.user_id);
    const preview = String(p.last_content || '').replace(/\s+/g, ' ').slice(0, 90);
    return `<button class="chat-row ${active === p.user_id ? 'active' : ''}" ${call('openChat', p.user_id)}>
      <div class="chat-top"><b>${esc(p.display_name || p.user_id)}</b>
        <span class="muted">${esc(when)}</span></div>
      <div class="chat-preview"><span class="muted">${esc(who)}:</span> ${esc(preview)}</div>
      <div class="chat-meta"><span class="badge">${p.message_count} сообщ.</span>
        <span class="badge">${p.file_count} файлов</span>
        ${p.chat_id ? '' : '<span class="badge warn">нет чата</span>'}</div>
    </button>`;
  });
  const list = rows.length ? rows.join('') : empty('Пятнице пока никто не писал');
  setApp(gen, `<div class="chat-layout">
    <section class="card chat-list"><h2>Кто писал Пятнице</h2>${list}</section>
    <section class="card chat-thread" id="chatThread">${
      active ? '<div class="empty">Загружаю переписку…</div>' : empty('Выберите человека слева')
    }</section>
  </div>`);
  if (active) await loadChatThread(active);
};

async function openChat(userId) {
  state.chatPerson = userId;
  await refresh();
}

// Живая переписка: вкладка обновляется сама, как обычный мессенджер.
//
// Заказ владельца: «хотелось бы, чтобы было как настоящий телеграм» — раньше
// новое сообщение появлялось только после F5.
//
// Опрашивается ДЕШЁВАЯ метка (`/chats/cursor` — два агрегата), а не сама лента:
// та собирается оконной функцией по всем сообщениям плюс четырьмя подзапросами,
// и дёргать её каждые несколько секунд значило бы держать базу занятой впустую.
// Полная перерисовка — только когда метка изменилась.
const LIVE_PERIOD_MS = 4000;
let liveTimer = null;
let liveCursor = '';

function stopLiveChats() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
}

function startLiveChats() {
  stopLiveChats();
  liveTimer = setInterval(async () => {
    // Ушли с вкладки или закрыли окно — не тратим ни запрос, ни батарею.
    if (state.view !== 'chats' || document.hidden || !state.token) return;
    try {
      const mark = await api('/api/admin/chats/cursor');
      const stamp = `${mark.total}:${mark.last_at}`;
      if (stamp === liveCursor) return;
      liveCursor = stamp;
      await refreshChatsQuietly();
    } catch (error) {
      // Молча: обрыв сети на фоновом опросе не повод пугать человека тостом
      // каждые четыре секунды. Следующий тик попробует снова.
    }
  }, LIVE_PERIOD_MS);
}

// Перерисовка БЕЗ «Загрузка…», с сохранением того, что человек делает прямо
// сейчас: набранного ответа и места, до которого он дочитал.
async function refreshChatsQuietly() {
  const draft = document.getElementById('replyText')?.value || '';
  const thread = document.querySelector('#chatThread .thread');
  // «Прилипание» к низу, как в мессенджере: если человек читает старое, лента
  // не должна выдёргивать его вниз при каждом новом сообщении.
  const wasAtBottom = thread
    ? thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80
    : true;
  const keptTop = thread ? thread.scrollTop : 0;
  const gen = ++renderGen;
  try {
    await renderers.chats(gen);
  } catch (error) {
    return;
  }
  if (gen !== renderGen) return;
  const field = document.getElementById('replyText');
  if (field && draft) field.value = draft;
  const fresh = document.querySelector('#chatThread .thread');
  if (fresh) fresh.scrollTop = wasAtBottom ? fresh.scrollHeight : keptTop;
}

// Переписка человека: все его разговоры одной лентой, старые сверху — так же,
// как её видит он сам в Telegram.
async function loadChatThread(userId) {
  const person = (state.chatFeed || []).find(p => p.user_id === userId) || {};
  const box = document.getElementById('chatThread');
  if (!box) return;
  try {
    const convs = await api(`/api/admin/conversations?user_id=${q(userId)}&include_archived=true&limit=20`);
    const items = [];
    for (const conv of (convs.items || []).slice(0, 5)) {
      const page = await api(`/api/admin/conversations/${q(conv.id)}/messages?user_id=${q(userId)}&limit=200`);
      for (const message of page.items || []) items.push({ ...message, conversation_title: conv.title });
    }
    items.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
    state.chatMessages = items;
    const bubbles = items.map(m => {
      const mine = m.role === 'assistant';
      return `<div class="bubble ${mine ? 'from-friday' : 'from-person'}">
        <div class="bubble-head">${mine ? 'Пятница' : esc(person.display_name || userId)}
          <span class="muted">${esc(fmtDate(m.created_at))}</span></div>
        <div class="bubble-body">${esc(String(m.content || '')).replace(/\n/g, '<br>')}</div>
      </div>`;
    }).join('');
    const canReply = Boolean(person.chat_id);
    box.innerHTML = `<div class="toolbar"><h2 class="grow">${esc(person.display_name || userId)}</h2>
        <span class="badge">${items.length} сообщений</span></div>
      <div class="thread">${bubbles || empty('Сообщений нет')}</div>
      ${canReply
        ? `<div class="reply-box"><textarea id="replyText" class="field" rows="3"
             placeholder="Ответить человеку в Telegram…"></textarea>
           <button class="btn primary" ${call('sendReply', userId)}>Отправить</button></div>`
        : '<div class="notice">У этого человека нет привязанного чата — ответить некуда.</div>'}`;
  } catch (error) {
    box.innerHTML = `<div class="notice bad">${esc(String(error.message || error))}</div>`;
  }
}

async function sendReply(userId) {
  const field = document.getElementById('replyText');
  const text = String(field?.value || '').trim();
  if (!text) { toast('Пустой ответ отправить нельзя'); return; }
  try {
    await api(`/api/admin/chats/${q(userId)}/reply`, { method: 'POST', body: JSON.stringify({ text }) });
    field.value = '';
    toast('Ответ поставлен в очередь — мост доставит его в течение 15 секунд');
  } catch (error) {
    toast(String(error.message || error));
  }
}

renderers.inbox=async gen=>{
  const uid=selectedUser();
  const data=await api(`/api/admin/inbox?user_id=${q(uid)}&limit=${PAGE}&offset=${state.inboxOffset}`);if(gen!==renderGen)return;
  state.inbox=data.items||[];
  let groupsBlock='';
  try{
    const gd=await api(`/api/admin/inbox/groups?user_id=${q(uid)}&by=${q(state.inboxAxis)}`);
    state.inboxGroups=gd.groups||[];
    const axisTabs=(gd.axes||[]).map(a=>`<button class="btn small ${a===gd.axis?'primary':''}" ${call('groupInbox',a)}>${esc({extension:'по типу файла',directory:'по каталогу',source:'по источнику',quality:'по качеству разбора'}[a]||a)}</button>`).join(' ');
    const rows=state.inboxGroups.map(g=>{
      const acts=Object.entries(g.actions||{}).map(([k,v])=>`<span class="badge">${esc(k)} ${v}</span>`).join(' ');
      // Качество — единственный измеренный разделитель: на живом импорте совет
      // классификатора стоял `promote` во всех группах, а качество разложило
      // нечитаемое (0.13), дампы (0.198) и связный текст (0.9+) по разным полкам.
      const qm=Number(g.quality_median||0);
      const qcls=qm<0.25?'danger':qm<0.5?'warn':'ok';
      const qual=`<span class="badge ${qcls}">качество ${qm.toFixed(2)}</span> <span class="muted">${Number(g.quality_min||0).toFixed(2)}–${Number(g.quality_max||0).toFixed(2)}</span>`;
      const note=g.truncated?`<span class="muted">действие охватит ${g.inbox_ids.length} из ${g.total}</span>`:'';
      return `<tr><td><b>${esc(g.key)}</b> ${note}</td><td>${g.total}</td><td>${qual}</td><td>${acts}</td><td><button class="btn small" ${call('dismissGroup',g.key,'archived')}>В архив</button> <button class="btn small danger" ${call('dismissGroup',g.key,'ignored')}>Игнорировать</button></td></tr>`});
    groupsBlock=state.inboxGroups.length?`<section class="card"><div class="toolbar"><h2 class="grow">Группы непроверенного (${gd.grouped})</h2>${axisTabs}</div><div class="notice">Групповое действие только отклоняет. Продвижение в знания — поштучно, через «Разобрать», где виден исходный текст.</div>${table(['Группа','Материалов','Качество разбора','Что предлагает классификатор',''],rows)}</section>`:'';
  }catch(e){groupsBlock=`<div class="notice">Группировка недоступна: ${esc(e.message)}</div>`}
  const rows=state.inbox.map(i=>{
    const suggestion=i.suggestions||parse(i.suggestions_json,{});
    const raw=i.raw_object||{};
    const entities=Array.isArray(suggestion.entities)?suggestion.entities:[];
    const tags=Array.isArray(suggestion.tags)?suggestion.tags:(i.suggested_tags||parse(i.suggested_tags_json,[]));
    const advice=suggestion.model_advice&&typeof suggestion.model_advice==='object'?suggestion.model_advice:null;
    const title=suggestion.title||i.knowledge_object?.title||'Требуется разбор';
    const action=i.suggested_action||'review';
    const statusClass=i.status==='pending'?'warn':(i.status==='ignored'?'bad':'ok');
    const selectable=i.status==='pending';
    // Только непроверенные строки можно выбрать. Массовое «Игнорировать» мягко
    // удаляет привязанный Knowledge Object, а очередь показывает и уже
    // разобранные записи — «Выбрать все» + «Игнорировать» выносило из поиска
    // знания, которые оператор подтвердил вручную.
    const check=selectable?`<input class="inbox-check" type="checkbox" value="${esc(i.id)}">`:`<span class="muted" title="Уже разобрано: массовые действия применяются только к непроверенному">—</span>`;
    return `<tr><td>${check}</td><td><b>${esc(title)}</b><div class="muted">${esc(short(raw.raw_content||i.classification_notes||'',220))}</div><div class="mono">${esc(i.id)}</div></td><td><span class="badge ${statusClass}">${esc(i.status)}</span> <span class="badge">${esc(action)}</span>${advice?` <span class="badge ok">LLM: ${esc(advice.recommended_action||'review')} · ${Number(advice.confidence||0).toFixed(2)}</span>`:''}<div class="muted">${esc(short(i.classification_notes||'',120))}</div></td><td><span class="badge">${esc(suggestion.knowledge_kind||i.knowledge_object?.knowledge_kind||'note')}</span><div>${tags.slice(0,7).map(t=>`<span class="badge">${esc(t)}</span>`).join(' ')||'—'}</div><div class="muted">Сущностей предложено: ${entities.length}</div></td><td><div>promotion <b>${Number(i.promotion_score||0).toFixed(2)}</b></div><div>quality <b>${Number(i.quality_score||0).toFixed(2)}</b></div></td><td>${fmtDate(raw.received_at||i.created_at)}</td><td><button class="btn small primary" ${call('reviewInbox',i.id)}>Разобрать</button> <button class="btn small danger" ${call('classifyInbox',i.id,'ignored',false)}>Игнорировать</button></td></tr>`;
  });
  setApp(gen,`${groupsBlock}<div class="notice">Friday не превращает пограничные сообщения в долгосрочное знание молча. Здесь можно проверить предложенные заголовок, краткое содержание, тип, теги и сущности до продвижения.</div><section class="card"><div class="toolbar"><h2 class="grow">Inbox пользователя</h2><button class="btn" ${call('importDialog')}>Импорт (ICS/закладки)</button><button class="btn" ${call('selectAllInbox',true)}>Выбрать все</button><button class="btn" ${call('selectAllInbox',false)}>Снять</button><button class="btn" ${call('bulkInbox','archived',false)}>Архив Inbox</button><button class="btn danger" ${call('bulkInbox','ignored',false)}>Игнорировать</button><span class="badge">${countBadge(rows.length,data.total)}</span></div>${rows.length?table(['','Содержимое','Решение системы','Предлагаемая структура','Оценки','Получен','Действия'],rows):empty('Очередь пуста')}${pager('inboxPage',state.inboxOffset,state.inbox.length,data.total)}</section>`);
};
actions.reviewInbox=id=>{
  const i=state.inbox.find(item=>item.id===id);
  if(!i)return;
  const s=i.suggestions||parse(i.suggestions_json,{});
  const raw=i.raw_object||{};
  const tags=Array.isArray(s.tags)?s.tags:(i.suggested_tags||parse(i.suggested_tags_json,[]));
  const entities=Array.isArray(s.entities)?s.entities:[];
  const advice=s.model_advice&&typeof s.model_advice==='object'?s.model_advice:null;
  const promoted=Boolean(i.knowledge_object_id);
  const adviceBlock=advice?`<div class="notice"><b>Совет локальной модели (${esc(advice.model||'local')}):</b> ${esc(advice.recommended_action||'review')} · уверенность ${Number(advice.confidence||0).toFixed(2)}<br>${esc(advice.rationale||'')}<div class="muted">Только рекомендация: модель не продвигает материал и не создаёт сущности автоматически.</div></div>`:'<div class="muted">Совет локальной модели ещё не запрашивался.</div>';
  openModal('Разбор входящего материала',`<div class="form"><div class="notice">${promoted?'Knowledge Object уже создан, но структура или связи требуют проверки.':'Объект пока хранится только как Raw Object. Продвижение создаст версионируемый Knowledge Object с provenance.'}</div>${adviceBlock}<label>Оригинал<div class="pre">${esc(raw.raw_content||'')}</div></label><label>Заголовок<input id="inboxTitle" value="${esc(s.title||i.knowledge_object?.title||'')}"></label><label>Краткое содержание<textarea id="inboxSummary">${esc(s.summary||i.knowledge_object?.summary||'')}</textarea></label><div class="grid two"><label>Тип знания<select id="inboxKind" class="field">${['note','fact','decision','preference','task','event','project','procedure','contact','reference','idea','technical_note','document'].map(v=>`<option value="${v}" ${(s.knowledge_kind||i.knowledge_object?.knowledge_kind||'note')===v?'selected':''}>${v}</option>`).join('')}</select></label><label>Важность (0–1)<input id="inboxImportance" type="number" min="0" max="1" step="0.05" value="${esc(s.importance??i.knowledge_object?.importance??0.5)}"></label></div><label>Теги через запятую<input id="inboxTags" value="${esc(tags.join(', '))}"></label><label>Предложенные сущности<div>${entities.length?entities.map(e=>`<span class="badge ${Number(e.confidence||0)>=.88?'ok':'warn'}">${esc(e.name)} · ${esc(e.entity_type)} · ${Number(e.confidence||0).toFixed(2)}${e.method==='local_model_advice'?' · LLM':''}</span>`).join(' '):'<span class="muted">Нет уверенных предложений</span>'}</div></label><label>Комментарий проверяющего<textarea id="inboxNotes" placeholder="Необязательно"></textarea></label></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn" ${call('adviseInbox',i.id)}>${advice?'Обновить совет LLM':'Уточнить локальной моделью'}</button><button class="btn" ${call('classifyInbox',i.id,'archived',false)}>В архив Inbox</button><button class="btn primary" ${call('promoteInbox',i.id)}>${promoted?'Подтвердить структуру':'Продвинуть в знания'}</button>`);
};
actions.promoteInbox=async id=>{
  try{
    const payload={user_id:selectedUser(),status:'classified',promote:true,title:document.getElementById('inboxTitle').value,summary:document.getElementById('inboxSummary').value,knowledge_kind:document.getElementById('inboxKind').value,importance:Number(document.getElementById('inboxImportance').value),tags:document.getElementById('inboxTags').value.split(',').map(v=>v.trim()).filter(Boolean),notes:document.getElementById('inboxNotes').value};
    await api(`/api/admin/inbox/${q(id)}/classify`,{method:'POST',body:JSON.stringify(payload)});
    closeModal();toast('Материал проверен и сохранён как знание');refresh();
  }catch(e){toast(e.message,true)}
};
actions.classifyInbox=async(id,status,promote)=>{try{await api(`/api/admin/inbox/${q(id)}/classify`,{method:'POST',body:JSON.stringify({user_id:selectedUser(),status,promote})});closeModal();toast('Статус обновлён');refresh()}catch(e){toast(e.message,true)}};
actions.adviseInbox=async id=>{try{await api(`/api/admin/inbox/${q(id)}/advise`,{method:'POST',body:JSON.stringify({user_id:selectedUser(),force:true})});closeModal();toast('Локальная модель обновила только предложение для проверки');await refresh();actions.reviewInbox(id)}catch(e){toast(e.message,true)}};
actions.importDialog=()=>openModal('Импорт в Inbox',`<div class="form"><div class="notice">Календарь (.ics), экспорт закладок браузера (.html), почтовый архив (.mbox) или письмо (.eml). Каждый элемент попадёт во входящие на проверку — ничего не станет знанием без вашего подтверждения. Повторный импорт того же файла безопасен (дубликаты пропускаются).</div><label>Файл<input id="importFile" type="file" accept=".ics,.html,.htm,.mbox,.eml"></label></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn primary" ${call('runImport')}>Импортировать</button>`);
actions.runImport=async()=>{const input=document.getElementById('importFile');const file=input&&input.files&&input.files[0];if(!file){toast('Выберите файл',true);return}try{const form=new FormData();form.append('file',file);const d=await api('/api/import',{method:'POST',body:form});toast(`Импорт (${d.kind}): в Inbox ${d.queued_for_review}, уже было ${d.already_imported}${d.failed?`, ошибок ${d.failed}`:''}${d.truncated?`, обрезано ${d.truncated}`:''}`);closeModal();refresh()}catch(e){toast(e.message,true)}};
actions.selectAllInbox=value=>document.querySelectorAll('.inbox-check').forEach(el=>{el.checked=value});
const BULK_BATCH=200;
// Every bulk route refuses more than 200 ids, and it refuses BEFORE writing anything:
// an unbatched «выбрать все» over a real backlog applied nothing at all and showed the
// server's English refusal in a toast. Four of the five actions were unbatched, and the
// worst of them — очистка legacy — had already collected the user's confirmation for a
// soft delete before doing nothing.
// Partial progress is reported rather than discarded: batches that completed before a
// failure are committed on the server, so the count is real and the list must be
// re-read either way — stale rows on screen invite the same action twice.
const bulkApplied=d=>Array.isArray(d.changed)?d.changed.length:Number(d.changed_count||0);
async function bulkApply(ids,send){let ok=0,skipped=0,error=null;
  for(let i=0;i<ids.length;i+=BULK_BATCH){
    try{const d=await send(ids.slice(i,i+BULK_BATCH));ok+=bulkApplied(d);skipped+=(d.skipped||[]).length}
    catch(e){error=e;break}}
  // Сначала отчёт, потом перечитывание: результат ЗАПИСИ не должен зависеть от
  // успеха постороннего чтения — при зависшем refresh пользователь иначе не узнает
  // ни что применилось, ни что сломалось.
  if(error)toast(`Применено ${ok} из ${ids.length}, дальше сбой: ${error.message}`,true);
  else toast(`Применено: ${ok}; пропущено: ${skipped}`);
  await refresh();
}
actions.bulkInbox=async(status,promote)=>{state.inboxOffset=0;const ids=[...document.querySelectorAll('.inbox-check:checked')].map(el=>el.value);if(!ids.length){toast('Сначала выберите материалы',true);return}const verb=status==='classified'?'продвинуть':status==='ignored'?'игнорировать':'архивировать';const linked=status==='ignored'?(state.inbox||[]).filter(i=>ids.includes(i.id)&&i.knowledge_object_id).length:0;const warn=linked?`\n\nИз них ${linked} уже стали знаниями — они будут удалены из поиска.`:'';if(!confirm(`${verb[0].toUpperCase()+verb.slice(1)} ${ids.length} материалов?${warn}`))return;await bulkApply(ids,batch=>api('/api/admin/inbox/bulk',{method:'POST',body:JSON.stringify({user_id:selectedUser(),inbox_ids:batch,status,promote,notes:'admin UI bulk review'})}))};
renderers.knowledge=async gen=>{
  const uid=selectedUser();
  const tagFilter=state.knowledgeTag?`&tag=${q(state.knowledgeTag)}`:'';
  // Строка поиска — единственный работающий способ найти документ руками. Замерено на
  // настоящем корпусе: важность лежит в полосе 0.66..0.72, различных дней в updated_at
  // три на 1537 объектов, а два служебных тега стоят на 1524 из них. То есть и порядок,
  // и чипы тегов вырождены, и без поиска остаётся листание полутора тысяч строк.
  const search=state.knowledgeQuery?`&q=${q(state.knowledgeQuery)}`:'';
  // Диапазон дат, УПОМЯНУТЫХ в документе. Тот же приём, что на экране «Активность»,
  // где фильтр по периоду уже есть, — человеку не надо учить второй способ.
  const period=(state.knowledgeSince?`&since=${q(state.knowledgeSince)}`:'')+(state.knowledgeUntil?`&until=${q(state.knowledgeUntil)}`:'');
  const [data,tagData]=await Promise.all([
    api(`/api/admin/knowledge?user_id=${q(uid)}&limit=${PAGE}&offset=${state.knowledgeOffset}${tagFilter}${search}${period}`),
    api(`/api/admin/knowledge/tags?user_id=${q(uid)}&limit=40`)
  ]);if(gen!==renderGen)return;
  state.knowledge=data.items||[];
  const tags=tagData.items||[];
  const chips=tags.map(t=>`<button class="btn small${state.knowledgeTag===t.tag?' primary':''}" ${call('filterKnowledgeTag',t.tag)}>#${esc(t.tag)} · ${Number(t.count||0)}</button>`).join(' ');
  const rows=state.knowledge.map(k=>{const ktags=parse(k.tags_json,[]);return `<tr><td><b>${esc(k.title||'Без названия')}</b><div class="muted clip">${esc(short(k.summary||k.content,180))}</div><div>${ktags.slice(0,6).map(t=>`<span class="badge">${esc(t)}</span>`).join(' ')}</div><div class="mono">${esc(k.id)}</div></td><td><span class="badge">${esc(k.knowledge_kind||'note')}</span><div><span class="badge ${k.lifecycle_stage==='active'?'ok':'warn'}">${esc(k.lifecycle_stage)}</span></div></td><td><div>важность <b>${Number(k.importance||0).toFixed(2)}</b></div><div>качество <b>${Number(k.quality_score??.5).toFixed(2)}</b></div><div>promotion <b>${Number(k.promotion_score??.5).toFixed(2)}</b></div></td><td>v${esc(k.version)}<div class="muted">${fmtDate(k.updated_at)}</div></td><td><button class="btn small primary" ${call('inspectKnowledge',k.id)}>Инспекция</button> <button class="btn small" ${call('editKnowledge',k.id)}>Исправить</button> <button class="btn small danger" ${call('deleteKnowledge',k.id)}>Убрать из списка</button></td></tr>`});
  const filterNote=state.knowledgeTag?`<div class="notice">Фильтр по тегу <b>#${esc(state.knowledgeTag)}</b> — нажмите тег ещё раз, чтобы снять.</div>`:'';
  const searchBar=`<div class="toolbar"><input id="knowledgeSearch" class="grow" placeholder="Поиск по заголовку, аннотации и имени файла" value="${esc(state.knowledgeQuery||'')}">`
    +`<button class="btn primary" ${call('searchKnowledge')}>Найти</button>`
    +(state.knowledgeQuery?`<button class="btn" ${call('clearKnowledgeSearch')}>Сбросить</button>`:'')
    +`<button class="btn" ${call('sourceSearchDialog')}>Найти по тексту документов</button>`
    +`</div>`
    +`<div class="toolbar"><span class="muted">Даты в документе:</span>`
    +`<input id="knowledgeSince" type="date" value="${esc(state.knowledgeSince||'')}">`
    +`<span class="muted">—</span>`
    +`<input id="knowledgeUntil" type="date" value="${esc(state.knowledgeUntil||'')}">`
    +`<button class="btn" ${call('applyKnowledgePeriod')}>Применить</button>`
    +((state.knowledgeSince||state.knowledgeUntil)?`<button class="btn" ${call('clearKnowledgePeriod')}>Снять</button>`:'')
    +`</div>`
    +(state.knowledgeQuery?`<div class="notice">Найдено по «${esc(state.knowledgeQuery)}»: <b>${Number(data.total||0)}</b>. Это поиск по НАЗВАНИЯМ. Если помните фразу из самого документа — «Найти по тексту документов».</div>`:'');
  setApp(gen,`${filterNote}<section class="card"><div class="toolbar"><h2 class="grow">Объекты знаний</h2><button class="btn" ${call('ingestUrlDialog')}>Сохранить страницу по URL</button><button class="btn" ${call('bookmarkletDialog')}>Букмарклет</button><button class="btn" ${call('runLifecycle')}>Архивировать устаревшее</button><button class="btn" ${call('navigate','cleanup')}>Проверить старые данные</button><span class="badge">${countBadge(rows.length,data.total)}</span></div>${searchBar}${chips?`<div class="toolbar">${chips}</div>`:''}${rows.length?table(['Знание','Тип и lifecycle','Сигналы качества','Версия','Действия'],rows):empty(state.knowledgeTag?'По этому тегу записей нет':'База знаний пока пуста')}${pager('knowledgePage',state.knowledgeOffset,state.knowledge.length,data.total)}</section>`);
};
actions.ingestUrlDialog=(pref)=>openModal('Сохранить веб-страницу',`<div class="form"><div class="notice">Страница будет загружена (только публичные адреса), очищена и отправлена во входящие на проверку — как и любой материал, она станет знанием лишь после подтверждения.</div><label>URL<input id="ingestUrl" value="${esc(pref||'')}" placeholder="https://…"></label></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn primary" ${call('ingestUrl')}>Загрузить в Inbox</button>`);
actions.ingestUrl=async()=>{const url=document.getElementById('ingestUrl').value.trim();if(!url){toast('Укажите URL',true);return}try{const d=await api('/api/ingest/url',{method:'POST',body:JSON.stringify({url})});toast(`«${d.title||url}» сохранена во входящие`);closeModal();navigate('inbox')}catch(e){toast(e.message,true)}};
actions.bookmarkletDialog=()=>{
  // The bookmarklet scheme is split across a concat so the CSP-invariant grep
  // stays clean: this string runs on OTHER pages, not on this locked-down one.
  const bm='java'+'script:(function(){var u=encodeURIComponent(location.href),t=encodeURIComponent(document.title);open("'+location.origin+'/admin/#save="+u+"&title="+t)})()';
  openModal('Букмарклет «Сохранить в Friday»',`<div class="form"><div class="notice">Перетащите ссылку ниже на панель закладок браузера. На любой странице нажмите её — страница откроется в Friday и попадёт во входящие на проверку. Токен в закладке не хранится: сохранение выполняется уже в открытом Friday, поэтому первый раз может понадобиться ввести ключ.</div><p><a id="bmLink" class="btn primary">＋ Сохранить в Friday</a></p><label>Или создайте закладку вручную и вставьте это как её адрес:<textarea id="bmCode" class="mono" readonly></textarea></label></div>`,`<button class="btn" ${call('closeModal')}>Закрыть</button>`);
  const l=document.getElementById('bmLink');if(l)l.href=bm;
  const c=document.getElementById('bmCode');if(c){c.value=bm;c.addEventListener('focus',()=>c.select())}
};
// Подтверждённый дубликат нигде не показывался, и узнать его идентификатор было
// неоткуда — то есть «Подтвердить» означало «спрятать навсегда». При том что разрешить
// такой конфликт технически по-прежнему можно.
// Сторона, уже погашенная соседним решением, выглядела равноправным кандидатом.
// Замерено: 207 пар дубликатов складываются в 126 кластеров — 19 троек, 7 четвёрок и
// 3 пятёрки, — то есть больше половины пар решаются не поодиночке.
function sideNote(stage,supersededBy){
  if(supersededBy)return '<div><span class="badge bad">уже погашен</span></div>';
  if(stage&&stage!=='active')return `<div><span class="badge warn">${esc(stage)}</span></div>`;
  return '';
}
actions.filterConflictStatus=st=>{state.conflictStatus=st;state.conflictsOffset=0;refresh()};
// Отклонённая связь остаётся в базе со статусом rejected — до этой кнопки посмотреть
// её из админки было нечем, и массовое «Отклонить» выглядело безвозвратным.
actions.filterRelationStatus=st=>{state.relationStatus=st;state.relationsOffset=0;refresh()};
actions.filterKnowledgeTag=tag=>{state.knowledgeTag=state.knowledgeTag===tag?'':tag;state.knowledgeOffset=0;refresh()};
// Смена запроса ВСЕГДА сбрасывает смещение: иначе человек ищет и попадает на
// четвёртую страницу нового набора, где обычно пусто, и решает, что не нашлось.
// Дословный поиск по ИСХОДНОМУ тексту. Отдельной кнопкой, а не вторым режимом той же
// строки, потому что это другой вопрос: там «как называется», здесь «где эта фраза».
// Замерено: 93% загруженных знаков живут только в raw_objects, то есть точная фраза из
// документа иначе не находится вовсе.
actions.sourceSearchDialog=()=>openModal('Поиск по тексту документов',
  `<div class="form"><div class="notice">Ищет дословно по исходному тексту загруженного материала — мимо ранжирования. Отклонённое во входящих сюда не входит: это решение, а не фильтр.</div>`
  +`<label>Фраза<input id="sourceQuery" placeholder="например, номер приказа или фамилия"></label>`
  +`<div id="sourceResults"></div></div>`,
  `<button class="btn" ${call('closeModal')}>Закрыть</button><button class="btn primary" ${call('runSourceSearch')}>Найти</button>`);
actions.runSourceSearch=async()=>{
  const el=document.getElementById('sourceQuery');const text=el?el.value.trim():'';
  const box=document.getElementById('sourceResults');if(!box)return;
  if(!text){box.innerHTML=empty('Введите фразу');return}
  box.innerHTML='<div class="muted">Ищу…</div>';
  try{
    const d=await api(`/api/admin/source-search?user_id=${q(selectedUser())}&q=${q(text)}&limit=25`);
    const items=d.items||[];
    if(!items.length){box.innerHTML=empty('Ничего не найдено в доступном исходном тексте');return}
    box.innerHTML=`<div class="notice">Показано ${items.length} совпадений (не более 25). Это страница, а не полное число.</div>`
      +table(['Материал','Где','Действие'],items.map(it=>{
        const marker=it.knowledge_object_id?'знание':(it.inbox_status||'—');
        const act=it.knowledge_object_id
          ?`<button class="btn small primary" ${call('inspectKnowledge',it.knowledge_object_id)}>Инспекция</button>`
          :'<span class="muted">ещё во входящих</span>';
        return `<tr><td><div class="muted clip">…${esc(short(it.excerpt||'',220))}…</div><div class="mono">${esc(it.source_ref||it.id)}</div></td>`
          +`<td><span class="badge">${esc(marker)}</span><div class="muted">${esc(it.source||'')}</div></td><td>${act}</td></tr>`}));
  }catch(e){box.innerHTML=empty(e.message)}
};
actions.searchKnowledge=()=>{const el=document.getElementById('knowledgeSearch');state.knowledgeQuery=el?el.value.trim():'';state.knowledgeOffset=0;refresh()};
actions.applyKnowledgePeriod=()=>{
  const a=document.getElementById('knowledgeSince'),b=document.getElementById('knowledgeUntil');
  state.knowledgeSince=a?a.value:'';state.knowledgeUntil=b?b.value:'';state.knowledgeOffset=0;refresh()};
actions.clearKnowledgePeriod=()=>{state.knowledgeSince='';state.knowledgeUntil='';state.knowledgeOffset=0;refresh()};
actions.clearKnowledgeSearch=()=>{state.knowledgeQuery='';state.knowledgeOffset=0;refresh()};
// История версий со СВОЕЙ кнопкой отката. Раньше версии показывались строкой JSON
// внутри «Метаданные и история версий» — то есть их было видно и нельзя было ничего с
// ними сделать. А редактор содержимого здесь это одна textarea с полным текстом
// документа, в среднем на 16.5 тысяч знаков: первая же настоящая ошибка упиралась в
// тупик.
function versionRows(id,versions){
  if(!versions.length)return empty('Правок не было');
  return table(['Версия','Когда','Действие'],versions.map(v=>
    `<tr><td><b>${Number(v.version)}</b></td><td class="muted">${fmtDate(v.created_at)}</td>`
    +`<td><button class="btn small" ${call('restoreVersion',id,v.version)}>Вернуть это состояние</button></td></tr>`));
}
actions.restoreVersion=async(id,version)=>{
  // Спрашиваем, потому что откат меняет содержимое документа. Но пугать не за что, и
  // это сказано прямо: откат создаёт НОВУЮ версию, вернуться обратно можно.
  if(!confirm(`Вернуть документ к версии ${version}? Текущее состояние останется в истории — откатиться обратно можно будет так же.`))return;
  try{
    await api(`/api/admin/knowledge/${q(id)}/restore`,{method:'POST',
      body:JSON.stringify({user_id:selectedUser(),version:Number(version)})});
    toast(`Возвращено к версии ${version}`);closeModal();refresh();
  }catch(e){toast(e.message,true)}
};
// Текст документа с подсветкой упоминаний. Куски экранируются ПООТДЕЛЬНОСТИ и
// только потом склеиваются: собрать строку и экранировать целиком нельзя — тогда
// вместе с текстом экранируется и сама разметка.
// Смещения приходят в КОДОВЫХ ТОЧКАХ — так их считает Python, — а строка в
// JavaScript адресуется в единицах UTF-16. Один эмодзи или иероглиф вне BMP в
// начале документа, и всякая подсветка после него уезжает на знак: на «Отчёт 😀
// подписал Иванов» сервер отдаёт 17..23, а body.slice(17,23) даёт « Ивано».
// Это тот же класс, который сам модуль позиций закрывает свёрткой, сохраняющей
// длину, — только там граница Python↔Python, а здесь Python↔браузер, и единица
// измерения меняется вместе с языком. Поэтому режем массив кодовых точек.
function highlightMentions(text,spans){
  const chars=[...String(text||'')];
  const list=(spans||[]).filter(s=>Number.isInteger(s.start)&&Number.isInteger(s.end)&&s.end>s.start).sort((a,b)=>a.start-b.start);
  let out='',cursor=0;
  for(const span of list){
    if(span.start<cursor)continue;
    out+=esc(chars.slice(cursor,span.start).join(''));
    out+=`<mark title="${esc(span.name)}">${esc(chars.slice(span.start,span.end).join(''))}</mark>`;
    cursor=span.end;
  }
  return out+esc(chars.slice(cursor).join(''));
}
actions.inspectKnowledge=async id=>{
  try{
    const d=await api(`/api/admin/knowledge/${q(id)}?user_id=${q(selectedUser())}`);
    state.inspectedKnowledge=d;
    const k=d.item||{};const raw=d.raw_object||{};const links=d.entity_links||[];
    // Кандидаты в сущности, которых в графе ещё нет. Считаются по запросу из текста:
    // нигде не хранились, а автоматически создаются только при уверенности >= 0.88,
    // тогда как два метода, дающие на реальном корпусе почти всё, стоят на 0.75-0.76.
    // Без этого списка цепочка «извлекли -> человек подтвердил -> нашлась связь»
    // была разомкнута на первом звене.
    let sugg={items:[]};
    try{sugg=await api(`/api/admin/knowledge/${q(id)}/entity-suggestions?user_id=${q(selectedUser())}`)}catch(e){sugg={items:[],error:e.message}}
    // Подсветка подтверждённых сущностей в самом тексте. Позиции считает сервер:
    // там живёт свёртка, сохраняющая длину, и её тесты — на лигатурах смещения
    // уже однажды уезжали на соседние слова.
    let mentions={items:[]};
    try{mentions=await api(`/api/admin/knowledge/${q(id)}/entity-mentions?user_id=${q(selectedUser())}`)}catch(e){mentions={items:[],error:e.message}}
    state.entitySuggestions=sugg.items||[];
    const suggRows=(sugg.items||[]).map(sg=>`<tr><td><b>${esc(sg.name)}</b><div class="muted">${esc(sg.method||'')}</div></td><td>${esc(sg.entity_type||'other')}</td><td>${Number(sg.confidence||0).toFixed(2)}</td><td><button class="btn small primary" ${call('acceptEntity',id,sg.name,sg.entity_type||'other')}>Подтвердить</button></td></tr>`);
    const suggBlock=sugg.error?`<div class="notice">Подсказки недоступны: ${esc(sugg.error)}</div>`
      :(suggRows.length?`<section class="card"><h3>Предложенные сущности (${suggRows.length})</h3><div class="notice">Подтверждение создаёт узел графа и утверждённую связь с этим документом, после чего пересчитываются связи сущность-сущность.</div>${table(['Имя','Тип','Уверенность',''],suggRows)}</section>`
      :`<section class="card"><h3>Предложенные сущности</h3>${empty('Все кандидаты уже разобраны')}</section>`);
    const linkRows=links.map(l=>`<tr><td><b>${esc(l.entity?.name||l.entity_name||l.entity_id)}</b><div class="muted">${esc(l.entity?.entity_type||l.entity_type||'')}</div></td><td><span class="badge ${l.status==='accepted'?'ok':(l.status==='rejected'?'bad':'warn')}">${esc(l.status)}</span></td><td>${Number(l.confidence||0).toFixed(2)}</td><td>${l.status!=='accepted'?`<button class="btn small good" ${call('reviewEntityLink',l.id,'accepted',k.id)}>Принять</button>`:''} ${l.status!=='rejected'?`<button class="btn small danger" ${call('reviewEntityLink',l.id,'rejected',k.id)}>Отклонить</button>`:''}</td></tr>`);
    openModal(`Инспекция: ${k.title||k.id}`,`${suggBlock}<div class="grid two"><section class="card"><h3>Knowledge Object</h3><div class="kv"><div>Тип</div><div>${esc(k.knowledge_kind)}</div><div>Lifecycle</div><div>${esc(k.lifecycle_stage)}</div><div>Качество</div><div>${Number(k.quality_score??.5).toFixed(2)}</div><div>Promotion</div><div>${Number(k.promotion_score??.5).toFixed(2)}</div><div>Версия</div><div>${esc(k.version)}</div></div><h3 class="mt16">Summary</h3><div class="pre">${esc(k.summary||'')}</div></section><section class="card"><h3>Provenance / Raw Object</h3><div class="kv"><div>Источник</div><div>${esc(raw.source||'')}</div><div>Source ref</div><div class="mono">${esc(raw.source_ref||'')}</div><div>Raw ID</div><div class="mono">${esc(raw.id||'')}</div><div>Получен</div><div>${fmtDate(raw.received_at)}</div></div><div class="pre mt12">${esc(raw.raw_content||'')}</div></section></div><section class="card mt14"><div class="toolbar"><h3 class="grow">Текст с подсветкой сущностей</h3><span class="badge">${Number(mentions.count||0)}</span></div>${mentions.error?`<div class="notice">Подсветка недоступна: ${esc(mentions.error)}</div>`:((mentions.items||[]).length?`<div class="notice">Отмечены только ПОДТВЕРЖДЁННЫЕ сущности: предложенные не подсвечиваются, чтобы догадка не выглядела как решение человека.${mentions.truncated?' Показаны первые 500 упоминаний.':''}</div><div class="pre">${highlightMentions(k.content||'',mentions.items)}</div>`:empty('Подтверждённых сущностей в этом документе нет — подтвердите кандидатов выше, и они появятся в тексте'))}</section><section class="card mt14"><h3>Связи с сущностями</h3>${linkRows.length?table(['Сущность','Статус','Уверенность','Решение'],linkRows):empty('Связей пока нет')}<div class="toolbar mt12"><button class="btn" ${call('addEntityLinkDialog',k.id)}>Добавить связь</button></div></section><section class="card mt14"><h3>История версий</h3>${versionRows(k.id,d.versions||[])}</section><section class="card mt14"><h3>Метаданные</h3><div class="pre">${esc(JSON.stringify({metadata:k.metadata,tags:k.tags},null,2))}</div></section>`,`${(d.versions||[]).length>1?`<button class="btn" ${call('showDiff',k.id)}>Изменения версий</button>`:''}<button class="btn" ${call('reenrichKnowledge',k.id,false)}>Предпросмотр enrichment</button><button class="btn" ${call('editKnowledge',k.id)}>Исправить</button><button class="btn" ${call('closeModal')}>Закрыть</button>`);
  }catch(e){toast(e.message,true)}
};
actions.acceptEntity=async(koId,name,entityType)=>{
  try{
    const out=await api(`/api/admin/knowledge/${q(koId)}/entities`,{method:'POST',body:JSON.stringify({user_id:selectedUser(),name,entity_type:entityType})});
    const made=(out.relation_candidates||[]).length;
    toast(`Сущность подтверждена${out.entity_created?' (узел создан)':''}${made?`, предложено связей: ${made}`:''}`);
    await actions.inspectKnowledge(koId);
  }catch(e){toast(e.message,true)}
};
function renderDiffChanges(changes){const keys=Object.keys(changes);if(!keys.length)return `<div class="notice">Между этими версиями изменений нет.</div>`;return keys.map(field=>{const c=changes[field];if(c.kind==='scalar'||field==='superseded_by_id')return `<div class="card"><b>${esc(field)}</b><div class="kv mt8"><div>было</div><div class="mono">${esc(String(c.from??'—'))}</div><div>стало</div><div class="mono">${esc(String(c.to??'—'))}</div></div></div>`;if(c.kind==='text')return `<div class="card"><b>${esc(field)}</b>${c.unified?`<div class="pre mt8">${esc(c.unified)}</div>`:`<div class="grid two mt8"><div><div class="muted">было</div><div class="pre">${esc(c.from||'')}</div></div><div><div class="muted">стало</div><div class="pre">${esc(c.to||'')}</div></div></div>`}</div>`;if(c.kind==='set')return `<div class="card"><b>Теги</b><div class="mt8">${(c.added||[]).map(t=>`<span class="badge ok">+${esc(t)}</span>`).join(' ')} ${(c.removed||[]).map(t=>`<span class="badge bad">−${esc(t)}</span>`).join(' ')||''}</div></div>`;if(c.kind==='map'){const rows=[];Object.entries(c.added||{}).forEach(([kk,vv])=>rows.push(`<span class="badge ok">+${esc(kk)}</span>`));Object.entries(c.removed||{}).forEach(([kk])=>rows.push(`<span class="badge bad">−${esc(kk)}</span>`));Object.entries(c.changed||{}).forEach(([kk])=>rows.push(`<span class="badge warn">~${esc(kk)}</span>`));return `<div class="card"><b>Метаданные</b><div class="mt8">${rows.join(' ')}</div><details class="mt8"><summary>Детали</summary><div class="pre">${esc(JSON.stringify(c,null,2))}</div></details></div>`}return ''}).join('')}
actions.showDiff=async(id,from,to)=>{try{const params=[`user_id=${q(selectedUser())}`];if(from)params.push(`from_version=${from}`);if(to)params.push(`to_version=${to}`);const d=await api(`/api/admin/knowledge/${q(id)}/diff?${params.join('&')}`);const av=d.available_versions||[];const opt=(sel)=>av.map(v=>`<option value="${v}" ${v===sel?'selected':''}>v${v}</option>`).join('');openModal(`Изменения: v${d.from_version} → v${d.to_version}`,`<div class="toolbar"><label>с <select id="diffFrom" class="field" ${chg('diffPick',id)}>${opt(d.from_version)}</select></label><label>на <select id="diffTo" class="field" ${chg('diffPick',id)}>${opt(d.to_version)}</select></label></div><div class="grid mt12">${renderDiffChanges(d.changes||{})}</div>`,`<button class="btn" ${call('closeModal')}>Закрыть</button>`)}catch(e){toast(e.message,true)}};
actions.diffPick=id=>{const from=document.getElementById('diffFrom').value;const to=document.getElementById('diffTo').value;actions.showDiff(id,from,to)};
actions.editKnowledge=id=>{
  const k=state.knowledge.find(item=>item.id===id)||state.inspectedKnowledge?.item;
  if(!k)return;
  const tags=parse(k.tags_json,[]);
  openModal('Редактирование знания',`<div class="form"><label>Заголовок<input id="koTitle" value="${esc(k.title)}"></label><label>Краткое описание<textarea id="koSummary">${esc(k.summary)}</textarea></label><label>Содержимое<textarea id="koContent" class="minh230">${esc(k.content)}</textarea></label><label>Теги через запятую<input id="koTags" value="${esc(tags.join(', '))}"></label><div class="grid two"><label>Тип знания<input id="koKind" value="${esc(k.knowledge_kind||'note')}"></label><label>Важность (0–1)<input id="koImportance" type="number" min="0" max="1" step="0.05" value="${esc(k.importance)}"></label><label>Качество (0–1)<input id="koQuality" type="number" min="0" max="1" step="0.05" value="${esc(k.quality_score??.5)}"></label><label>Стадия<select id="koLifecycle" class="field">${['active','archived','deprecated'].map(v=>`<option value="${v}" ${k.lifecycle_stage===v?'selected':''}>${v}</option>`).join('')}</select></label></div></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn primary" ${call('saveKnowledge',k.id)}>Сохранить новую версию</button>`);
};
actions.saveKnowledge=async id=>{try{await api(`/api/admin/knowledge/${q(id)}`,{method:'PATCH',body:JSON.stringify({user_id:selectedUser(),title:document.getElementById('koTitle').value,summary:document.getElementById('koSummary').value,content:document.getElementById('koContent').value,tags_json:document.getElementById('koTags').value.split(',').map(v=>v.trim()).filter(Boolean),knowledge_kind:document.getElementById('koKind').value,importance:Number(document.getElementById('koImportance').value),quality_score:Number(document.getElementById('koQuality').value),lifecycle_stage:document.getElementById('koLifecycle').value})});closeModal();toast('Создана новая версия');refresh()}catch(e){toast(e.message,true)}};
actions.reenrichKnowledge=async(id,apply)=>{try{if(apply&&!confirm('Применить предложенные title, summary, tags, type и entity links новой версией?'))return;const d=await api(`/api/admin/knowledge/${q(id)}/reenrich`,{method:'POST',body:JSON.stringify({user_id:selectedUser(),apply})});if(apply){closeModal();toast('Enrichment применён новой версией');refresh();return}const s=d.suggestion||{};const a=d.assessment||{};openModal('Предпросмотр enrichment',`<div class="notice">Решение политики: <b>${esc(a.assessment?.action||'unknown')}</b>; risk ${Number(a.risk_score||0).toFixed(2)}. Применение не выполняется автоматически.</div><div class="form"><label>Предлагаемый заголовок<div class="pre">${esc(s.title||'')}</div></label><label>Предлагаемое summary<div class="pre">${esc(s.summary||'')}</div></label><label>Тип / важность / качество<div>${esc(s.knowledge_kind||'note')} · ${Number(s.importance||0).toFixed(2)} · ${Number(s.quality_score||0).toFixed(2)}</div></label><label>Теги<div>${(s.tags||[]).map(t=>`<span class="badge">${esc(t)}</span>`).join(' ')||'—'}</div></label><label>Сущности<div>${(s.entities||[]).map(e=>`<span class="badge">${esc(e.name)} · ${Number(e.confidence||0).toFixed(2)}</span>`).join(' ')||'—'}</div></label></div>`,`<button class="btn" ${call('closeModal')}>Закрыть</button><button class="btn primary" ${call('reenrichKnowledge',id,true)}>Применить новой версией</button>`)}catch(e){toast(e.message,true)}};
actions.reviewEntityLink=async(linkId,status,knowledgeId)=>{try{await api(`/api/admin/entity-links/${q(linkId)}`,{method:'PATCH',body:JSON.stringify({user_id:selectedUser(),status})});toast('Связь проверена');await actions.inspectKnowledge(knowledgeId)}catch(e){toast(e.message,true)}};
actions.addEntityLinkDialog=async knowledgeId=>{try{const d=await api(`/api/admin/entities?user_id=${q(selectedUser())}&limit=5000`);const items=d.items||[];openModal('Добавить связь с сущностью',`<div class="form"><label>Сущность<select id="linkEntity" class="field">${items.map(e=>`<option value="${esc(e.id)}">${esc(e.name)} · ${esc(e.entity_type)}</option>`).join('')}</select></label><label>Уверенность<input id="linkConfidence" type="number" min="0" max="1" step="0.05" value="1"></label></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn primary" ${call('createEntityLink',knowledgeId)}>Связать</button>`)}catch(e){toast(e.message,true)}};
actions.createEntityLink=async knowledgeId=>{try{await api(`/api/admin/knowledge/${q(knowledgeId)}/entity-links`,{method:'POST',body:JSON.stringify({user_id:selectedUser(),entity_id:document.getElementById('linkEntity').value,confidence:Number(document.getElementById('linkConfidence').value),status:'accepted',evidence:{source:'admin_ui'}})});toast('Связь добавлена');await actions.inspectKnowledge(knowledgeId)}catch(e){toast(e.message,true)}};
actions.deleteKnowledge=async id=>{if(!confirm('Выполнить мягкое удаление объекта? Raw Object и история версий сохранятся.'))return;try{await api(`/api/admin/knowledge/${q(id)}?user_id=${q(selectedUser())}`,{method:'DELETE'});toast('Объект мягко удалён');refresh()}catch(e){toast(e.message,true)}};
actions.runLifecycle=async()=>navigate('quality');
// --- Визуализация графа: SVG и своя раскладка, без внешних зависимостей ----
//
// Библиотеку не тянем: интерфейс отдаётся локально и должен работать без сети, а на
// сотнях узлов разница с библиотекой почти вся в интерактивности (перетаскивание,
// зум, подсветка соседей) — она здесь и сделана.
//
// Рёбра ДВУХ РОДОВ и рисуются по-разному. `relation` — утверждение, которое кто-то
// подтвердил, сплошной линией. `cooccurrence` — просто встретились в одном
// документе, пунктиром, толщина по числу общих документов. Рисовать их одинаково
// значило бы выдавать наблюдение за утверждение.
const GRAPH_COLORS={person:'#e06c9f',organization:'#f4a261',project:'#7bc86c',collection:'#7bc86c',
  location:'#61a5c2',event:'#c98bdb',concept:'#8ecae6',other:'#9aa5b1'};
const GRAPH_W=1200, GRAPH_H=700;

function graphLayout(nodes,edges){
  // Стартовое положение по кругу, а не случайное: случайный старт даёт разную
  // картинку при каждом открытии, и человек не узнаёт свой же граф.
  const n=nodes.length;
  nodes.forEach((node,i)=>{const a=2*Math.PI*i/Math.max(1,n);
    node.x=GRAPH_W/2+Math.cos(a)*Math.min(GRAPH_W,GRAPH_H)*0.36;
    node.y=GRAPH_H/2+Math.sin(a)*Math.min(GRAPH_W,GRAPH_H)*0.36;node.vx=0;node.vy=0});
  const byId=new Map(nodes.map(x=>[x.id,x]));
  const links=edges.map(e=>({s:byId.get(e.source),t:byId.get(e.target),w:Math.min(4,e.weight||1)})).filter(l=>l.s&&l.t);
  for(let step=0;step<260;step++){
    const cool=1-step/260;
    for(let i=0;i<n;i++){const a=nodes[i];
      for(let j=i+1;j<n;j++){const b=nodes[j];
        let dx=a.x-b.x,dy=a.y-b.y;const d=Math.sqrt(dx*dx+dy*dy)||0.01;
        const push=3000/(d*d);dx/=d;dy/=d;
        a.vx+=dx*push;a.vy+=dy*push;b.vx-=dx*push;b.vy-=dy*push}}
    for(const l of links){let dx=l.t.x-l.s.x,dy=l.t.y-l.s.y;
      const d=Math.sqrt(dx*dx+dy*dy)||0.01;const pull=(d-110)*0.007*l.w;dx/=d;dy/=d;
      l.s.vx+=dx*pull;l.s.vy+=dy*pull;l.t.vx-=dx*pull;l.t.vy-=dy*pull}
    for(const node of nodes){node.vx+=(GRAPH_W/2-node.x)*0.0015;node.vy+=(GRAPH_H/2-node.y)*0.0015;
      node.x+=node.vx*cool;node.y+=node.vy*cool;node.vx*=0.82;node.vy*=0.82;
      node.x=Math.max(40,Math.min(GRAPH_W-40,node.x));node.y=Math.max(30,Math.min(GRAPH_H-30,node.y))}}
  return nodes;
}

function graphMarkup(data){
  const nodes=(data.nodes||[]).map(x=>({...x}));
  if(!nodes.length)return empty('В графе пока нет подтверждённых связей с документами');
  const edges=(data.edges||[]).filter(e=>e.source&&e.target);
  graphLayout(nodes,edges);
  state.graphNodes=nodes;state.graphEdges=edges;
  const byId=new Map(nodes.map(x=>[x.id,x]));
  const maxCount=Math.max(1,...nodes.map(x=>x.knowledge_count||0));
  const lines=edges.map((e,i)=>{const s=byId.get(e.source),t=byId.get(e.target);if(!s||!t)return '';
    const rel=e.kind==='relation';const w=rel?2.2:Math.min(3,0.6+(e.weight||1)*0.35);
    return `<line data-edge="${i}" data-a="${esc(e.source)}" data-b="${esc(e.target)}"`
      +` x1="${s.x.toFixed(1)}" y1="${s.y.toFixed(1)}" x2="${t.x.toFixed(1)}" y2="${t.y.toFixed(1)}"`
      +` stroke="${rel?'#4c9aff':'#5a6472'}" stroke-width="${w.toFixed(1)}"${rel?'':' stroke-dasharray="3 4"'}`
      +` opacity="${rel?0.9:0.45}"><title>${esc(rel?String(e.relation_type||'подтверждённая связь'):'общих документов: '+e.weight)}</title></line>`}).join('');
  const circles=nodes.map(node=>{const r=7+11*Math.sqrt((node.knowledge_count||0)/maxCount);
    const kind=GRAPH_COLORS[node.entity_type]?node.entity_type:'other';
    return `<g class="gnode" data-node="${esc(node.id)}">`
      +`<circle cx="${node.x.toFixed(1)}" cy="${node.y.toFixed(1)}" r="${r.toFixed(1)}" class="gfill-${kind}" stroke="#0a0f18" stroke-width="1.5">`
      +`<title>${esc(node.name)} — ${esc(node.entity_type)}, документов: ${node.knowledge_count}</title></circle>`
      +`<text x="${node.x.toFixed(1)}" y="${(node.y-r-6).toFixed(1)}" text-anchor="middle" font-size="12" fill="#c9d1d9">${esc(short(node.name,24))}</text></g>`}).join('');
  const legend=Object.entries({person:'человек',organization:'организация',project:'проект',
    location:'место',event:'событие',concept:'понятие',other:'прочее'})
    .map(([k,label])=>`<span class="badge gt-${k}">${label}</span>`).join(' ');
  // Обрез называется вслух: картинка, молча показывающая часть графа, хуже отсутствующей.
  const capped=(data.total||0)>(data.shown||0)
    ? `<div class="notice">Показано ${data.shown} сущностей из ${data.total} — самые связанные с документами. Остальные не поместились, а не отсутствуют.</div>`:'';
  return `${capped}<div class="graph-legend">${legend}<span class="muted">сплошная — подтверждённая связь, пунктир — встретились в одном документе; размер — сколько документов</span></div>
    <div class="graph-canvas" id="graphCanvas"><svg id="graphSvg" viewBox="0 0 ${GRAPH_W} ${GRAPH_H}">${lines}${circles}</svg>
    <div class="graph-hint">колесо — масштаб, тянуть фон — сдвиг, тянуть узел — переставить, клик — подробности</div></div>`;
}

// Взаимодействие вешается ПОСЛЕ отрисовки: обработчики живут на контейнере, а не на
// каждом узле, иначе их пришлось бы переставлять при каждой перерисовке.
function bindGraph(){
  const canvas=document.getElementById('graphCanvas'), svg=document.getElementById('graphSvg');
  if(!canvas||!svg)return;
  let view={x:0,y:0,k:1}, drag=null, moved=false;
  const apply=()=>svg.setAttribute('viewBox',`${view.x} ${view.y} ${GRAPH_W/view.k} ${GRAPH_H/view.k}`);
  const toSvg=event=>{const rect=canvas.getBoundingClientRect();
    return {x:view.x+(event.clientX-rect.left)/rect.width*(GRAPH_W/view.k),
            y:view.y+(event.clientY-rect.top)/rect.height*(GRAPH_H/view.k)}};
  canvas.addEventListener('wheel',event=>{event.preventDefault();
    const before=toSvg(event);
    view.k=Math.max(0.35,Math.min(6,view.k*(event.deltaY<0?1.15:1/1.15)));
    const after=toSvg(event);view.x+=before.x-after.x;view.y+=before.y-after.y;apply()},{passive:false});
  canvas.addEventListener('pointerdown',event=>{
    const group=event.target.closest('.gnode');moved=false;
    drag=group?{node:group.dataset.node,group}:{pan:true,sx:event.clientX,sy:event.clientY,vx:view.x,vy:view.y};
    canvas.classList.add('dragging');canvas.setPointerCapture(event.pointerId)});
  canvas.addEventListener('pointermove',event=>{
    if(!drag)return;moved=true;
    if(drag.pan){const rect=canvas.getBoundingClientRect();
      view.x=drag.vx-(event.clientX-drag.sx)/rect.width*(GRAPH_W/view.k);
      view.y=drag.vy-(event.clientY-drag.sy)/rect.height*(GRAPH_H/view.k);apply();return}
    const point=toSvg(event), node=(state.graphNodes||[]).find(n=>n.id===drag.node);
    if(!node)return;node.x=point.x;node.y=point.y;
    const circle=drag.group.querySelector('circle'), label=drag.group.querySelector('text');
    circle.setAttribute('cx',point.x);circle.setAttribute('cy',point.y);
    label.setAttribute('x',point.x);label.setAttribute('y',point.y-Number(circle.getAttribute('r'))-6);
    svg.querySelectorAll(`line[data-a="${CSS.escape(node.id)}"]`).forEach(line=>{
      line.setAttribute('x1',point.x);line.setAttribute('y1',point.y)});
    svg.querySelectorAll(`line[data-b="${CSS.escape(node.id)}"]`).forEach(line=>{
      line.setAttribute('x2',point.x);line.setAttribute('y2',point.y)})});
  canvas.addEventListener('pointerup',event=>{
    const wasNode=drag&&drag.node;canvas.classList.remove('dragging');drag=null;
    // Клик и перетаскивание различаются по факту движения — иначе любое смещение
    // узла открывало бы карточку, и переставить его было бы нельзя.
    if(wasNode&&!moved)actions.inspectEntityNode(wasNode)});
  // Наведение подсвечивает соседей: на плотном графе это единственный способ
  // разглядеть, с чем связан конкретный узел.
  svg.addEventListener('pointerover',event=>{
    const group=event.target.closest('.gnode');if(!group)return;
    const id=group.dataset.node, near=new Set([id]);
    svg.querySelectorAll('line').forEach(line=>{
      if(line.dataset.a===id)near.add(line.dataset.b);
      if(line.dataset.b===id)near.add(line.dataset.a)});
    svg.querySelectorAll('.gnode').forEach(other=>other.classList.toggle('gdim',!near.has(other.dataset.node)));
    svg.querySelectorAll('line').forEach(line=>
      line.classList.toggle('gdim',line.dataset.a!==id&&line.dataset.b!==id))});
  svg.addEventListener('pointerout',event=>{if(event.relatedTarget&&svg.contains(event.relatedTarget))return;
    svg.querySelectorAll('.gdim').forEach(node=>node.classList.remove('gdim'))});
}

actions.inspectEntityNode=async id=>{
  try{
    const data=await api(`/api/admin/graph/${q(id)}?user_id=${q(selectedUser())}&depth=1`);
    const node=(data.nodes||[]).find(n=>n.id===id)||(state.graphNodes||[]).find(n=>n.id===id)||{};
    const near=(data.nodes||[]).filter(n=>n.id!==id);
    openModal(`Сущность: ${node.name||id}`,
      `<div class="kv"><div>Тип</div><div>${esc(node.entity_type||'—')}</div><div>Документов</div><div>${esc(String(node.knowledge_count??'—'))}</div><div>Идентификатор</div><div class="mono">${esc(id)}</div></div>
       <h3>Соседи по подтверждённым связям (${near.length})</h3>
       ${near.length?table(['Имя','Тип'],near.map(n=>`<tr><td>${esc(n.name)}</td><td>${esc(n.entity_type)}</td></tr>`)):empty('Подтверждённых связей сущность-сущность нет — они появляются после вашего подтверждения')}`);
  }catch(e){toast(e.message,true)}
};

renderers.graph=async gen=>{
  const uid=selectedUser();
  const [entities,resolutions,suggestionQueue,relations,conflicts,containers,overview]=await Promise.all([
    api(`/api/admin/entities?user_id=${q(uid)}&limit=${PAGE}&offset=${state.entitiesOffset}`),
    api(`/api/admin/resolutions?user_id=${q(uid)}&status=suggested&limit=${PAGE}&offset=${state.resolutionsOffset}`),
    api(`/api/admin/entity-suggestions/queue?user_id=${q(uid)}&limit=15`),
    api(`/api/admin/relation-candidates?user_id=${q(uid)}&status=${q(state.relationStatus||'suggested')}&limit=${PAGE}&offset=${state.relationsOffset}`),
    // Статус выбирается, а не зашит. С зашитым `suggested` нажатие «Подтвердить»
    // убирало дубликат из админки НАВСЕГДА: подтверждённые нигде не показывались, а
    // узнать их идентификатор было неоткуда — при том что разрешить такой конфликт
    // технически по-прежнему можно.
    api(`/api/admin/conflicts?user_id=${q(uid)}&status=${q(state.conflictStatus||'suggested')}&limit=${PAGE}&offset=${state.conflictsOffset}`),
    api(`/api/admin/containers?user_id=${q(uid)}`),
    api(`/api/admin/graph?user_id=${q(uid)}&limit=150`).catch(()=>({nodes:[],edges:[],shown:0,total:0}))
  ]);
  if(gen!==renderGen)return;
  state.entities=entities.items||[];state.resolutions=resolutions.items||[];state.relationCandidates=relations.items||[];state.conflicts=conflicts.items||[];state.containers=containers.items||[];
  const erows=state.entities.map(e=>`<tr><td><b>${esc(e.name)}</b><div class="mono">${esc(e.id)}</div></td><td><span class="badge">${esc(e.entity_type)}</span></td><td>${(parse(e.aliases_json,[])||[]).map(a=>`<span class="badge">${esc(a)}</span>`).join(' ')||'—'}</td><td>${esc(short(e.description,120))}</td><td><button class="btn small" ${call('showGraph',e.id)}>Связи</button> <button class="btn small" ${call('editEntityDialog',e.id)}>Исправить</button> <button class="btn small danger" ${call('deleteEntity',e.id)}>Убрать из списка</button></td></tr>`);
  const rrows=state.resolutions.map(r=>{const a=r.entity_a||{};const b=r.entity_b||{};return `<tr><td><b>${esc(a.name||r.entity_a_id)}</b><div class="muted">${esc(a.entity_type||'')} · знаний ${Number(a.knowledge_count||0)} · связей ${Number(a.relation_count||0)}</div></td><td><b>${esc(b.name||r.entity_b_id)}</b><div class="muted">${esc(b.entity_type||'')} · знаний ${Number(b.knowledge_count||0)} · связей ${Number(b.relation_count||0)}</div></td><td><b>${Number(r.confidence||0).toFixed(3)}</b><div class="muted">${esc(r.resolution_method)}</div><button class="btn small" ${call('showResolutionEvidence',r.id)}>Evidence</button></td><td><button class="btn small good" ${call('resolveEntity',r.id,'accept',a.id||r.entity_a_id)}>Оставить A</button> <button class="btn small good" ${call('resolveEntity',r.id,'accept',b.id||r.entity_b_id)}>Оставить B</button> <button class="btn small danger" ${call('resolveEntity',r.id,'reject','')}>Не дубликаты</button></td></tr>`});
  const lrows=state.relationCandidates.map(r=>`<tr><td><input class="relation-check" type="checkbox" value="${esc(r.id)}"></td><td><b>${esc(r.source_name)}</b><div class="muted">${esc(r.source_type)}</div></td><td><span class="badge">${esc(r.relation_type)}</span><div class="muted">уверенность ${Number(r.confidence||0).toFixed(2)}</div></td><td><b>${esc(r.target_name)}</b><div class="muted">${esc(r.target_type)}</div></td><td><button class="btn small" ${call('showCandidateEvidence','relation',r.id)}>Evidence</button> <button class="btn small good" ${call('reviewRelation',r.id,'accepted')}>Принять</button> <button class="btn small danger" ${call('reviewRelation',r.id,'rejected')}>Отклонить</button></td></tr>`);
  const crows=state.conflicts.map(c=>`<tr><td><input class="conflict-check" type="checkbox" value="${esc(c.id)}"></td><td><b>${esc(c.knowledge_a_title||c.knowledge_a_id)}</b>${sideNote(c.knowledge_a_stage,c.knowledge_a_superseded_by)}<div class="mono">${esc(c.knowledge_a_id)}</div></td><td><span class="badge warn">${esc(c.conflict_type==='near_duplicate'?'почти дубликат':c.conflict_type)}</span><div class="muted">${c.conflict_type==='near_duplicate'?'сходство':'уверенность'} ${Number(c.confidence||0).toFixed(2)}</div></td><td><b>${esc(c.knowledge_b_title||c.knowledge_b_id)}</b>${sideNote(c.knowledge_b_stage,c.knowledge_b_superseded_by)}<div class="mono">${esc(c.knowledge_b_id)}</div></td><td><button class="btn small" ${call('showCandidateEvidence','conflict',c.id)}>Evidence</button> <button class="btn small good" ${call('resolveConflict',c.id,c.knowledge_a_id,c.knowledge_a_title||c.knowledge_a_id)}>Оставить A</button> <button class="btn small good" ${call('resolveConflict',c.id,c.knowledge_b_id,c.knowledge_b_title||c.knowledge_b_id)}>Оставить B</button> <button class="btn small danger" ${call('reviewConflict',c.id,'dismissed')}>Не конфликт</button></td></tr>`);
  const kindLabel={project:'проект',collection:'коллекция'};
  const byParent={};
  state.containers.forEach(c=>{(byParent[c.parent_id||'']=byParent[c.parent_id||'']||[]).push(c)});
  const containerRows=[];
  const addLevel=(parentKey,depth)=>{(byParent[parentKey]||[]).forEach(c=>{containerRows.push(`<div class="toolbar">${'&nbsp;&nbsp;&nbsp;'.repeat(depth)}<span class="badge">${esc(kindLabel[c.entity_type]||c.entity_type)}</span><b>${esc(c.name)}</b><span class="muted">знаний: ${Number(c.knowledge_count||0)}</span><span class="grow"></span><button class="btn small" ${call('showContainerKnowledge',c.id,c.name)}>Знания</button> <button class="btn small" ${call('showGraph',c.id)}>Связи</button></div>`);addLevel(c.id,depth+1)})};
  addLevel('',0);
  setApp(gen,`<section class="card"><div class="toolbar"><h2 class="grow">Картина графа</h2></div>${graphMarkup(overview)}</section><div class="notice">Граф развивается через предложения: Friday не объединяет сущности, не добавляет спорные связи и не объявляет факты устаревшими без явного решения. Массовые действия возвращают число применённых и пропущенных элементов, поэтому частичный сбой не теряется.</div><section class="card"><div class="toolbar"><h2 class="grow">Проекты и коллекции</h2><button class="btn primary" ${call('createContainerDialog')}>Новый контейнер</button><span class="badge">${containerRows.length}</span></div>${containerRows.join('')||empty('Контейнеров пока нет: создайте проект или коллекцию и привязывайте к ним знания через «Добавить связь».')}</section><section class="card"><div class="toolbar"><h2 class="grow">Документы с неразобранными сущностями</h2><button class="btn" ${call('loadSuggestionGroups')}>Группы</button><span class="badge">${Number(suggestionQueue.total||0)}</span></div>${(suggestionQueue.items||[]).length?`<div class="notice">Оценка сверху: столько предложений извлекатель нашёл при приёме, за вычетом уже решённых связей. Откройте документ и подтвердите то, что действительно является сущностью — граф растёт только так.</div>`+table(['Документ','Осталось',''],(suggestionQueue.items||[]).map(it=>`<tr><td><b>${esc(it.title)}</b><div class="mono">${esc(it.id)}</div></td><td><b>${Number(it.pending||0)}</b></td><td><button class="btn small primary" ${call('inspectKnowledge',it.id)}>Разобрать</button></td></tr>`)):empty('Неразобранных предложений нет')}</section><section class="card"><div class="toolbar"><h2 class="grow">Предлагаемые связи</h2>${['suggested','accepted','rejected'].map(st=>`<button class="btn small${(state.relationStatus||'suggested')===st?' primary':''}" ${call('filterRelationStatus',st)}>${({suggested:'предложены',accepted:'приняты',rejected:'отклонены'})[st]}</button>`).join(' ')}<button class="btn" ${call('selectAllGraph','relation',true)}>Выбрать все</button><button class="btn" ${call('selectAllGraph','relation',false)}>Снять</button><button class="btn good" ${call('bulkReviewRelations','accepted')}>Принять выбранные</button><button class="btn danger" ${call('bulkReviewRelations','rejected')}>Отклонить выбранные</button><span class="badge">${lrows.length}</span></div>${lrows.length?table(['','Источник','Связь','Цель','Решение'],lrows):empty('Предложений связей нет')}${pager('relationsPage',state.relationsOffset,state.relationCandidates.length,relations.total)}</section><section class="card"><div class="toolbar"><h2 class="grow">Противоречия и дубликаты</h2>${['suggested','confirmed','dismissed','resolved'].map(st=>`<button class="btn small${(state.conflictStatus||'suggested')===st?' primary':''}" ${call('filterConflictStatus',st)}>${({suggested:'предложены',confirmed:'подтверждены',dismissed:'отклонены',resolved:'разрешены'})[st]}</button>`).join(' ')}<button class="btn" ${call('detectDuplicates2')}>Найти дубли</button><button class="btn" ${call('selectAllGraph','conflict',true)}>Выбрать все</button><button class="btn" ${call('selectAllGraph','conflict',false)}>Снять</button><button class="btn good" ${call('bulkReviewConflicts','confirmed')}>Подтвердить выбранные</button><button class="btn danger" ${call('bulkReviewConflicts','dismissed')}>Отклонить выбранные</button><span class="badge">${crows.length}</span></div>${crows.length?table(['','Утверждение A','Тип','Утверждение B','Решение'],crows):empty('Потенциальных противоречий нет')}${pager('conflictsPage',state.conflictsOffset,state.conflicts.length,conflicts.total)}</section><section class="card"><div class="toolbar"><h2 class="grow">Entity Resolution</h2><button class="btn primary" ${call('detectDuplicates')}>Пересчитать кандидатов</button><span class="badge">${Number(resolutions.total||0).toLocaleString('ru')}</span></div>${rrows.length?table(['Сущность A','Сущность B','Сигналы','Решение'],rrows):empty('Нерешённых предложений нет')}${pager('resolutionsPage',state.resolutionsOffset,state.resolutions.length,resolutions.total)}</section><section class="card"><div class="toolbar"><h2 class="grow">Сущности</h2><button class="btn primary" ${call('createEntityDialog')}>Новая сущность</button><span class="badge">${erows.length}</span></div>${erows.length?table(['Имя','Тип','Псевдонимы','Описание',''],erows):empty('Сущностей пока нет')}${pager('entitiesPage',state.entitiesOffset,state.entities.length,entities.total)}</section>`);
  bindGraph();
};
// Пакетное подтверждение: 42 кандидата на документ делают поштучный разбор
// нечитаемым из-за объёма, и граф фактически не растёт. Группы считаются ПО
// НАЖАТИЮ (скан окна очереди стоит секунд, а не миллисекунд) — не при каждой
// отрисовке экрана.
actions.loadSuggestionGroups=async()=>{try{toast('Считаю группы по окну очереди…');const d=await api(`/api/admin/entity-suggestions/groups?user_id=${q(selectedUser())}&scan=60`);state.suggestionGroups=d.groups||[];const rows=state.suggestionGroups.map((g,i)=>`<tr><td><b>${esc(g.name)}</b><div class="muted">${esc(g.entity_type)} · ${esc(g.method)} · увер. ${Number(g.confidence_max||0).toFixed(2)}</div></td><td><b>${Number(g.document_count||0)}</b><div class="muted">${esc((g.documents||[]).slice(0,3).map(dd=>dd.title).join('; '))}${(g.documents||[]).length>3?'…':''}</div></td><td><button class="btn small good" ${call('decideSuggestionGroup',i,'accept')}>Принять все</button> <button class="btn small danger" ${call('decideSuggestionGroup',i,'reject')}>Отклонить все</button></td></tr>`);openModal(`Группы кандидатов (окно: ${Number(d.scanned_documents||0)} документов)`,rows.length?`<div class="notice">Одно решение вместо N: принятие ставит подтверждённую связь в каждый документ группы, отклонение записывает отказ — и группа больше не предлагается. Считается по окну очереди, не по всему архиву.</div>`+table(['Сущность','Документов','Решение'],rows):empty('В окне очереди нет групп из двух и более документов — разбирайте поштучно через «Разобрать»'),`<button class="btn" ${call('closeModal')}>Закрыть</button>`)}catch(e){toast(e.message,true)}};
actions.decideSuggestionGroup=async(index,decision)=>{const g=(state.suggestionGroups||[])[index];if(!g)return;if(!confirm(decision==='accept'?`Подтвердить «${g.name}» в ${g.document_count} документах?`:`Отклонить «${g.name}» во всех ${g.document_count} документах?`))return;try{const d=await api('/api/admin/entity-suggestions/groups/decide',{method:'POST',body:JSON.stringify({user_id:selectedUser(),name:g.name,entity_type:g.entity_type,decision,knowledge_object_ids:(g.documents||[]).map(dd=>dd.id)})});toast(`Решено: ${d.decided}, пропущено (уже решённые): ${d.skipped_existing}`);closeModal();refresh()}catch(e){toast(e.message,true)}};
actions.detectDuplicates=async()=>{try{const d=await api('/api/admin/resolutions/detect',{method:'POST',body:JSON.stringify({user_id:selectedUser()})});toast(`Предложений найдено/обновлено: ${(d.suggested||[]).length}`);refresh()}catch(e){toast(e.message,true)}};
actions.detectDuplicates2=async()=>{try{const d=await api('/api/admin/knowledge/detect-duplicates',{method:'POST',body:JSON.stringify({user_id:selectedUser()})});toast(d.reason?d.reason:`Дубликатов предложено: ${d.detected} (сравнено ${d.objects_compared} из ${d.objects_scanned}${d.pending?`, осталось ${d.pending}`:''})`);refresh()}catch(e){toast(e.message,true)}};
actions.resolveEntity=async(id,action,target)=>{if(action==='accept'&&!confirm('Объединить сущности с сохранением истории, aliases, отношений и knowledge links?'))return;try{const body={user_id:selectedUser()};if(target)body.target_entity_id=target;await api(`/api/admin/resolutions/${q(id)}/${action}`,{method:'POST',body:JSON.stringify(body)});toast(action==='accept'?'Сущности объединены':'Кандидат отклонён');refresh()}catch(e){toast(e.message,true)}};
actions.showResolutionEvidence=id=>{const r=state.resolutions.find(item=>item.id===id);if(!r)return;openModal('Evidence кандидата',`<div class="pre">${esc(JSON.stringify(r.evidence||parse(r.evidence_json,{}),null,2))}</div>`,`<button class="btn" ${call('closeModal')}>Закрыть</button>`)};
actions.showCandidateEvidence=(kind,id)=>{const source=kind==='relation'?state.relationCandidates:state.conflicts;const item=source.find(v=>v.id===id);if(!item)return;openModal('Evidence',`<div class="pre">${esc(JSON.stringify(item.evidence||parse(item.evidence_json,{}),null,2))}</div>`,`<button class="btn" ${call('closeModal')}>Закрыть</button>`)};
actions.reviewRelation=async(id,status)=>{if(status==='accepted'&&!confirm('Создать подтверждённую связь в Knowledge Graph?'))return;try{await api(`/api/admin/relation-candidates/${q(id)}/review`,{method:'POST',body:JSON.stringify({user_id:selectedUser(),status})});toast(status==='accepted'?'Связь добавлена':'Предложение отклонено');refresh()}catch(e){toast(e.message,true)}};
actions.reviewConflict=async(id,status)=>{try{await api(`/api/admin/conflicts/${q(id)}/review`,{method:'POST',body:JSON.stringify({user_id:selectedUser(),status})});toast(status==='confirmed'?'Конфликт подтверждён для ручной сверки':'Предложение отклонено');refresh()}catch(e){toast(e.message,true)}};
actions.resolveConflict=async(id,winnerId,winnerTitle)=>{if(!confirm(`Оставить «${winnerTitle}» актуальной? Вторая запись станет deprecated со ссылкой на неё (обратимо правкой).`))return;try{await api(`/api/admin/conflicts/${q(id)}/resolve`,{method:'POST',body:JSON.stringify({user_id:selectedUser(),winner_id:winnerId})});toast('Конфликт разрешён: проигравшая запись помечена устаревшей');refresh()}catch(e){toast(e.message,true)}};
actions.selectAllGraph=(kind,value)=>document.querySelectorAll(`.${kind}-check`).forEach(el=>{el.checked=value});
actions.bulkReviewRelations=async status=>{const ids=[...document.querySelectorAll('.relation-check:checked')].map(el=>el.value);if(!ids.length){toast('Сначала выберите связи',true);return}const question=status==='accepted'?`Создать ${ids.length} подтверждённых связей в Knowledge Graph?`:`Отклонить ${ids.length} предложенных связей? Они останутся в базе со статусом «отклонены» — их видно по фильтру статуса, — но заново принять их будет нельзя.`;if(!confirm(question))return;await bulkApply(ids,batch=>api('/api/admin/relation-candidates/bulk-review',{method:'POST',body:JSON.stringify({user_id:selectedUser(),candidate_ids:batch,status})}))};
actions.bulkReviewConflicts=async status=>{const ids=[...document.querySelectorAll('.conflict-check:checked')].map(el=>el.value);if(!ids.length){toast('Сначала выберите противоречия',true);return}if(!confirm(status==='confirmed'?`Подтвердить ${ids.length} противоречий?`:`Отклонить ${ids.length} противоречий? Они останутся в базе со статусом «отклонены» и видны по фильтру статуса.`))return;await bulkApply(ids,batch=>api('/api/admin/conflicts/bulk-review',{method:'POST',body:JSON.stringify({user_id:selectedUser(),conflict_ids:batch,status,resolution_note:'admin UI bulk review'})}))};
const ENTITY_TYPES=['person','project','concept','event','organization','location','document','collection','other'];
function entityTypeOptions(selected){return ENTITY_TYPES.map(t=>`<option value="${t}" ${t===selected?'selected':''}>${t}</option>`).join('')}
actions.createEntityDialog=()=>openModal('Новая сущность',`<div class="form"><label>Имя<input id="entityName"></label><label>Тип<select id="entityType" class="field">${entityTypeOptions('concept')}</select></label><label>Псевдонимы через запятую<input id="entityAliases"></label><label>Описание<textarea id="entityDescription"></textarea></label></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn primary" ${call('createEntity')}>Создать</button>`);
actions.createEntity=async()=>{try{await api('/api/admin/entities',{method:'POST',body:JSON.stringify({user_id:selectedUser(),name:document.getElementById('entityName').value,entity_type:document.getElementById('entityType').value,aliases:document.getElementById('entityAliases').value.split(',').map(v=>v.trim()).filter(Boolean),description:document.getElementById('entityDescription').value})});closeModal();toast('Сущность создана');refresh()}catch(e){toast(e.message,true)}};
actions.editEntityDialog=id=>{const e=state.entities.find(item=>item.id===id);if(!e)return;const aliases=parse(e.aliases_json,[]);openModal('Исправление сущности',`<div class="form"><label>Имя<input id="entityName" value="${esc(e.name)}"></label><label>Тип<select id="entityType" class="field">${entityTypeOptions(e.entity_type)}</select></label><label>Псевдонимы через запятую<input id="entityAliases" value="${esc(aliases.join(', '))}"></label><label>Описание<textarea id="entityDescription">${esc(e.description||'')}</textarea></label></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn primary" ${call('updateEntity',id)}>Сохранить</button>`)};
actions.updateEntity=async id=>{try{await api(`/api/admin/entities/${q(id)}`,{method:'PATCH',body:JSON.stringify({user_id:selectedUser(),name:document.getElementById('entityName').value,entity_type:document.getElementById('entityType').value,aliases:document.getElementById('entityAliases').value.split(',').map(v=>v.trim()).filter(Boolean),description:document.getElementById('entityDescription').value})});closeModal();toast('Сущность обновлена');refresh()}catch(e){toast(e.message,true)}};
actions.deleteEntity=async id=>{if(!confirm('Мягко удалить сущность? Её связи станут недоступны в графе.'))return;try{await api(`/api/admin/entities/${q(id)}?user_id=${q(selectedUser())}`,{method:'DELETE'});toast('Сущность удалена');refresh()}catch(e){toast(e.message,true)}};
actions.showContainerKnowledge=async(id,name)=>{try{const d=await api(`/api/admin/knowledge?user_id=${q(selectedUser())}&entity_id=${q(id)}&limit=500`);const items=d.items||[];openModal(`Знания: ${name}`,items.length?`<div class="grid">${items.map(k=>`<div class="card"><b>${esc(k.title||'Без названия')}</b> <span class="badge">${esc(k.knowledge_kind||'note')}</span><div class="muted">${esc(short(k.summary||k.content,200))}</div><div class="toolbar mt8"><button class="btn small" ${call('inspectKnowledge',k.id)}>Инспекция</button></div></div>`).join('')}</div>`:empty('Подтверждённых знаний в контейнере нет. Привяжите их через «Инспекция → Добавить связь».'),`<button class="btn" ${call('closeModal')}>Закрыть</button>`)}catch(e){toast(e.message,true)}};
actions.createContainerDialog=()=>{const options=state.containers.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');openModal('Новый контейнер',`<div class="form"><label>Название<input id="containerName" placeholder="Например: Ремонт квартиры"></label><label>Тип<select id="containerKind" class="field"><option value="collection">Коллекция</option><option value="project">Проект</option></select></label><label>Родительский контейнер (необязательно)<select id="containerParent" class="field"><option value="">— нет —</option>${options}</select></label><label>Описание<textarea id="containerDescription"></textarea></label></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn primary" ${call('createContainer')}>Создать</button>`)};
actions.createContainer=async()=>{try{const payload={user_id:selectedUser(),name:document.getElementById('containerName').value,kind:document.getElementById('containerKind').value,description:document.getElementById('containerDescription').value};const parent=document.getElementById('containerParent').value;if(parent)payload.parent_id=parent;await api('/api/admin/containers',{method:'POST',body:JSON.stringify(payload)});closeModal();toast('Контейнер создан');refresh()}catch(e){toast(e.message,true)}};
actions.showGraph=async id=>{try{const g=await api(`/api/admin/graph/${q(id)}?user_id=${q(selectedUser())}&depth=2`);const edges=g.edges||g.relations||[];openModal('Фрагмент графа',`<div class="grid two"><section class="card"><h3>Узлы</h3>${(g.nodes||[]).map(n=>`<div class="toolbar"><span class="badge">${esc(n.entity_type)}</span><b>${esc(n.name)}</b></div>`).join('')||empty('Нет узлов')}</section><section class="card"><h3>Отношения</h3>${edges.map(r=>`<div class="toolbar"><span class="badge">${esc(r.relation_type)}</span><span class="mono">${esc(r.source_entity_id)} → ${esc(r.target_entity_id)}</span></div>`).join('')||empty('Нет отношений')}</section></div><details class="mt14"><summary>JSON</summary><div class="pre">${esc(JSON.stringify(g,null,2))}</div></details>`,`<button class="btn" ${call('closeModal')}>Закрыть</button>`)}catch(e){toast(e.message,true)}};
renderers.quality=async gen=>{
  const uid=selectedUser();const [d,evalCases]=await Promise.all([api(`/api/admin/quality?user_id=${q(uid)}&lifecycle_limit=${PAGE}&lifecycle_offset=${state.lifecycleOffset}`),api(`/api/admin/eval/cases?user_id=${q(uid)}`)]);if(gen!==renderGen)return;state.lifecycle=d.lifecycle_candidates||[];const g=d.graph||{},u=d.usage||{},f=d.feedback||{},r=d.review_pressure||{};
  state.evalCases=evalCases.items||[];
  const evalRows=state.evalCases.map(c=>`<tr><td><b>${esc(c.query)}</b>${c.source&&c.source!=='manual'?` <span class="badge">${esc(c.source)}</span>`:''}</td><td>${(c.expected_ids||[]).length}</td><td>${esc(c.note||'')}</td><td><button class="btn small danger" ${call('deleteEvalCase',c.id)}>Убрать из списка</button></td></tr>`);
  const lastReport=state.evalReport?`<div class="notice">recall@${state.evalReport.k||10}: <b>${state.evalReport.recall_at_k}</b> · MRR: <b>${state.evalReport.mrr}</b> · кейсов: ${state.evalReport.cases}${state.evalReport.regression&&state.evalReport.regression.regressed?` · <span class="badge bad">регрессия ${state.evalReport.regression.delta}</span>`:''}</div>`:'';
  const evalCard=`<section class="card"><div class="toolbar"><h2 class="grow">Оценка качества поиска</h2><button class="btn" ${call('addEvalCaseDialog')}>Добавить эталон</button><button class="btn primary" ${call('runEval')}>Прогнать</button><span class="badge">${evalRows.length}</span></div><div class="notice">Золотой набор «запрос → ожидаемые записи». Прогон измеряет recall@k и MRR по реальному поиску и предупреждает о регрессии относительно прошлого прогона.</div>${lastReport}${evalRows.length?table(['Запрос','Ожидается','Заметка',''],evalRows):empty('Эталонов пока нет — добавьте запрос и отметьте, какие записи он должен находить.')}</section>`;
  const explainCard=`<section class="card"><h2>Трейс ретривера</h2><div class="notice">Детерминированный разбор поиска (без модели): что ретривер нашёл, что отбросил и почему, и вклад каждого сигнала в итоговый скор.</div><div class="toolbar"><input id="explainQuery" class="field grow" placeholder="например: IP сервера Atlas"><button class="btn primary" ${call('explainSearch')}>Объяснить</button></div><div id="explainResults" class="muted">Введите запрос и нажмите «Объяснить».</div></section>`;
  const rows=state.lifecycle.map(c=>{const k=c.knowledge_object||{};return `<tr><td><input class="lifecycle-check" type="checkbox" value="${esc(k.id)}"></td><td><b>${esc(k.title||'Без названия')}</b><div class="muted">${esc(short(k.summary||k.content,160))}</div><div class="mono">${esc(k.id)}</div></td><td><span class="badge ${Number(c.risk_score||0)>=.68?'bad':'warn'}">${Number(c.risk_score||0).toFixed(2)}</span><div>${(c.reasons||[]).map(v=>`<span class="badge">${esc(v)}</span>`).join(' ')}</div></td><td>${Number(k.importance||0).toFixed(2)} → ${Number(c.suggested_importance||0).toFixed(2)}</td></tr>`});
  setApp(gen,`<div class="grid stats">${[['Использований в поиске',u.retrievals],['Использований в ответах',u.answers],['Положительных сигналов',u.positive],['Отрицательных сигналов',u.negative],['Inbox',r.pending_inbox],['Связей на review',r.relation_candidates],['Конфликтов на review',r.conflicts],['Lifecycle кандидатов',r.lifecycle_candidates]].map(([l,v])=>`<div class="card stat"><div class="value">${Number(v||0).toLocaleString('ru')}</div><div class="label">${l}</div></div>`).join('')}</div><div class="grid two"><section class="card"><h2>Здоровье графа</h2><div class="pre">${esc(JSON.stringify(g,null,2))}</div></section><section class="card"><h2>Feedback loop</h2><div class="kv"><div>Текущих оценок</div><div>${Number(f.current_count||0)}</div><div>Оценок классификации</div><div>${Number(f.classification_current||0)}</div><div>Отрицательных классификаций</div><div>${Number(f.classification_negative||0)}</div></div><details><summary>История</summary><div class="pre">${esc(JSON.stringify(f.history||{},null,2))}</div></details></section></div><section class="card"><div class="toolbar"><h2 class="grow">Review-only lifecycle</h2><button class="btn" ${call('selectAllLifecycle',true)}>Выбрать все</button><button class="btn" ${call('selectAllLifecycle',false)}>Снять</button><button class="btn" ${call('applyLifecycle','lower_importance')}>Снизить важность</button><button class="btn good" ${call('applyLifecycle','keep')}>Оставить</button><button class="btn danger" ${call('applyLifecycle','archive')}>Архивировать</button></div><div class="notice">Friday только предлагает кандидатов. Недавно использованные, вручную подтверждённые, полученные из файлов и положительно оценённые знания защищены.</div>${rows.length?table(['','Знание','Риск и причины','Важность'],rows):empty('Кандидатов на пересмотр нет')}${pager('lifecyclePage',state.lifecycleOffset,state.lifecycle.length,d.lifecycle_total)}</section>${evalCard}${explainCard}`);
};
actions.runEval=async()=>{try{const d=await api('/api/admin/eval/run',{method:'POST',body:JSON.stringify({user_id:selectedUser()})});state.evalReport=d.report;if(d.report.reason){toast(d.report.reason)}else{toast(`recall@${d.report.k}: ${d.report.recall_at_k} · MRR: ${d.report.mrr}`)}refresh()}catch(e){toast(e.message,true)}};
actions.deleteEvalCase=async id=>{if(!confirm('Удалить эталонный запрос?'))return;try{await api(`/api/admin/eval/cases/${q(id)}?user_id=${q(selectedUser())}`,{method:'DELETE'});toast('Эталон удалён');refresh()}catch(e){toast(e.message,true)}};
actions.addEvalCaseDialog=()=>{openModal('Новый эталон поиска',`<div class="form"><div class="notice">Введите запрос и найдите текущие результаты, затем отметьте записи, которые он ДОЛЖЕН находить.</div><label>Запрос<input id="evalQuery" placeholder="например: IP сервера Atlas"></label><div class="toolbar"><button class="btn" ${call('evalSearch')}>Найти</button></div><div id="evalResults" class="muted">Результаты поиска появятся здесь.</div><label>Заметка (необязательно)<input id="evalNote"></label></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn primary" ${call('saveEvalCase')}>Сохранить эталон</button>`)};
actions.evalSearch=async()=>{const query=document.getElementById('evalQuery').value.trim();if(!query){toast('Введите запрос',true);return}try{const d=await api(`/api/admin/eval/search?user_id=${q(selectedUser())}&q=${q(query)}`);const items=d.items||[];document.getElementById('evalResults').innerHTML=items.length?items.map(it=>`<label class="pick"><input class="eval-pick" type="checkbox" value="${esc(it.id)}"><span><b>${esc(it.title)}</b> <span class="badge">${esc(it.knowledge_kind||'note')}</span> <span class="mono">${esc(it.id)}</span></span></label>`).join(''):'<div class="muted">Ничего не найдено.</div>'}catch(e){toast(e.message,true)}};
actions.saveEvalCase=async()=>{const query=document.getElementById('evalQuery').value.trim();const ids=[...document.querySelectorAll('.eval-pick:checked')].map(el=>el.value);if(!query){toast('Введите запрос',true);return}if(!ids.length){toast('Отметьте хотя бы одну ожидаемую запись',true);return}try{await api('/api/admin/eval/cases',{method:'POST',body:JSON.stringify({user_id:selectedUser(),query,expected_ids:ids,note:document.getElementById('evalNote').value})});closeModal();toast('Эталон сохранён');refresh()}catch(e){toast(e.message,true)}};
const TRACE_REASONS={identifier_mismatch:'несовпадение идентификатора',insufficient_evidence:'мало доказательств',deprecated_weak:'устаревший, слабое совпадение',deleted:'удалён'};
const TRACE_SIGNALS=[['rrf','RRF'],['lexical','лексика'],['field','поля'],['embedding','вектор'],['embedding_chunks','фрагментов'],['graph','граф'],['feedback','фидбэк'],['kind_alignment','тип'],['usage','исп.'],['noise_penalty','шум−']];
function traceBadge(row){if(row.status==='returned')return `<span class="badge ok">выдан #${(row.rank||0)+1}</span>`;if(row.status==='below_limit')return '<span class="badge warn">ниже отсечки</span>';return `<span class="badge bad">отброшен: ${esc(TRACE_REASONS[row.reason]||row.reason||'—')}</span>`}
function traceSignals(c){const chips=TRACE_SIGNALS.filter(([k])=>Math.abs(Number(c[k]||0))>=0.0005).map(([k,l])=>`<span class="badge">${l}: ${Number(c[k]||0).toFixed(3)}</span>`).join(' ');return chips||'<span class="muted">нет заметных сигналов</span>'}
function renderTrace(d){const rows=(d.trace||[]).map(row=>{const c=row.components||{};const life=row.lifecycle_stage||'active';return `<div class="trace-row"><div>${traceBadge(row)} <b>${esc(row.title)}</b> <span class="badge">${esc(row.knowledge_kind||'note')}</span> <span class="badge ${life==='active'?'ok':'warn'}">${esc(life)}</span> <span class="mono">скор ${Number(row.score||0).toFixed(4)}</span></div><div class="mt6">${traceSignals(c)}</div><details class="mt6"><summary>Все сигналы</summary><div class="pre">${esc(JSON.stringify(c,null,2))}</div></details><div class="mono muted mt6">${esc(row.id)}</div></div>`}).join('');return `<div class="notice">Кандидатов: ${d.candidates} · выдано: ${d.returned} · отброшено: ${d.discarded}</div>${rows}`}
actions.explainSearch=async()=>{const query=document.getElementById('explainQuery').value.trim();if(!query){toast('Введите запрос',true);return}try{const d=await api(`/api/admin/retrieval/explain?user_id=${q(selectedUser())}&q=${q(query)}`);const box=document.getElementById('explainResults');if(!box)return;box.innerHTML=(d.trace||[]).length?renderTrace(d):'<div class="muted">Кандидатов не найдено.</div>'}catch(e){toast(e.message,true)}};
actions.selectAllLifecycle=value=>document.querySelectorAll('.lifecycle-check').forEach(el=>{el.checked=value});
actions.applyLifecycle=async action=>{state.lifecycleOffset=0;const ids=[...document.querySelectorAll('.lifecycle-check:checked')].map(el=>el.value);if(!ids.length){toast('Сначала выберите кандидатов',true);return}if(action==='archive'&&!confirm(`Архивировать ${ids.length} выбранных знаний?`))return;await bulkApply(ids,batch=>api('/api/admin/lifecycle/apply',{method:'POST',body:JSON.stringify({user_id:selectedUser(),knowledge_ids:batch,action,require_candidate:true,days_threshold:90})}))};
renderers.cleanup=async gen=>{
  const uid=selectedUser();
  const data=await api(`/api/admin/cleanup/legacy?user_id=${q(uid)}&limit=${PAGE}&offset=${state.cleanupOffset}`);
  if(gen!==renderGen)return;
  state.cleanup=data.items||[];
  const rows=state.cleanup.map(c=>{const k=c.knowledge_object||{};return `<tr><td><input class="cleanup-check" type="checkbox" value="${esc(k.id)}"></td><td><b>${esc(k.title||'Без названия')}</b><div class="muted">${esc(short(k.content||k.summary,180))}</div><div class="mono">${esc(k.id)}</div></td><td><span class="badge ${Number(c.risk_score||0)>=.8?'bad':'warn'}">risk ${Number(c.risk_score||0).toFixed(2)}</span><div>${(c.reasons||[]).map(v=>`<span class="badge">${esc(v)}</span>`).join(' ')}</div></td><td><span class="badge">${esc(c.assessment?.action||'')}</span><div class="muted">${esc(c.assessment?.reason||'')}</div></td><td>${esc(c.recommended_action||'review')}</td><td><button class="btn small" ${call('inspectKnowledge',k.id)}>Инспекция</button></td></tr>`});
  setApp(gen,`<div class="notice"><b>Безопасная ревизия:</b> сканирование ничего не удаляет и не меняет. Файлы, вручную подтверждённые и уже проверенные объекты защищены. «Вернуть в Inbox» мягко удаляет Knowledge Object из retrieval, сохраняя Raw Object, provenance и версии.</div><section class="card"><div class="toolbar"><h2 class="grow">Кандидаты на очистку legacy-данных</h2><button class="btn" ${call('selectAllCleanup',true)}>Выбрать все</button><button class="btn" ${call('selectAllCleanup',false)}>Снять</button><button class="btn good" ${call('applyCleanup','return_to_inbox')}>Вернуть в Inbox</button><button class="btn" ${call('applyCleanup','reclassify')}>Переобогатить</button><button class="btn" ${call('applyCleanup','keep')}>Оставить подтверждённым</button><button class="btn" ${call('applyCleanup','archive')}>Архивировать</button><button class="btn danger" ${call('applyCleanup','soft_delete')}>Мягко удалить</button><span class="badge">${countBadge(rows.length,data.total)}</span></div>${rows.length?table(['','Объект','Почему отмечен','Свежая классификация','Рекомендация',''],rows):empty('Подозрительных объектов не найдено')}${pager('cleanupPage',state.cleanupOffset,state.cleanup.length,data.total)}</section>`);
};
actions.selectAllCleanup=value=>document.querySelectorAll('.cleanup-check').forEach(el=>{el.checked=value});
actions.applyCleanup=async action=>{state.cleanupOffset=0;const ids=[...document.querySelectorAll('.cleanup-check:checked')].map(el=>el.value);if(!ids.length){toast('Сначала выберите объекты',true);return}if(action==='soft_delete'&&!confirm(`Мягко удалить ${ids.length} объектов? История и Raw Objects сохранятся.`))return;await bulkApply(ids,batch=>api('/api/admin/cleanup/legacy/apply',{method:'POST',body:JSON.stringify({user_id:selectedUser(),action,knowledge_ids:batch,require_suspect:true,reason:'admin UI legacy quality review'})}))};
// Хроника корпуса. Столбики строятся по СОБСТВЕННОЙ дате документа, а не по дате
// загрузки: на корпусе владельца различных дней в `updated_at` три на 1537 объектов,
// то есть лента по загрузке показала бы три деления и ничего не сказала бы о времени.
// Своя дата известна у 88%; сколько объектов в ленту не попадает, экран называет сам.
const monthName=v=>['январь','февраль','март','апрель','май','июнь','июль','август','сентябрь','октябрь','ноябрь','декабрь'][Number(v)-1]||v;
// Подпись столбика зависит от крупности: «2024», «март 2024», «17.03».
const bucketLabel=(key,gran)=>gran==='year'?key:gran==='month'?`${monthName(key.slice(5,7))} ${key.slice(0,4)}`:`${key.slice(8,10)}.${key.slice(5,7)}`;
// Провал внутрь столбика: год раскрывается в месяцы, месяц — в дни, день дальше не
// делится. Границы считаются здесь, а не на сервере: сервер получает готовое окно и
// не должен догадываться, что значит «нажали на 2024».
const bucketWindow=(key,gran)=>{
  if(gran==='year')return[`${key}-01-01`,`${key}-12-31`];
  if(gran==='month'){const y=Number(key.slice(0,4)),m=Number(key.slice(5,7));return[`${key}-01`,`${key}-${String(new Date(y,m,0).getDate()).padStart(2,'0')}`]}
  return[key,key];
};
renderers.timeline=async gen=>{
  const uid=selectedUser();
  const params=[`user_id=${q(uid)}`,'granularity=auto','limit=100'];
  if(state.timelineSince)params.push(`since=${q(state.timelineSince)}`);
  if(state.timelineUntil)params.push(`until=${q(state.timelineUntil)}`);
  const data=await api(`/api/admin/knowledge/timeline?${params.join('&')}`);
  if(gen!==renderGen)return;
  const buckets=data.buckets||[],gran=data.granularity||'year';
  const peak=Math.max(1,...buckets.map(b=>Number(b.count)||0));
  const total=buckets.reduce((sum,b)=>sum+(Number(b.count)||0),0);
  // Высота столбика — доля от самого высокого, но КЛАССОМ, а не инлайновым стилем:
  // админка запрещает их ради CSP, и это стережёт отдельный тест. Отсюда шаг в 5%
  // и низ, обрезанный снизу: столбик в один документ на фоне пятисот обязан остаться
  // видимым и нажимаемым, иначе редкие годы исчезают с картинки вовсе. Точное число
  // всё равно подписано над столбиком, так что округление ничего не скрывает.
  const bars=buckets.map(b=>`<button class="tl-bar" ${call('timelineZoom',b.bucket,gran)} title="${esc(bucketLabel(b.bucket,gran))}: ${Number(b.count)}"><span class="tl-fill tl-h${Math.max(1,Math.round(Number(b.count)/peak*20))*5}"></span><span class="tl-count">${Number(b.count)}</span><span class="tl-key">${esc(bucketLabel(b.bucket,gran))}</span></button>`).join('');
  const rows=(data.items||[]).map(it=>`<tr><td class="mono">${esc(it.document_date||'—')}</td><td><b>${esc(it.title||'Без названия')}</b><div class="mono">${esc(it.id)}</div></td><td><span class="badge">${esc(it.knowledge_kind||'note')}</span></td><td><button class="btn small primary" ${call('inspectKnowledge',it.id)}>Инспекция</button></td></tr>`);
  const scope=state.timelineSince||state.timelineUntil?`${esc(state.timelineSince||'начало')} — ${esc(state.timelineUntil||'сегодня')}`:'весь корпус';
  const granName={year:'по годам',month:'по месяцам',day:'по дням'}[gran]||gran;
  const back=state.timelineSince||state.timelineUntil?`<button class="btn" ${call('timelineReset')}>Ко всему корпусу</button>`:'';
  const undated=Number(data.undated||0);
  setApp(gen,`<section class="card"><div class="toolbar"><h2 class="grow">Хроника корпуса</h2>${back}<span class="badge">${scope}</span><span class="badge">${granName}</span></div>`
    +`<div class="notice">Лента строится по <b>собственной дате документа</b> — той, что стоит в самом файле, а не по дате загрузки. Столбик открывается нажатием: год раскрывается в месяцы, месяц — в дни.`
    +(undated?` <b>${undated.toLocaleString('ru')}</b> объектов собственной даты не имеют и в хронику не попадают вовсе.`:'')+`</div>`
    +(buckets.length?`<div class="tl-chart">${bars}</div><div class="muted">В окне ${total.toLocaleString('ru')} документов.</div>`:empty('В этом окне документов с собственной датой нет'))
    +`</section><section class="card"><div class="toolbar"><h2 class="grow">Документы окна по дате</h2><span class="badge">${rows.length}</span></div>`
    +(rows.length?table(['Дата','Документ','Вид',''],rows):empty('Пусто'))
    // Лента обрезана лимитом, и это сказано вслух: молчание читалось бы как «это всё».
    +(rows.length>=Number(data.limit||100)?`<div class="muted">Показаны первые ${rows.length} — сузьте окно, нажав на столбик.</div>`:'')
    +`</section>`);
};
actions.timelineZoom=(key,gran)=>{if(gran==='day')return;const[since,until]=bucketWindow(key,gran);state.timelineSince=since;state.timelineUntil=until;refresh()};
actions.timelineReset=()=>{state.timelineSince='';state.timelineUntil='';refresh()};
// Руководитель лежит в метаданных учётки — там же, где chat_id.
function supervisorOf(user){try{const meta=typeof user.metadata_json==='string'?JSON.parse(user.metadata_json||'{}'):(user.metadata_json||{});return String(meta.supervisor_id||'')}catch(e){return ''}}
renderers.users=async gen=>{await loadUsers(gen);const rows=state.users.map(u=>`<tr><td><b>${esc(u.display_name||u.username||u.id)}</b><div class="mono">${esc(u.id)}</div><div class="muted">${esc(u.source)} ${esc(u.external_id)}</div></td><td><span class="badge ${u.status==='active'?'ok':'bad'}">${esc(u.status)}</span></td><td><select class="field" ${chg('setPreset',u.id)}>${state.presets.map(p=>`<option value="${esc(p.preset_key)}" ${u.preset_key===p.preset_key?'selected':''}>${esc(p.name||p.preset_key)}</option>`).join('')}</select></td><td><select class="field" ${chg('setSupervisor',u.id)}><option value="">— никому не подчинён —</option>${state.users.filter(o=>o.id!==u.id).map(o=>`<option value="${esc(o.id)}" ${supervisorOf(u)===o.id?'selected':''}>${esc(o.display_name||o.username||o.id)}</option>`).join('')}</select></td><td>${fmtDate(u.last_seen_at)}</td><td><button class="btn small" ${call('permissionDialog',u.id)}>Права</button> <button class="btn small ${u.status==='active'?'danger':'good'}" ${call('setUserStatus',u.id,u.status==='active'?'disabled':'active')}>${u.status==='active'?'Отключить':'Включить'}</button></td></tr>`);setApp(gen,`<section class="card"><div class="toolbar"><h2 class="grow">Пользователи и роли</h2><button class="btn" ${call('createUserDialog')}>Добавить</button><span class="badge">${Number(state.usersTotal||rows.length)}</span></div>${rows.length?table(['Пользователь','Статус','Пресет','Подчинён','Последняя активность','Действия'],rows):empty('Пользователи не найдены')}</section>`)};
actions.setPreset=async(id,preset_key)=>{try{await api(`/api/admin/users/${q(id)}/preset`,{method:'POST',body:JSON.stringify({preset_key})});toast('Пресет назначен');await loadUsers()}catch(e){toast(e.message,true)}};
// Кто чей руководитель. От этого зависит, чью деятельность человек вправе
// смотреть: право надзора говорит «можно смотреть чужое», но не «можно смотреть
// ЛЮБОГО». Пустое значение снимает подчинение.
actions.setSupervisor=async(id,supervisor_id)=>{try{await api(`/api/admin/users/${q(id)}/supervisor`,{method:'POST',body:JSON.stringify({supervisor_id})});toast(supervisor_id?'Руководитель назначен':'Подчинение снято');await loadUsers()}catch(e){toast(e.message,true)}};
actions.setUserStatus=async(id,status)=>{try{await api(`/api/admin/users/${q(id)}`,{method:'PATCH',body:JSON.stringify({status})});toast('Статус изменён');await loadUsers();refresh()}catch(e){toast(e.message,true)}};
actions.permissionDialog=id=>{const u=state.users.find(item=>item.id===id);if(!u)return;const overrides=u.permission_overrides||{};openModal(`Права: ${u.display_name||u.id}`,`<div class="table-wrap"><table><thead><tr><th>Capability</th><th>Риск</th><th>Override</th></tr></thead><tbody>${state.capabilities.map(c=>`<tr><td><b>${esc(c.security_id)}</b><div class="muted">${esc(c.description)}</div></td><td>${esc(c.risk_level)}</td><td><select class="field" ${chg('setOverride',u.id,c.security_id)}><option value="inherit" ${!overrides[c.security_id]?'selected':''}>inherit</option><option value="allow" ${overrides[c.security_id]==='allow'?'selected':''}>allow</option><option value="deny" ${overrides[c.security_id]==='deny'?'selected':''}>deny</option></select></td></tr>`).join('')}</tbody></table></div>`,`<button class="btn" ${call('closeModal')}>Готово</button>`)};
actions.setOverride=async(id,cap,effect)=>{try{await api(`/api/admin/users/${q(id)}/permissions/${q(cap)}`,{method:'PUT',body:JSON.stringify({effect})});toast('Override сохранён');await loadUsers()}catch(e){toast(e.message,true)}};
actions.createUserDialog=()=>openModal('Новый пользователь',`<div class="form"><label>ID<input id="newUserId" placeholder="local:alice"></label><label>Отображаемое имя<input id="newUserName"></label><label>Пресет<select id="newUserPreset" class="field">${state.presets.map(p=>`<option value="${esc(p.preset_key)}">${esc(p.name||p.preset_key)}</option>`).join('')}</select></label></div>`,`<button class="btn" ${call('closeModal')}>Отмена</button><button class="btn primary" ${call('createUser')}>Создать</button>`);
actions.createUser=async()=>{try{await api('/api/admin/users',{method:'POST',body:JSON.stringify({id:document.getElementById('newUserId').value,display_name:document.getElementById('newUserName').value,preset_key:document.getElementById('newUserPreset').value})});closeModal();toast('Пользователь создан');await loadUsers();refresh()}catch(e){toast(e.message,true)}};
// --- Активность: что и когда конкретный человек писал и загружал ---------
// The one screen that reads across accounts, so it says out loud whose account it
// is showing and how that account was chosen — a name typed as «Ивану» resolves
// through case endings, layout and transliteration, and a tolerant match that
// landed on the wrong person has to be visible rather than implied.
const ACTIVITY_PAGE = 50;
const ACTIVITY_PERIODS = [['', 'Всё время'], ['7', '7 дней'], ['30', '30 дней'], ['90', '90 дней']];
function activityWindow(days) { if (!days) return ''; const from = new Date(Date.now() - Number(days) * 864e5); return from.toISOString(); }
function activityQuery(offset) { const parts = [`limit=${ACTIVITY_PAGE}`, `offset=${offset}`, 'analysis=topics', 'analysis=rhythm', 'analysis=volume', 'analysis=change']; if (state.activitySince) parts.push('since=' + q(state.activitySince)); if (state.activityUntil) parts.push('until=' + q(state.activityUntil)); return parts.join('&'); }
renderers.activity = async gen => {
  await loadUsers(gen);
  const target = selectedUser();
  if (!target) { setApp(gen,`<section class="card">${empty('Нет ни одного аккаунта')}</section>`); return; }
  const data = await api(`/api/admin/users/${q(target)}/activity?${activityQuery(state.activityOffset)}`);
  if(gen!==renderGen)return;
  state.activity = data.items || []; state.activitySummary = data.summary || null;
  state.activityAnalysis = data.analysis || null;
  const who = state.users.find(u => u.id === target) || {};
  const s = state.activitySummary || {};
  const found = state.activityFound;
  const foundBlock = !found ? '' : found.unambiguous
    ? `<div class="notice">Найден: <b>${esc(found.unambiguous.display_name || found.unambiguous.user_id)}</b> — совпадение <span class="mono">${esc(found.unambiguous.method)}</span>${found.unambiguous.method === 'exact' ? '' : ' (не точное написание — проверьте, тот ли это человек)'}</div>`
    : found.matches.length
      ? `<div class="notice"><b>Под это имя подходят несколько аккаунтов.</b> Выберите нужный: ${found.matches.map(m => `<button class="btn small" ${call('activityPick', m.user_id)}>${esc(m.display_name || m.user_id)} <span class="muted">${esc(m.method)}</span></button>`).join(' ')}</div>`
      : `<div class="notice">Никто не найден по этому имени.</div>`;
  const periodButtons = ACTIVITY_PERIODS.map(([days, label]) => `<button class="btn small ${(state.activitySince === activityWindow(days)) ? 'primary' : ''}" ${call('activityPeriod', days)}>${label}</button>`).join(' ');
  const cards = [['Поступлений', s.arrivals], ['Знаний', s.knowledge_objects], ['В Inbox', s.pending_inbox], ['Сообщений', s.messages]]
    .map(([l, v]) => `<div class="card stat"><div class="value">${Number(v || 0).toLocaleString('ru')}</div><div class="label">${l}</div></div>`).join('');
  const bySource = (s.by_source || []).map(r => `<span class="badge">${esc(r.source)}: ${r.count}</span>`).join(' ') || '<span class="muted">—</span>';
  const byDay = (s.by_day || []).slice(0, 14);
  const peak = Math.max(1, ...byDay.map(d => d.count));
  const dayBars = byDay.length ? byDay.slice().reverse().map(d => `<div class="kv"><div class="mono">${esc(d.day)}</div><div><span class="badge ok">${d.count}</span> <span class="muted">${'▬'.repeat(Math.max(1, Math.round(d.count / peak * 20)))}</span></div></div>`).join('') : empty('Нет активности за период');
  const rows = state.activity.map((item, index) => `<tr><td class="mono">${fmtDate(item.at)}</td><td><span class="badge ${item.activity === 'upload' ? 'warn' : 'ok'}">${item.activity === 'upload' ? 'загрузил' : 'написал'}</span><div class="muted">${esc(item.source)}</div></td><td><b>${esc(short(item.title || '—', 70))}</b>${item.filename ? `<div class="mono">${esc(item.filename)}</div>` : ''}</td><td>${item.size_bytes ? Number(item.size_bytes).toLocaleString('ru') + ' Б' : `${Number(item.content_chars || 0).toLocaleString('ru')} симв.`}</td><td>${item.knowledge_object_id ? `<span class="badge ok">в знаниях</span>` : item.inbox_status ? `<span class="badge">${esc(item.inbox_status)}</span>` : '<span class="muted">—</span>'}</td><td>${item.redacted ? '<span class="muted" title="Ваш уровень доступа показывает объём и судьбу, но не написанное">скрыто</span>' : `<button class="btn small" ${call('activityPreview', item.raw_object_id)}>Показать</button>`}</td></tr>`);
  // Общий `pager`, а не своя копия: этот экран писался первым, до появления
  // хелпера, и его собственный пейджер собирался в переменную, которую строка
  // ниже потом не подставляла — в разметку уходил `${pager}`, то есть ИСХОДНИК
  // стрелочной функции. Две реализации одного пейджера ровно так и расходятся.
  const activityPager = pager('activityPage', state.activityOffset, state.activity.length, s.arrivals);
  const an = state.activityAnalysis;
  const countChips = (rows, key, label) => rows && rows.length ? rows.map(r => `<span class="badge">${esc(String(r[key]))}: ${r.count}</span>`).join(' ') : `<span class="muted">${label}</span>`;
  const hours = an && an.by_hour ? an.by_hour : [];
  const hourPeak = Math.max(1, ...hours.map(h => h.count));
  const hourBars = hours.length ? hours.map(h => `<div class="kv"><div class="mono">${esc(h.hour)}:00</div><div><span class="badge ok">${h.count}</span> <span class="muted">${'▬'.repeat(Math.max(1, Math.round(h.count / hourPeak * 16)))}</span></div></div>`).join('') : empty('Нет данных');
  const ch = an && an.change ? an.change : null;
  const delta = !ch ? '' : !ch.available
    ? `<div class="muted">Выберите период — сравнивать текущее окно не с чем.</div>`
    : `<div class="kv"><div>Поступлений сейчас</div><div><b>${ch.arrivals_now}</b> против <b>${ch.arrivals_before}</b> за предыдущие столько же</div></div>`
      + (ch.new_topics && ch.new_topics.length ? `<div class="kv"><div>Появилось</div><div>${ch.new_topics.map(t => `<span class="badge ok">${esc(t)}</span>`).join(' ')}</div></div>` : '')
      + (ch.dropped_topics && ch.dropped_topics.length ? `<div class="kv"><div>Пропало</div><div>${ch.dropped_topics.map(t => `<span class="badge warn">${esc(t)}</span>`).join(' ')}</div></div>` : '')
      + (ch.topics_redacted ? `<div class="muted mt8">Темы скрыты вашим уровнем доступа.</div>` : '');
  const analysisBlock = !an ? '' : `
<div class="grid two"><section class="card"><h2>О чём пишет</h2><div class="mt8">${an.topics_redacted ? '<span class="muted">Скрыто вашим уровнем доступа</span>' : countChips(an.topics, 'topic', 'Тем не найдено')}</div>${an.topics_total > (an.topics || []).length ? `<div class="muted mt8">Показано ${(an.topics || []).length} из ${an.topics_total}</div>` : ''}<div class="mt8">${an.topics_redacted ? '' : countChips(an.kinds, 'kind', '')}</div></section>
<section class="card"><h2>Когда работает</h2>${hourBars}</section></div>
<div class="grid two"><section class="card"><h2>Что изменилось</h2>${delta}</section>
<section class="card"><h2>Объём</h2>${an.volume ? `<div class="kv"><div>Символов</div><div><b>${Number(an.volume.chars || 0).toLocaleString('ru')}</b></div></div><div class="kv"><div>Дней с активностью</div><div><b>${an.volume.active_days}</b></div></div><div class="kv"><div>Символов в активный день</div><div><b>${Number(an.volume.chars_per_active_day || 0).toLocaleString('ru')}</b></div></div>` : empty('Нет данных')}</section></div>`;
  setApp(gen,`
<section class="card"><div class="toolbar"><h2 class="grow">Активность: ${esc(who.display_name || who.username || target)}</h2><span class="badge">${esc(target)}</span></div>
<div class="toolbar"><input class="field grow" id="activityName" placeholder="Найти по имени — «Иван», «у Ивана», можно с опечаткой" ${chg('activityFind')}><span>${periodButtons}</span></div>
${foundBlock}
<div class="muted mt8">Первое поступление: ${fmtDate(s.first_at)} · последнее: ${fmtDate(s.last_at)}</div></section>
<div class="grid stats">${cards}</div>
<div class="grid two"><section class="card"><h2>Откуда приходило</h2><div class="mt8">${bySource}</div></section><section class="card"><h2>По дням</h2>${dayBars}</section></div>
${analysisBlock}
<section class="card"><div class="toolbar"><h2 class="grow">Что и когда</h2></div>${rows.length ? table(['Когда', 'Что', 'Название', 'Объём', 'Судьба', ''], rows) : empty('За выбранный период ничего нет')}${activityPager}</section>`);
};
actions.activityPeriod = async days => { state.activitySince = activityWindow(days); state.activityOffset = 0; await refresh() };
actions.activityPage = async direction => { const next = state.activityOffset + direction * ACTIVITY_PAGE; state.activityOffset = Math.max(0, next); await refresh() };
actions.activityPick = async userId => { state.userId = userId; state.activityFound = null; state.activityOffset = 0; const select = document.getElementById('userSelect'); if (select) select.value = userId; await refresh() };
actions.activityFind = async name => {
  name = String(name || '').trim();
  if (!name) { state.activityFound = null; await refresh(); return }
  try {
    const found = await api(`/api/admin/users/resolve?name=${q(name)}`);
    state.activityFound = found;
    // A single clear match jumps straight to that account; anything else is shown
    // as a question, because picking one of two people here shows the wrong person.
    if (found.unambiguous) { state.userId = found.unambiguous.user_id; state.activityOffset = 0; const select = document.getElementById('userSelect'); if (select) select.value = state.userId }
    await refresh();
  } catch (e) { toast(e.message, true) }
};
actions.activityPreview = rawId => {
  // По идентификатору Raw Object, не по позиции строки: на этом экране расхождение
  // состояния и таблицы означало бы показ материала ЧУЖОГО аккаунта.
  const item = (state.activity || []).find(row => row.raw_object_id === rawId);
  if (!item) { toast('Запись не найдена — обновите раздел', true); return }
  openModal(item.title || 'Материал', `<div class="kv"><div>Когда</div><div>${fmtDate(item.at)}</div><div>Как</div><div>${esc(item.activity)} / ${esc(item.source)}</div>${item.filename ? `<div>Файл</div><div class="mono">${esc(item.filename)}</div>` : ''}<div>Объём</div><div>${Number(item.content_chars || 0).toLocaleString('ru')} символов</div><div>Судьба</div><div>${item.knowledge_object_id ? 'в знаниях' : item.inbox_status ? esc(item.inbox_status) : '—'}</div></div><div class="pre prewrap mt12">${esc(item.preview)}${item.content_chars > item.preview.length ? '\n\n…' : ''}</div>`);
};

renderers.conversations=async gen=>{const uid=selectedUser();const data=await api(`/api/admin/conversations?user_id=${q(uid)}&include_archived=true&limit=${PAGE}&offset=${state.conversationsOffset}`);const rows=(data.items||[]).map(c=>`<tr><td><b>${esc(c.title||'Диалог')}</b><div class="mono">${esc(c.id)}</div></td><td><span class="badge">${esc(modeName(c.mode))}</span></td><td>${fmtDate(c.created_at)}</td><td>${fmtDate(c.updated_at)}</td><td><span class="badge ${c.is_archived?'warn':'ok'}">${c.is_archived?'archived':'active'}</span></td><td><button class="btn small" ${call('showConversation',c.id)}>Сообщения</button> <button class="btn small" ${call('archiveConversation',c.id,!c.is_archived)}>${c.is_archived?'Разархивировать':'Архивировать'}</button> <button class="btn small" ${call('deleteConversation',c.id)}>Убрать из списка</button></td></tr>`);setApp(gen,`<section class="card"><h2>Диалоги пользователя</h2><div class="notice">Режим сохраняется для каждого диалога отдельно. Telegram-команды /chat, /work и /research переключают его явно.</div>${rows.length?table(['Диалог','Режим','Создан','Обновлён','Статус',''],rows):empty('Диалогов пока нет')}${pager('conversationsPage',state.conversationsOffset,(data.items||[]).length,data.total)}</section>`)};
actions.showConversation=async id=>{try{const d=await api(`/api/admin/conversations/${q(id)}/messages?user_id=${q(selectedUser())}&limit=1000`);openModal('История диалога',`<div class="grid">${(d.items||[]).map(m=>{const ins=m.insights||{};const cites=(ins.citations||[]).filter(c=>c.title||c.label);const legend=cites.length?`<div class="mono mt6">📎 Источники: ${cites.map(c=>`${c.label?`[${esc(c.label)}] `:''}${esc(c.title||c.knowledge_id)}${c.knowledge_id?` <button class="btn small" ${call('inspectKnowledge',c.knowledge_id)}>Открыть</button>`:''}`).join('; ')}</div>`:'';const grounded=ins.answer_grounded===false?` <span class="badge warn">без ссылок</span>`:(ins.answer_grounded===true?` <span class="badge ok">с источниками</span>`:'');const vs=ins.verification_status;const ver=vs&&vs!=='skipped'?` <span class="badge ${vs==='passed'?'ok':vs==='failed'?'bad':'warn'}">${esc(vs)}</span>`:'';return `<div class="card"><span class="badge">${esc(m.role)}</span>${grounded}${ver} <span class="muted">${fmtDate(m.created_at)}</span><div class="mt8 prewrap">${esc(m.content)}</div>${legend}</div>`}).join('')||empty('Сообщений нет')}</div>`,`<button class="btn" ${call('closeModal')}>Закрыть</button>`)}catch(e){toast(e.message,true)}};
actions.archiveConversation=async(id,archived)=>{try{await api(`/api/admin/conversations/${q(id)}/archive?user_id=${q(selectedUser())}`,{method:'POST',body:JSON.stringify({archived})});toast(archived?'Диалог архивирован':'Диалог разархивирован');refresh()}catch(e){toast(e.message,true)}};
// Обещание «это необратимо» перестало быть правдой в схеме 23: сказанное в чате
// неудаляемо, и метод убирает диалог из списка, сохраняя переписку. Текст должен
// говорить то, что произойдёт, иначе человек нажимает кнопку, рассчитывая на
// удаление, и получает архивирование.
actions.deleteConversation=async id=>{if(!confirm('Убрать диалог из списка? Переписка сохранится — сообщения чата не удаляются.'))return;try{await api(`/api/admin/conversations/${q(id)}?user_id=${q(selectedUser())}`,{method:'DELETE'});toast('Диалог убран из списка, переписка сохранена');refresh()}catch(e){toast(e.message,true)}};
renderers.files=async gen=>{const uid=selectedUser();const data=await api(`/api/admin/files?user_id=${q(uid)}&limit=${PAGE}&offset=${state.filesOffset}`);const rows=(data.items||[]).map(f=>{const m=f.metadata||{};return `<tr><td><b>${esc(m.filename||f.source_ref||f.id)}</b><div class="mono">${esc(f.id)}</div></td><td>${esc(m.mime_type||'—')}</td><td>${Number(m.size_bytes||0).toLocaleString('ru')} байт</td><td>${fmtDate(f.received_at)}</td><td><button class="btn small" ${call('download',`/api/admin/files/${q(f.id)}/download?user_id=${q(uid)}`)}>Скачать</button></td></tr>`});setApp(gen,`<section class="card"><h2>Файлы пользователя</h2>${rows.length?table(['Файл','MIME','Размер','Получен',''],rows):empty('Файлов пока нет')}${pager('filesPage',state.filesOffset,(data.items||[]).length,data.total)}</section>`)};
renderers.backups=async gen=>{const data=await api('/api/admin/backups');const rows=(data.items||[]).map(b=>`<tr><td><b>${esc(b.database)}</b><div class="mono">${esc(b.sha256)}</div></td><td>${fmtDate(b.created_at)}</td><td>${Number(b.size_bytes||0).toLocaleString('ru')} байт</td><td><span class="badge ${b.integrity_check==='ok'?'ok':'bad'}">${esc(b.integrity_check)}</span></td><td><button class="btn small" ${call('verifyBackup',b.database)}>Проверить</button> <button class="btn small" ${call('download',`/api/admin/backups/${q(b.database)}/download`,b.database)}>Скачать</button></td></tr>`);setApp(gen,`<section class="card"><div class="toolbar"><h2 class="grow">Резервные копии SQLite</h2><button class="btn primary" ${call('createBackup')}>Создать сейчас</button><button class="btn" ${call('exportUser')}>Экспорт пользователя</button></div>${rows.length?table(['Копия','Создана','Размер','Целостность','Действия'],rows):empty('Копий ещё нет')}</section><div class="notice"><b>Восстановление выполняется только из CLI при остановленном backend:</b> <span class="mono">jericho restore-backup &lt;имя.sqlite3&gt; --yes</span>. Команда повторно проверяет manifest/hash/schema/FK, создаёт предаварийную копию и атомарно откатывается при сбое. Встроенная копия содержит только SQLite; файлы, vault, Telegram queue, конфигурация и веса резервируются отдельно.</div>`)};
actions.createBackup=async()=>{try{const d=await api('/api/admin/backups',{method:'POST',body:JSON.stringify({label:'admin-ui'})});toast(`Создано: ${d.backup.database}`);refresh()}catch(e){toast(e.message,true)}};
actions.verifyBackup=async name=>{try{const d=await api(`/api/admin/backups/${q(name)}/verify`,{method:'POST'});toast(d.verification.ok?'Копия исправна':'Проверка не пройдена',!d.verification.ok)}catch(e){toast(e.message,true)}};
actions.exportUser=async()=>{try{const d=await api('/api/admin/exports',{method:'POST',body:JSON.stringify({user_id:selectedUser()})});toast(`Экспорт готов: ${d.export.filename}`);download(`/api/admin/exports/${q(d.export.filename)}/download`,d.export.filename)}catch(e){toast(e.message,true)}};
renderers.audit=async gen=>{const data=await api(`/api/admin/audit?limit=${PAGE}&offset=${state.auditOffset}${state.auditAnchor?`&before=${q(state.auditAnchor)}`:''}`);if(gen!==renderGen)return;state.audit=data.items||[];if(!state.auditAnchor)state.auditAnchor=data.anchor||null;const rows=state.audit.map((a,index)=>`<tr><td>${fmtDate(a.created_at)}</td><td><b>${esc(a.action)}</b></td><td class="mono">${esc(a.user_id)}</td><td>${esc(a.target_type)}<div class="mono">${esc(a.target_id||'')}</div></td><td>${esc(a.ip_address||'—')}</td><td><button class="btn small" ${call('showJson',a.id)}>Детали</button></td></tr>`);setApp(gen,`<section class="card"><h2>Журнал административных и инструментальных действий</h2>${rows.length?table(['Время','Действие','Актор','Объект','IP',''],rows):empty('Записей аудита пока нет')}${pager('auditPage',state.auditOffset,state.audit.length,data.total)}</section>`)};
// По идентификатору, а не по позиции: если состояние и таблица всё же разойдутся,
// пусть будет «запись не найдена», а не чужая строка журнала, выданная за эту.
actions.showJson=id=>{const entry=(state.audit||[]).find(a=>a.id===id);openModal('Детали',`<div class="pre">${esc(entry?JSON.stringify(entry,null,2):'Запись не найдена — обновите раздел')}</div>`,`<button class="btn" ${call('closeModal')}>Закрыть</button>`)};
// Состояние ЖИВОЙ пробы, а не флага настройки. Отдельной функцией, потому что
// различать надо три случая, и они значат разное: выключено (человек так решил),
// недоступно (сервис лежит), отвечает но не ту модель (вектора несравнимы).
// Покрытие и место — числа, которые СОБИРАЛИСЬ и лежали в свёрнутом JSON-дампе.
// Сопоставить покрытие с числом объектов было некому, а порога по диску не
// существовало вовсе: при 99% занятости состояние оставалось «ready».
function coverageCell(index){
  if(!index||!index.available)return '<span class="muted">—</span>';
  const expected=Number(index.expected_objects||0),got=Number(index.indexed_objects||0);
  if(!expected)return `${got}`;
  const share=Number(index.coverage||0);
  return `<span class="badge ${share>=0.9?'ok':'warn'}">${Math.round(share*100)}%</span> <span class="muted">${got} из ${expected}</span>`;
}
function diskCell(disk){
  if(!disk||!disk.total_bytes)return '<span class="muted">—</span>';
  const free=Number(disk.free_bytes||0),total=Number(disk.total_bytes||0);
  const share=total?free/total:0;
  return `<span class="badge ${share<0.05?'bad':share<0.15?'warn':'ok'}">${(free/1e9).toFixed(1)} ГБ</span> <span class="muted">из ${(total/1e9).toFixed(0)}</span>`;
}
function endpointCell(probe,enabled){
  if(!enabled)return '<span class="badge">выключен</span>';
  if(!probe)return '<span class="badge warn">не проверялся</span>';
  if(!probe.reachable)return '<span class="badge bad">недоступен</span>';
  if(probe.model_served===false)return `<span class="badge bad">не отдаёт «${esc(probe.model_expected||'')}»</span>`;
  return `<span class="badge ok">отвечает</span> <span class="muted">${esc(probe.model_expected||'')}</span>`;
}
renderers.diagnostics=async gen=>{
  // `check_llm=true` обязателен, и без него экран врал: он печатал «LLM: включён» из
  // ФАЙЛА КОНФИГУРАЦИИ, то есть показывал ровно то же самое при выключенной машине с
  // моделью. Живые пробы (доступен ли endpoint, отдаёт ли он настроенную модель) в
  // вебе не выполнялись никогда, и действия start_llm_runtime / llm_model_not_served
  // были физически недостижимы из админки.
  const [d,s]=await Promise.all([api('/api/admin/diagnostics?check_llm=true'),api('/api/admin/settings')]);const db=d.database||{},backup=d.backups||{},workers=d.workers||{},lease=d.backend_lease||{},bq=d.bridge_queue||{};const actionCards=(d.actions||[]).map(a=>`<div class="card ${a.severity==='error'?'error':''}"><div class="toolbar"><span class="badge ${a.severity==='error'?'bad':a.severity==='warning'?'warn':'ok'}">${esc(a.severity)}</span><b>${esc(a.title)}</b></div><div>${esc(a.detail)}</div>${a.command?`<div class="pre mt10">${esc(a.command)}</div>`:''}</div>`).join('');setApp(gen,`<div class="grid stats"><div class="card stat"><div class="value">${esc(d.state||'unknown')}</div><div class="label">Общее состояние</div></div><div class="card stat"><div class="value">${esc(db.schema_version??'—')}</div><div class="label">Схема SQLite</div></div><div class="card stat"><div class="value">${backup.verified?'verified':esc(backup.state||'none')}</div><div class="label">Последняя копия</div></div><div class="card stat"><div class="value">${workers.healthy?'healthy':'attention'}</div><div class="label">Workers</div></div></div><div class="grid two"><section class="card"><h2>Операционное состояние</h2><div class="kv"><div>SQLite</div><div><span class="badge ${db.ok?'ok':'bad'}">${esc(db.state||db.integrity_check)}</span></div><div>Backend lease</div><div>${esc(lease.state||'unknown')} ${lease.pid?`· PID ${esc(lease.pid)}`:''}</div><div>Worker-задач</div><div>${Number(workers.task_count||0)}; failures ${(workers.degraded_tasks||[]).length}; stale ${(workers.stale_tasks||[]).length}</div><div>Очередь моста</div><div>${bq.state==='present'?`${Number(bq.pending||0)} pending · <span class="badge ${Number(bq.dead_letter||0)?'bad':'ok'}">${Number(bq.dead_letter||0)} dead-letter</span>`:'—'}</div><div>LLM</div><div>${endpointCell(d.llm_endpoint,d.features?.llm_enabled)}</div><div>Эмбеддинги</div><div>${endpointCell(d.embeddings_endpoint,d.features?.embeddings_enabled)}</div><div>Переранжировщик</div><div>${endpointCell(d.rerank_endpoint,d.rerank_endpoint!==undefined)}</div><div>Покрытие индексом</div><div>${coverageCell(d.embeddings_index)}</div><div>Свободно на диске</div><div>${diskCell(d.runtime?.disk)}</div></div></section><section class="card"><h2>Рекомендуемые действия</h2>${actionCards||empty('Действий не требуется')}</section></div><div class="grid two mt16"><section class="card"><details><summary>Полная диагностика</summary><div class="pre">${esc(JSON.stringify(d,null,2))}</div></details></section><section class="card"><details><summary>Активная конфигурация</summary><div class="pre">${esc(JSON.stringify(s,null,2))}</div></details></section></div>`)};
async function health(){try{const d=await fetch('/api/health').then(r=>r.json());const dot=document.getElementById('healthDot');dot.classList.remove('health-bad');dot.classList.add('health-ok');document.getElementById('healthText').textContent=`${d.status} · ${d.version}`;}catch{const dot=document.getElementById('healthDot');dot.classList.remove('health-ok');dot.classList.add('health-bad');document.getElementById('healthText').textContent='сервер недоступен'}}
async function bootstrap(){state.view=startingView();document.getElementById('pageTitle').textContent=views.find(v=>v[0]===state.view)?.[1]||state.view;renderNav();await health();if(!state.token){openTokenDialog();return}await loadUsers();await refresh();if(state.view==='chats')startLiveChats();handleSaveHash()}
function handleSaveHash(){const m=/[#&]save=([^&]*)/.exec(location.hash||'');if(!m||!m[1])return;let url='';try{url=decodeURIComponent(m[1])}catch(e){return}history.replaceState(null,'',location.pathname+location.search);if(/^https?:\/\//i.test(url))actions.ingestUrlDialog(url)}
// Обработчики регистрируются ЯВНО: без этой строки кнопка рисуется, клик
// доходит до диспетчера и молча теряется — поймано браузерным прогоном,
// строка ленты не открывала переписку и ошибок в консоли не было.
Object.assign(actions,{navigate,refresh,toggleMenu,openTokenDialog,saveToken,clearToken,closeModal,download,openChat,sendReply});
// Кнопки «назад»/«вперёд» браузера тоже переключают вкладку.
window.addEventListener('hashchange',()=>{const view=startingView();if(view!==state.view)navigate(view,{push:false})});
bootstrap();setInterval(health,30000);

