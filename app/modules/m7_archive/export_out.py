import io,csv,json,hashlib

def _remit_rows(remit):
    """来账分配证据：工作台与结案下载共用同一份 state['remittance']。"""
    rows=[]
    for receipt in remit.get('receipts',[]):
        rows.append(['来账',receipt.get('id',''),'',json.dumps({
            'value_date':receipt.get('value_date',''),'memo':receipt.get('memo',''),
            'amount':receipt.get('amount',''),'scope':receipt.get('scope',[]),
            'unmatched_refs':receipt.get('unmatched_refs',[]),
            'allocated':receipt.get('allocated',''),'suspense':receipt.get('suspense',''),
            'balanced':receipt.get('balanced',False),'content_hash':receipt.get('content_hash','')},
            ensure_ascii=False,sort_keys=True)])
        for line in receipt.get('lines',[]):
            rows.append(['匹配',receipt.get('id','')+'/'+line.get('policy',''),'',json.dumps(line,ensure_ascii=False,sort_keys=True)])
    for policy in remit.get('net_by_policy',[]) if remit.get('receipts') else []:
        rows.append(['净额',policy.get('id',''),'',json.dumps(policy,ensure_ascii=False,sort_keys=True)])
    if remit.get('receipts') and remit.get('totals'):
        rows.append(['来账合计','',json.dumps(remit.get('decision',{}),ensure_ascii=False,sort_keys=True),
                     json.dumps(remit['totals'],ensure_ascii=False,sort_keys=True)])
    return rows

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for kind,ident,reason,body in _remit_rows(state.get('remittance') or {}):writer.writerow([kind,rid,'',ident,reason,body])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
