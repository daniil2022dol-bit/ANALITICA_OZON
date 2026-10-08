/* All figures and source downloads read saved snapshots, never Ozon directly. */
const stockStatuses = [
 ['available_stock_count','Доступно к продаже'],
 ['valid_stock_count','Готовятся к продаже'],
 ['transit_stock_count','В пути между складами'],
 ['requested_stock_count','В заявках на поставку'],
 ['inbound_replenishment','Принимаются на складе'],
 ['other_stock_count','Прочие статусы'],
 ['outbound_pending_delivery','Доставляются покупателям'],
 ['return_to_seller_stock_count','К вывозу по заявке'],
 ['outbound_returns_picking','К вывозу: на сборке'],
 ['outbound_returns_ready_to_ship','К вывозу: готовы к отгрузке'],
 ['outbound_returns_return_to_seller','К вывозу: едут продавцу'],
 ['return_from_customer_stock_count','Возвраты от покупателей'],
 ['stock_not_being_sold','Не продаются'],
 ['stock_defect_stock_count','Брак на складах'],
 ['transit_defect_stock_count','Брак в пути'],
 ['waiting_docs_stock_count','Ожидают документы'],
 ['waiting_docs_to_export_stock_count','Ожидают документы для вывоза'],
 ['excess_stock_count','Излишки'],
 ['expiring_stock_count','Истекает срок годности'],
];
function stockTotal(rows,key){return rows.length&&rows.every(r=>r[key]!=null)?rows.reduce((total,r)=>total+Number(r[key]),0):null;}
function stockPanels(d){
 const statuses=d.stock_summary?.locations||[],inv=d.stock_summary?.inventory;
 const pickup=statuses.filter(r=>r.location_kind==='pickup');
 const available=stockTotal(d.stocks,'available_stock_count');
 const allAvailable=stockTotal(statuses,'available_stock_count');
 const withdrawn=stockTotal(d.stocks,'return_to_seller_stock_count');
 const inventoryNote=inv?.day?`Независимый ответ Ozon от ${stamp(inv.observed_at)}`:'Нет отдельного снимка товаров';
 const stale=d.snapshot_date&&d.snapshot_date!==$('date-to').value?`<p class="notice warning">За выбранный день снимка нет. Показан последний сохранённый снимок за ${esc(d.snapshot_date)}.</p>`:'';
 const cards=`<div class="kpis">${kpi('Доступно к продаже',num(available),$('include-pickup').checked?'Склады и ПВЗ':'Склады, без ПВЗ',true)}${kpi('Сейчас на складе FBO',num(inv?.present),inv?inventoryNote:'Нет разбивки этого показателя по кластерам / складам')}${kpi('К вывозу по заявке',num(withdrawn),'Не прибавляется к доступному')}${kpi('ПВЗ отдельно',num(stockTotal(pickup,'available_stock_count')),pickup.length?num(pickup.reduce((n,r)=>n+Number(r.locations),0))+' пунктов; доступны в фильтре':'Нет строк ПВЗ в этом срезе')}</div>`;
 const sourceLinks=[];
 if(d.snapshot_date)sourceLinks.push(`<a class="quiet source-link" href="/api/stocks/source?${new URLSearchParams({day:d.snapshot_date,kind:'analytics'})}">Ответ API: остатки</a>`);
 if(inv?.day)sourceLinks.push(`<a class="quiet source-link" href="/api/stocks/source?${new URLSearchParams({day:inv.day,kind:'catalog'})}">Ответ API: товары</a>`);
 const explanations=panel('Сверка с Ozon',`<p>В полном ответе об остатках: <strong>${esc(num(allAvailable))} шт. доступны к продаже</strong>, включая ПВЗ. Вывоз и его этапы пересекаются — их нельзя складывать друг с другом. «Сейчас на складе FBO» получено другим методом и в другое время; это отдельный показатель, не сумма статусов.</p>${inv?.present!=null?`<p>Товары FBO: сейчас на складе ${esc(num(inv.present))}, зарезервировано ${esc(num(inv.reserved))}. Дата снимка товаров: ${esc(inv.day)}, время: ${esc(stamp(inv.observed_at))}.</p>`:''}<div class="source-links">${sourceLinks.join('')}</div><p>Скачиваются полные сохранённые ответы, включая скрытые ПВЗ и архив. Запросов к Ozon при скачивании нет.</p><details><summary>Все статусы выбранного среза</summary><div class="rows">${stockStatuses.map(([key,label])=>row(label,key,num(stockTotal(d.stocks,key))+' шт.')).join('')}</div><p>«—» означает, что API не передал показатель хотя бы в одной строке. Общая сумма всех статусов не рассчитывается.</p></details>`);
 const history=panel('История доступного остатка','<svg id="stock-chart" class="chart" viewBox="0 0 560 210" role="img" aria-label="История доступного остатка"></svg><p>Доступно к продаже в выбранном срезе. История накапливается с первого запуска. Пропущенные дни не заполняются значениями соседних дней.</p>');
 const forecasts=panel('Доступно к продаже и прогноз',`<div class="rows">${d.stocks.map(r=>{const f=r.forecast;return row(r.offer_id,`${r.name||''} · ${r.cluster_name||'Кластер не указан'}${r.warehouse_name?' / '+r.warehouse_name:''}`,num(r.available_stock_count),detail('Доступно к продаже',num(r.available_stock_count)+' шт.')+detail('К вывозу',num(r.return_to_seller_stock_count))+detail('В пути',num(r.transit_stock_count))+detail('Хватит на',f.days_left==null?'Недостаточно спроса / истории':num(f.days_left)+' дн.')+detail('Закончится',f.stockout_date||'—')+detail('Поставка до',f.reorder_date||'—')+detail('Рекомендуем пополнить',num(f.recommended_units)+' шт.')+detail('Расчёт',f.source),badge(num(r.available_stock_count)+' шт.',f.level));}).join('')||empty('Для этого среза нет строк остатков')}</div>`);
 return stale+cards+explanations+history+forecasts;
}
