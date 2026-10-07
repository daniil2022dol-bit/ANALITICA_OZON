import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from psycopg import sql
from psycopg.types.json import Jsonb

from .api import APIError, SellerAPI
from .config import settings
from .database import connect
from .storage import normalize, parse_report

log = logging.getLogger(__name__)
MSK = ZoneInfo('Europe/Moscow')
STOCK_FIELDS = (
    'available_stock_count valid_stock_count transit_stock_count requested_stock_count '
    'inbound_replenishment excess_stock_count expiring_stock_count other_stock_count '
    'outbound_pending_delivery outbound_returns_picking outbound_returns_ready_to_ship '
    'outbound_returns_return_to_seller return_from_customer_stock_count return_to_seller_stock_count '
    'stock_defect_stock_count transit_defect_stock_count stock_not_being_sold '
    'waiting_docs_stock_count waiting_docs_to_export_stock_count '
    'ads idc days_without_sales turnover_grade ads_cluster idc_cluster '
    'days_without_sales_cluster turnover_grade_cluster item_tags placement_zone'
).split()


def today():
    return datetime.now(MSK).date()


def upsert_product(conn, item):
    conn.execute('''INSERT INTO ozon.product(client_id,sku,product_id,offer_id,name,archived)
        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(client_id,sku) DO UPDATE
        SET product_id=coalesce(EXCLUDED.product_id,ozon.product.product_id),
        offer_id=EXCLUDED.offer_id,name=coalesce(EXCLUDED.name,ozon.product.name),
        archived=coalesce(EXCLUDED.archived,ozon.product.archived),last_seen_at=now()''',
        (settings.client_id, int(item['sku']), item.get('product_id'),
         item.get('offer_id') or str(item['sku']), item.get('name'), item.get('archived')))


def collect_products(api):
    result = {}
    for visibility in ('ALL','ARCHIVED'):
        cursor = ''
        while True:
            data = api.post('/v3/product/list', {'filter':{'visibility':visibility},'last_id':cursor,'limit':1000})['result']
            for item in data['items']:
                if item.get('sku'):
                    result[int(item['sku'])] = item
            if len(data['items']) < 1000:
                break
            next_cursor = data.get('last_id')
            if not next_cursor or next_cursor == cursor:
                raise APIError('Product pagination did not advance')
            cursor = next_cursor
    with connect() as conn:
        for item in result.values():
            upsert_product(conn,item)
    if not result:
        raise APIError('No SKU found; empty snapshot will not be published')
    return sorted(result)


def collect_stocks(api):
    day = today()
    with connect() as conn:
        if conn.execute('SELECT 1 FROM ozon.published_snapshot WHERE client_id=%s AND snapshot_date=%s', (settings.client_id,day)).fetchone():
            return
    # Macrolocal names come from a distinct identifier namespace.
    macro = api.post('/v2/cluster/list', {})
    with connect() as conn:
        for item in macro.get('result',[]):
            name = item.get('data',{}).get('macrolocal_cluster',{}).get('name')
            conn.execute('INSERT INTO ozon.macrolocal_cluster(macrolocal_cluster_id,name) VALUES(%s,%s) ON CONFLICT(macrolocal_cluster_id) DO UPDATE SET name=EXCLUDED.name,last_seen_at=now()',
                         (item['macrolocal_cluster_id'], name or str(item['macrolocal_cluster_id'])))
    skus = collect_products(api)
    batches = [skus[i:i+100] for i in range(0,len(skus),100)]
    with connect() as conn:
        run = conn.execute('INSERT INTO ozon.ingest_run(client_id,snapshot_date,requested_skus,expected_batches) VALUES(%s,%s,%s,%s) RETURNING run_id',
                           (settings.client_id,day,skus,len(batches))).fetchone()['run_id']
    try:
        seen = set()
        for n,batch in enumerate(batches,1):
            request = {'skus':[str(s) for s in batch]}
            data = api.post('/v1/analytics/stocks', request)
            observed = datetime.now(timezone.utc)
            rows = data.get('items')
            if not isinstance(rows,list):
                raise APIError('Missing stock items array')
            with connect() as conn:
                conn.execute('INSERT INTO ozon.api_response(run_id,request_number,endpoint,request_body,http_status,response_body) VALUES(%s,%s,%s,%s,200,%s)',
                             (run,n,'/v1/analytics/stocks',Jsonb(request),Jsonb(data)))
                for row in rows:
                    key = (int(row['sku']),int(row['warehouse_id']))
                    if key in seen or key[0] not in batch:
                        raise APIError('Duplicate or unrequested stock key; snapshot rejected')
                    seen.add(key)
                    upsert_product(conn,row)
                    warehouse = key[1]
                    conn.execute('INSERT INTO ozon.warehouse(warehouse_id,name) VALUES(%s,%s) ON CONFLICT(warehouse_id) DO UPDATE SET name=EXCLUDED.name,last_seen_at=now()',
                                 (warehouse,row['warehouse_name']))
                    cluster = row.get('cluster_id') or None
                    if cluster is not None:
                        conn.execute('INSERT INTO ozon.cluster(cluster_id,name) VALUES(%s,%s) ON CONFLICT(cluster_id) DO UPDATE SET name=EXCLUDED.name,last_seen_at=now()',
                                     (cluster,row.get('cluster_name') or str(cluster)))
                    macro_id = row.get('macrolocal_cluster_id') or None
                    if macro_id:
                        conn.execute('INSERT INTO ozon.macrolocal_cluster(macrolocal_cluster_id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING',(macro_id,str(macro_id)))
                    columns = ['run_id','client_id','sku','warehouse_id','cluster_id','macrolocal_cluster_id','observed_at']+STOCK_FIELDS
                    values = [run,settings.client_id,key[0],warehouse,cluster,macro_id,observed]+[row.get(f) for f in STOCK_FIELDS]
                    conn.execute(sql.SQL('INSERT INTO ozon.stock_daily ({}) VALUES ({})').format(
                        sql.SQL(',').join(map(sql.Identifier,columns)),sql.SQL(',').join(sql.Placeholder() for _ in columns)),values)
                conn.execute('UPDATE ozon.ingest_run SET successful_batches=%s WHERE run_id=%s',(n,run))
        with connect() as conn:
            conn.execute("UPDATE ozon.ingest_run SET status='succeeded',validation_passed=true,finished_at=now() WHERE run_id=%s",(run,))
            conn.execute('SELECT ozon.publish_stock_snapshot(%s)',(run,))
    except Exception:
        with connect() as conn:
            conn.execute("UPDATE ozon.ingest_run SET status='failed',finished_at=now(),error_message='See collector job log' WHERE run_id=%s",(run,))
        raise


