'use strict';
const $ = (selector) => document.querySelector(selector);
const state = {employees: [], report: null};
let toastTimeout;
const minutes = (ms) => {
  const total = Math.floor(Math.max(0,ms) / 60000);
  return `${String(Math.floor(total / 60)).padStart(2,'0')}:${String(total % 60).padStart(2,'0')}`;
};
function element(tag, cls = '', text = '') {
  const node = document.createElement(tag);
  if(cls) node.className = cls;
  node.textContent = text;
  return node;
}
function toast(message, error=false) {
  const node=$('#toast'); node.textContent=message; node.classList.toggle('error',error); node.classList.add('visible');
  clearTimeout(toastTimeout); toastTimeout=setTimeout(()=>node.classList.remove('visible'),3400);
}
async function request(url, data) {
  const response=await fetch(url, {method: data===undefined?'GET':'POST',headers: data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});
  let value;
  try { value=await response.json(); } catch { throw new Error('Некорректный ответ сервера'); }
  if(!response.ok) throw new Error(value.error || 'Ошибка запроса');
  return value;
}
function dateLocal(date){const y=date.getFullYear(), m=String(date.getMonth()+1).padStart(2,'0'), d=String(date.getDate()).padStart(2,'0');return `${y}-${m}-${d}`;}
function dateRange(){const from=$('#dateFrom').value,to=$('#dateTo').value;if(!from||!to||to<from)throw new Error('Выберите корректный период');const start=new Date(`${from}T00:00:00`),end=new Date(`${to}T00:00:00`);end.setDate(end.getDate()+1);return {from:start.toISOString(),to:end.toISOString()};}
function makeEmpty(title, subtitle) {const wrap=element('div','empty');wrap.append(element('div','empty-icon','◎'),element('strong','',title),element('div','',subtitle));return wrap;}
function renderEmployees(){
  const list=$('#employeeList');list.replaceChildren();
  $('#employeeCount').textContent=state.employees.length;$('#teamCount').textContent=state.employees.length;
  const active=state.employees.filter(e=>e.active_id);$('#runningCount').textContent=active.length;
  if(!state.employees.length) list.append(makeEmpty('Команда пока пуста','Добавьте первого сотрудника выше'));
  for(const employee of state.employees){
    const row=element('div','employee-row'),avatar=element('div','avatar',employee.name[0].toUpperCase()),info=element('div','employee-info');
    info.append(element('div','employee-name',employee.name),element('div',`employee-state ${employee.active_id?'on':''}`,employee.active_id?'● Сейчас работает':'○ Не в работе'));
    row.append(avatar,info);list.append(row);
  }
  const selected=$('#employeeSelect').value;$('#employeeSelect').replaceChildren(element('option','','Выберите сотрудника'));
  $('#employeeSelect').firstChild.value='';
  for(const employee of state.employees){const opt=element('option','',employee.name);opt.value=String(employee.id);$('#employeeSelect').append(opt);}
  if(state.employees.some(e=>String(e.id)===selected))$('#employeeSelect').value=selected;
  renderRunning();
}
function renderRunning(){
  const list=$('#activeTimers');list.replaceChildren();
  const active=state.employees.filter(e=>e.active_id);
  if(!active.length){list.append(element('p','muted','Нет активных таймеров'));return;}
  for(const e of active){
    const row=element('div','timer-item'),info=element('div'),actions=element('div','timer-actions');
    info.append(element('div','timer-person',e.name),element('div','timer-task',e.active_description));
    const clock=element('div','timer-clock');clock.dataset.start=String(e.active_since);
    const stop=element('button','stop-button','■ Стоп');stop.type='button';stop.addEventListener('click',async()=>{
      stop.disabled=true;
      try {await request('/api/timers/stop',{employee_id:e.id});toast(`Таймер сотрудника «${e.name}» остановлен`);await refresh();}
      catch(err){toast(err.message,true);stop.disabled=false;await reloadEmployees();}
    });
    actions.append(clock,stop);row.append(info,actions);list.append(row);
  }
  tick();
}
function tick(){for(const node of document.querySelectorAll('[data-start]'))node.textContent=minutes(Date.now()-Number(node.dataset.start));}
function readableDate(ms){return new Date(ms).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});}
function renderReport(){
  const list=$('#reportContent');list.replaceChildren();const report=state.report;if(!report)return;
  const totals=report.employees.reduce((s,e)=>s+e.total_ms,0);$('#periodTotal').textContent=minutes(totals);
  if(!report.employees.length){list.append(makeEmpty('Здесь появится статистика','Добавьте сотрудников и запустите таймер'));return;}
  const table=element('table','report-table'),head=element('thead'),hrow=element('tr');
  for(const label of ['Сотрудник и задачи','Записей','Всего времени'])hrow.append(element('th','',label));head.append(hrow);table.append(head);
  const tbody=element('tbody');
  for(const e of report.employees){
    const row=element('tr'),name=element('td'),nameWrap=element('div','table-name',e.name);
    name.append(nameWrap);const entryCount=e.entries.length;
    if(entryCount){
      const details=element('details','details'),summary=element('summary','',`Посмотреть задачи (${entryCount})`),ul=element('ul','details-list');
      for(const record of e.entries){
        const item=element('li','',record.description);let suffix=`${readableDate(record.started_at_ms)} — ${record.ended_at_ms===null?'идёт сейчас':readableDate(record.ended_at_ms)}`;
        item.append(element('small','',`${suffix} · ${minutes(record.period_ms)}`));ul.append(item);
      }
      details.append(summary,ul);name.append(details);
    }else name.append(element('div','task-count','Записей за период нет'));
    row.append(name,element('td','',String(entryCount)),element('td','hours',minutes(e.total_ms)));tbody.append(row);
  }
  table.append(tbody);list.append(table);
  if(!report.employees.some(e=>e.entries.length))list.append(element('p','report-empty','За выбранный период записей нет. Попробуйте выбрать другие даты.'));
}
async function reloadEmployees(){const data=await request('/api/employees');state.employees=data.employees;renderEmployees();}
async function reloadReport(){const {from,to}=dateRange();state.report=await request(`/api/report?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);renderReport();}
async function refresh(){await Promise.all([reloadEmployees(),reloadReport()]);}
async function guarded(callback){try{await callback();}catch(error){toast(error.message||'Ошибка сети',true);}}
$('#addForm').addEventListener('submit',e=>{e.preventDefault();guarded(async()=>{const name=$('#employeeName').value.trim();await request('/api/employees',{name});$('#employeeName').value='';toast(`Сотрудник «${name}» добавлен`);await refresh();});});
$('#timerForm').addEventListener('submit',e=>{e.preventDefault();guarded(async()=>{const employee_id=Number($('#employeeSelect').value),description=$('#taskDescription').value.trim();if(!employee_id)throw new Error('Выберите сотрудника');await request('/api/timers/start',{employee_id,description});$('#taskDescription').value='';toast('Таймер запущен');await refresh();});});
$('#reportForm').addEventListener('submit',e=>{e.preventDefault();guarded(reloadReport);});
const today=new Date();$('#dateTo').value=dateLocal(today);const monthStart=new Date(today.getFullYear(),today.getMonth(),1);$('#dateFrom').value=dateLocal(monthStart);
setInterval(tick,1000);guarded(refresh);