let storageSequence=0;
const storageMoney=v=>v==null?'Цена неизвестна':Number(v).toLocaleString('ru-RU',{style:'currency',currency:'RUB',minimumFractionDigits:2,maximumFractionDigits:2});
const storageDate=v=>v?String(v).split('-').reverse().join('.'):'Неизвестно';
function storageRange(p){
 if(p.high==null)return 'от '+storageMoney(p.low)+' + неоценённая часть';
 return Math.abs(Number(p.high)-Number(p.low))<0.01?storageMoney(p.low):storageMoney(p.low)+' — '+storageMoney(p.high);
}
function storagePage(d){
 return '<div id="storage-forecast-root" aria-live="polite">'+panel('Прогноз стоимости хранения',empty('Загружаем начисления и бесплатные лимиты…'))+'</div>'+
 panel('Начислено за выбранный период',d.storage_fees.length?d.storage_fees.map(r=>row(storageDate(r.day),'По отчёту Ozon',storageMoney(r.fee),detail('Платных единиц',num(r.paid_units)))).join(''):empty('Нет начислений за выбранный период'))+
 panel('Состояние отчётов',d.reports.map(r=>row(r.kind==='supplies'?'Бесплатные лимиты по поставкам':'Начисления по товарам',r.day,'','',badge(r.status,r.status))).join('')||empty('Отчёты ещё не запрошены'));
}
function storageChart(points,index=0){
 const w=600,h=240,left=90,right=582,top=22,bottom=194;
 const max=Math.max(1,...points.flatMap(p=>[Number(p.low),Number(p.high??p.low)]));
 const xy=(p,i,value)=>`${left+i/(points.length-1||1)*(right-left)},${bottom-Number(p[value])/max*(bottom-top)}`;
 const stepped=key=>points.flatMap((p,i)=>i?[xy({...p,[key]:points[i-1][key]},i,key),xy(p,i,key)]:[xy(p,i,key)]).join(' ');
 const line=key=>points.every(p=>p[key]!=null)?`<polyline class="storage-${key}" points="${stepped(key)}"/>`:'';
 const x=left+index/(points.length-1||1)*(right-left),p=points[index];
 return `<svg id="storage-cost-chart" class="chart storage-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Прогноз начислений за день на 30 дней вперёд"><title>Без продаж и новых поставок; тарифы остаются прежними</title>${[0,0.5,1].map(k=>`<line class="storage-grid" x1="${left}" x2="${right}" y1="${bottom-k*(bottom-top)}" y2="${bottom-k*(bottom-top)}"/><text class="chart-label" x="3" y="${bottom-k*(bottom-top)+4}">${Number(max*k).toLocaleString('ru-RU',{maximumFractionDigits:0})}</text>`).join('')}${line('high')}${line('low')}<line class="storage-marker" x1="${x}" x2="${x}" y1="${top}" y2="${bottom}"/><circle cx="${x}" cy="${bottom-Number(p.low)/max*(bottom-top)}" r="5" class="storage-dot"/>${[0,7,14,21,30].map(i=>`<text class="chart-label" text-anchor="${i===30?'end':i===0?'start':'middle'}" x="${left+i/30*(right-left)}" y="224">${esc(storageDate(points[i].day).slice(0,5))}</text>`).join('')}</svg>`;
}
function storageGroups(rows,renderRows){
 const groups=new Map();for(const r of rows){if(!groups.has(r.sku))groups.set(r.sku,[]);groups.get(r.sku).push(r);}
 return [...groups.values()].map(items=>`<details class="storage-group"><summary><strong>${esc(items[0].product_offer_id||items[0].offer_id||items[0].sku)}</strong><span>${esc(items[0].name||'Название пока не получено')} · ${items.length} строк</span></summary><div class="rows">${items.map(renderRows).join('')}</div></details>`).join('')||empty('Нет строк для выбранных фильтров');
}
function renderStorageForecast(d){
 if(!d.actual||!d.timeline.length)return panel('Прогноз стоимости хранения',empty('Нет отчёта по стоимости хранения на выбранную дату. Неизвестная стоимость не считается нулевой.'));
 const actual=d.actual,q=d.quality,points=d.timeline;
 const rateNames={observed:'По начислению этого товара на этом складе',sku_warehouse_history:'По прежним начислениям этого товара на этом складе',warehouse_type:'Оценка по товару того же типа на этом складе',type_estimate:'Оценка по тому же типу товара на другом складе',unknown:'Нет данных для расчёта цены'};
 const activeRows=d.rows.filter(r=>r.quantity==null||Number(r.quantity)>0||Number(r.fee)>0);
 const activeBatches=d.batches.filter(r=>r.quantity==null||Number(r.quantity)>0);
 const warnings=[];
 if(actual.day!==d.as_of)warnings.push(`Последние начисления — за ${storageDate(actual.day)}. Показанные будущие суммы используют остаток из этого отчёта.`);
 if(Number(q.other_warehouse_rate_units)>0)warnings.push(`Для ${num(q.other_warehouse_rate_units)} шт. тариф оценён по другим складам. Он может отличаться.`);
 if(Number(q.missing_rate_units)>0)warnings.push(`Для ${num(q.missing_rate_units)} шт. нет цены: их будущую стоимость пока нельзя полностью посчитать.`);
 if(Number(q.unknown_deadline_units)>0)warnings.push(`Для ${num(q.unknown_deadline_units)} бесплатных единиц нет подтверждённой даты перехода на платное хранение. График показывает диапазон.`);
 if(Number(q.missing_quantity_rows)>0)warnings.push('В некоторых строках нет полного количества товара. Верхняя граница стоимости неизвестна.');
 if(!d.supplies_report||d.supplies_report.day!==actual.day)warnings.push('Отчёт бесплатных лимитов отсутствует или старше отчёта начислений. Сроки требуют уточнения.');
 const forecast=panel('Хранение: начисления за день, ₽',`<div id="storage-chart-host">${storageChart(points)}</div><div class="storage-chart-legend"><span>Известные сроки</span><span>Верхняя оценка, если сроки неизвестны</span></div><label class="storage-day-picker">День прогноза<input id="storage-day-slider" type="range" min="0" max="30" step="1" value="0"></label><output id="storage-chart-value" class="storage-chart-value"></output><p>Если остатки останутся такими же, без продаж и новых поставок, а тарифы не изменятся. Каждый пункт — плата за один день.</p>`);
 const cards=`<div class="kpis storage-kpis">${kpi('Начислено '+storageDate(actual.day),actual.fee==null?'Неизвестно':storageMoney(actual.fee),num(actual.paid_units)+' платных шт. из '+num(actual.units),true)}${kpi('Завтра · '+storageDate(points[1].day),storageRange(points[1]),'За один день')}${kpi('Через неделю · '+storageDate(points[7].day),storageRange(points[7]),'За один день')}${kpi('Через месяц · '+storageDate(points[30].day),storageRange(points[30]),'За один день')}</div>`;
 const notices=warnings.map(w=>`<p class="notice warning">${esc(w)}</p>`).join('');
 const billing=panel('Стоимость по товарам и складам',`<p>${activeRows.length} сочетаний товар × склад. Цена единицы учитывает её объём. Для прогноза собираем бесплатный лимит товара по всем складам. Распределение новых платных единиц по складам — оценка, пропорциональная текущим бесплатным остаткам.</p>`+storageGroups(activeRows,r=>row(r.warehouse_name,r.cluster_name||'Кластер не указан',storageMoney(r.fee)+' / день',detail('Начислено / день',storageMoney(r.fee))+detail('На складе',num(r.quantity)+' шт.')+detail('Из них платно',num(r.paid_quantity)+' шт.')+detail('Объём единицы',(r.unit_liters==null?'—':Number(r.unit_liters).toLocaleString('ru-RU',{maximumFractionDigits:3}))+' л')+detail('Платная единица в день',storageMoney(r.unit_price))+detail('Источник цены',rateNames[r.rate_source])+detail('Дата цены',storageDate(r.rate_day))+detail('Следующее повышение',r.first_cost_increase?storageDate(r.first_cost_increase):Number(r.unknown_deadline_units)>0?'Дата неизвестна':'Не ожидается по известным срокам')+detail('Завтра / день',storageRange(r.tomorrow))+detail('Через 7 дней / день',storageRange(r.week))+detail('Через 30 дней / день',storageRange(r.month)),r.rate_source==='type_estimate'||r.rate_source==='warehouse_type'?badge('Оценка цены','warning'):r.rate_source==='unknown'?badge('Цена неизвестна','warning'):'')));
 const deadlines=panel('Когда заканчивается бесплатное хранение',`<p>Это лимиты по поставкам, а не физические остатки партий. Их сумма может быть больше остатка товара. Склад ниже — место поставки; текущий склад товара показан в стоимости выше. Бесплатный период считаем включительно до указанной даты, платный — со следующего дня. Счётчик Ozon показан отдельно.</p>`+storageGroups(activeBatches,r=>row('Поставка '+r.supply_id,'Склад поставки: '+(r.warehouse_name||'Не указан'),'',detail('Бесплатный лимит',num(r.quantity)+' шт.')+detail('Бесплатно по',storageDate(r.free_until))+detail('Платный период с',storageDate(r.paid_start))+detail('Счётчик в отчёте Ozon',r.free_days_left==null?'Неизвестно':num(r.free_days_left)+' дн.')+detail('Лимит на дату',storageDate(r.quantity_day)),badge(r.days_remaining==null?'Срок неизвестен':r.days_remaining<=0?'Период закончился':r.days_remaining+' дн. включительно',r.days_remaining==null?'warning':r.days_remaining<=0?'critical':r.days_remaining<=7?'warning':'ok'))));
 const explanation=panel('Как рассчитана стоимость',`<p>Начисления взяты из отчёта Ozon за ${esc(storageDate(actual.day))}, полученного ${esc(stamp(d.rows[0]?.completed_at))}. За следующие 7 дней: ${esc(storageRange(d.totals.week))}; за следующие 30 дней: ${esc(storageRange(d.totals.month))}.</p><p>Цена берётся из начисления по SKU и складу. Для бесплатного товара используем его объём и наблюдаемую цену литра для того же типа товара на этом складе; при отсутствии — на другом складе с явной пометкой. Это прогноз по фактическим начислениям, а не подтверждённый будущий тариф Ozon. Продажи снизят расходы, новые поставки и изменения тарифа могут их изменить.</p>`);
 return forecast+cards+notices+billing+deadlines+explanation;
}
async function loadStorageForecast(){
 const root=$('storage-forecast-root');if(!root)return;
 const request=++storageSequence,params=new URLSearchParams({as_of:$('date-to').value});
 for(const key of ['sku','cluster','warehouse'])if($(key).value)params.set(key,$(key).value);
 try{
  const response=await fetch('/api/storage/forecast?'+params);
  if(response.status===401){location.href='/login';return;}
  if(!response.ok)throw Error('Не удалось загрузить прогноз хранения. Повторите позже.');
  const data=await response.json();if(request!==storageSequence||root!==$('storage-forecast-root'))return;
  root.innerHTML=renderStorageForecast(data);
  const slider=$('storage-day-slider');if(!slider)return;
  const select=()=>{const i=Number(slider.value),p=data.timeline[i];$('storage-chart-host').innerHTML=storageChart(data.timeline,i);$('storage-chart-value').textContent=storageDate(p.day)+' · '+storageRange(p)+' / день · '+num(p.paid_units)+' платных шт.';};
  slider.addEventListener('input',select);select();
 }catch(e){if(request===storageSequence&&root===$('storage-forecast-root'))root.innerHTML=panel('Прогноз стоимости хранения',`<p class="notice danger">${esc(e.message)}</p>`);}
}