def warehouse_clusters(conn):
    rows = conn.execute('''SELECT DISTINCT ON(warehouse_id) warehouse_id,cluster_id
        FROM ozon.v_stock_daily WHERE client_id=%s ORDER BY warehouse_id,snapshot_date DESC''',(settings.client_id,))
    return {r['warehouse_id']:r['cluster_id'] for r in rows}


def save_postings(conn, postings, mapping, day):
    for item in postings:
        analytics, financial = item.get('analytics_data') or {},item.get('financial_data') or {}
        warehouse = analytics.get('warehouse_id') or None
        if warehouse:
            conn.execute('INSERT INTO ozon.warehouse(warehouse_id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                         (warehouse,analytics.get('warehouse_name') or str(warehouse)))
        # Never assign a current cluster mapping to a historical order without
        # a stock snapshot covering the order date. Unknown stays explicit.
        created = datetime.fromisoformat(item['created_at'].replace('Z','+00:00'))
        order_day = created.astimezone(MSK).date()
        old = conn.execute('SELECT cluster_id FROM ozon.posting WHERE client_id=%s AND posting_number=%s',
                           (settings.client_id,item['posting_number'])).fetchone()
        cluster = old['cluster_id'] if old else mapping.get(warehouse) if order_day==day else None
        conn.execute('''INSERT INTO ozon.posting(client_id,posting_number,order_number,created_at,status,warehouse_id,cluster_id,cluster_from,cluster_to)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(client_id,posting_number) DO UPDATE
            SET status=EXCLUDED.status,warehouse_id=EXCLUDED.warehouse_id,
            cluster_from=EXCLUDED.cluster_from,cluster_to=EXCLUDED.cluster_to,fetched_at=now()''',
            (settings.client_id,item['posting_number'],item.get('order_number'),created,item['status'],warehouse,cluster,financial.get('cluster_from'),financial.get('cluster_to')))
        conn.execute('DELETE FROM ozon.posting_item WHERE client_id=%s AND posting_number=%s',(settings.client_id,item['posting_number']))
        grouped = {}
        for product in item['products']:
            upsert_product(conn,product)
            sku = int(product['sku'])
            price = product['price']
            value = (Decimal(price['amount']),price['currency'])
            if sku in grouped and grouped[sku]['price'] != value:
                raise APIError('Duplicate posting SKU with conflicting prices')
            grouped.setdefault(sku,{'price':value,'quantity':0})['quantity'] += int(product['quantity'])
        for sku,g in grouped.items():
            conn.execute('INSERT INTO ozon.posting_item VALUES(%s,%s,%s,%s,%s,%s)',
                         (settings.client_id,item['posting_number'],sku,g['quantity'],*g['price']))


def collect_sales(api):
    day = today()
    start = day-timedelta(days=settings.history_days)
    postings,cursor,seen = [],'',set()
    while True:
        body = {'filter':{'since':datetime.combine(start,datetime.min.time(),MSK).isoformat(),
                          'to':datetime.now(MSK).isoformat()},'limit':100,'cursor':cursor,
                'sort_dir':'ASC','with':{'analytics_data':True,'financial_data':True}}
        data = api.post('/v3/posting/fbo/list',body)
        postings.extend(data['postings'])
        if not data.get('has_next'):
            break
        cursor = data.get('cursor')
        if not cursor or cursor in seen:
            raise APIError('Posting pagination did not advance')
        seen.add(cursor)
    with connect() as conn:
        # Reconcile the entire fetched time range atomically, preserving
        # mappings of existing rows and removing postings absent on refresh.
        mapping = warehouse_clusters(conn)
        save_postings(conn,postings,mapping,day)
        numbers = [p['posting_number'] for p in postings]
        conn.execute('DELETE FROM ozon.posting WHERE client_id=%s AND created_at>=%s AND created_at<%s AND NOT(posting_number=ANY(%s))',
                     (settings.client_id,datetime.combine(start,datetime.min.time(),MSK),datetime.now(MSK),numbers))
        for n in range((day-start).days):
            conn.execute('INSERT INTO ozon.sales_coverage(client_id,day) VALUES(%s,%s) ON CONFLICT(client_id,day) DO UPDATE SET fetched_at=now()',
                         (settings.client_id,start+timedelta(days=n)))
        # Historical nonterminal orders beyond the lookback need separate
        # refreshes, otherwise cancellations arriving late would remain stale.
        pending = list(conn.execute("SELECT posting_number FROM ozon.posting WHERE client_id=%s AND created_at<%s AND status NOT IN ('delivered','cancelled')",
                                    (settings.client_id,datetime.combine(start,datetime.min.time(),MSK))))
    for i in range(0,len(pending),100):
        data = api.post('/v3/posting/fbo/list',{'filter':{'posting_numbers':[r['posting_number'] for r in pending[i:i+100]]},'limit':100,
                                             'with':{'analytics_data':True,'financial_data':True}})
        if data.get('has_next'):
            raise APIError('Pending posting refresh exceeded one page')
        with connect() as conn:
            save_postings(conn,data['postings'],mapping,day)
    # Official daily amount + units, sku/day; refresh recent days for corrections.
    begin = (day-timedelta(days=3)).isoformat()
    metrics,offset = [],0
    while True:
        data = api.post('/v1/analytics/data',{'date_from':begin,'date_to':day.isoformat(),
                     'dimension':['sku','day'],'metrics':['ordered_units','revenue'],'limit':1000,'offset':offset})
        rows = data['result']['data'];metrics.extend(rows)
        if len(rows)<1000:
            break
        offset+=1000
    with connect() as conn:
        conn.execute('DELETE FROM ozon.sales_daily WHERE client_id=%s AND day BETWEEN %s AND %s',(settings.client_id,begin,day))
        for row in metrics:
            dimensions = row['dimensions'];sku = int(dimensions[0]['id']);metric_day = dimensions[1]['id']
            if not conn.execute('SELECT 1 FROM ozon.product WHERE client_id=%s AND sku=%s',(settings.client_id,sku)).fetchone():
                upsert_product(conn,{'sku':sku,'offer_id':str(sku),'name':dimensions[0].get('name')})
            conn.execute('INSERT INTO ozon.sales_daily(client_id,day,sku,ordered_units,ordered_amount) VALUES(%s,%s,%s,%s,%s)',
                         (settings.client_id,metric_day,sku,int(row['metrics'][0]),Decimal(str(row['metrics'][1]))))
        for n in range(4):
            conn.execute('INSERT INTO ozon.analytics_coverage(client_id,day) VALUES(%s,%s) ON CONFLICT(client_id,day) DO UPDATE SET fetched_at=now()',
                         (settings.client_id,day-timedelta(days=n)))


def collect_storage(api):
    day = today()
    for kind in ('products','supplies'):
        with connect() as conn:
            report = conn.execute('SELECT * FROM ozon.storage_report WHERE client_id=%s AND day=%s AND kind=%s',
                                  (settings.client_id,day,kind)).fetchone()
            if report and report['status'] == 'success':
                continue
            if not report:
                report = conn.execute('INSERT INTO ozon.storage_report(client_id,day,kind) VALUES(%s,%s,%s) RETURNING *',
                                      (settings.client_id,day,kind)).fetchone()
        if not report['code']:
            bootstrap = settings.data_dir/f'bootstrap_placement_{kind}.json'
            # Reuse the manually requested first reports; never create duplicates.
            if bootstrap.exists() and day.isoformat()=='2026-10-07':
                code = json.loads(bootstrap.read_text())['code']
            elif report['status'] == 'uncertain':
                raise APIError('Ambiguous report creation is awaiting manual review')
            else:
                with connect() as conn:
                    conn.execute("UPDATE ozon.storage_report SET status='uncertain' WHERE id=%s",(report['id'],))
                code = api.post(f'/v1/report/placement/by-{kind}/create',
                                {'date_from':(day-timedelta(days=1)).isoformat(),'date_to':day.isoformat()})['code']
            with connect() as conn:
                conn.execute("UPDATE ozon.storage_report SET code=%s,status='requested' WHERE id=%s",(code,report['id']))
        else:
            code = report['code']
        info = api.post('/v1/report/info',{'code':code})['result']
        if info['status'] in ('waiting','processing'):
            # Durable task; resume in the next 30-min run, no polling storm.
            continue
        if info['status'] != 'success':
            with connect() as conn:
                conn.execute("UPDATE ozon.storage_report SET status='failed',error='Ozon report generation failed' WHERE id=%s",(report['id'],))
            raise APIError('Storage report generation failed')
        content = api.download(info['file'])
        path = settings.data_dir/'reports'/f'{day}-{kind}.xlsx'
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        path.write_bytes(content);path.chmod(0o600)
        columns,rows = parse_report(content,kind)
        with connect() as conn:
            warehouse_names = {}
            for w in conn.execute('SELECT * FROM ozon.warehouse'):
                key = normalize(w['name'])
                warehouse_names.setdefault(key,[]).append(w['warehouse_id'])
            mapping = warehouse_clusters(conn)
            conn.execute('DELETE FROM ozon.storage_row WHERE report_id=%s',(report['id'],))
            for row in rows:
                matches = warehouse_names.get(normalize(row.get('warehouse_name')),[])
                warehouse = matches[0] if len(matches)==1 else None
                fields = ['report_id','warehouse_id','cluster_id']+list(row)
                values = [report['id'],warehouse,mapping.get(warehouse)]+[Jsonb(row[k]) if k=='raw' else row[k] for k in row]
                conn.execute(sql.SQL('INSERT INTO ozon.storage_row ({}) VALUES ({})').format(
                    sql.SQL(',').join(map(sql.Identifier,fields)),sql.SQL(',').join(sql.Placeholder() for _ in fields)),values)
            conn.execute("UPDATE ozon.storage_report SET status='success',completed_at=now(),file_path=%s,file_sha256=%s,columns=%s WHERE id=%s",
                         (str(path),hashlib.sha256(content).hexdigest(),Jsonb(columns),report['id']))


def run_job(name, function, api):
    with connect() as conn:
        job_id = conn.execute('INSERT INTO ozon.job_run(job) VALUES(%s) RETURNING id',(name,)).fetchone()['id']
    try:
        function(api)
    except Exception as exc:
        log.error('%s failed: %s',name,type(exc).__name__)
        with connect() as conn:
            conn.execute("UPDATE ozon.job_run SET status='failed',finished_at=now(),details=%s WHERE id=%s",(Jsonb({'error':str(exc) if isinstance(exc,APIError) else type(exc).__name__}),job_id))
        return False
    with connect() as conn:
        conn.execute("UPDATE ozon.job_run SET status='success',finished_at=now() WHERE id=%s",(job_id,))
    return True


def collect():
    settings.data_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    with connect() as lock:
        if not lock.execute('SELECT pg_try_advisory_lock(95141802) AS acquired').fetchone()['acquired']:
            return
        api = SellerAPI()
        with connect() as conn:
            conn.execute('INSERT INTO ozon.seller_account(client_id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING',(settings.client_id,'Ozon Seller'))
        # Idempotent daily stock collection; sales retry only once per day after
        # success, reports are resumed every 30 minutes without re-creation.
        results = []
        for name,fn in [('stocks',collect_stocks),('sales',collect_sales),('storage',collect_storage)]:
            if name == 'sales':
                with connect() as conn:
                    done = conn.execute("SELECT 1 FROM ozon.job_run WHERE job='sales' AND status='success' AND started_at>=%s",(datetime.combine(today(),datetime.min.time(),MSK),)).fetchone()
                if done:
                    continue
            results.append(run_job(name,fn,api))
        if not all(results):
            raise SystemExit(1)
