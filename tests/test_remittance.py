"""银行来账分配 / 暂挂 / 幂等 / 替换保留旧版。"""
import csv
import io
import json
from decimal import Decimal

import pytest

from app.errors import DomainError
from app.modules.m6_reconcile.remit import allocate, normalize
from app.modules.m7_archive.export_out import bundle
from app.workflow import load, run


def _remit(raw, remittances):
    return run(raw, {'remittances': remittances})


def test_scope_is_listed_policies_only(raw):
    # 附言只列一部分保单，未列出的保单不得吸收来账。
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '5000.00',
                          'memo': '保费 P002 P003'}])
    receipt = state['remittance']['receipts'][0]
    assert receipt['scope'] == ['P002', 'P003']
    net = {p['id']: p['net'] for p in state['remittance']['net_by_policy']}
    assert net['P002'] == '0.00' and net['P003'] == '0.00'
    assert net['P005'] == '1380.00'


def test_unexplained_amount_goes_to_suspense_not_prorated(raw):
    # 5000 到账只够两张保单各 1380，其余 2240 无法说明归属，必须暂挂。
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '5000.00',
                          'memo': 'P002 P003'}])
    receipt = state['remittance']['receipts'][0]
    assert receipt['allocated'] == '2760.00'
    assert receipt['suspense'] == '2240.00'
    assert all(line['allocated'] in ('1380.00', '0.00') for line in receipt['lines'])


def test_receipt_equals_allocated_plus_suspense(raw):
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '5000.00',
                          'memo': 'P002 P003 P004'}])
    totals = state['remittance']['totals']
    assert Decimal(totals['receipt']) == Decimal(totals['allocated']) + Decimal(totals['suspense'])
    assert totals['balanced'] is True and state['reconcile']['balanced'] is True


def test_net_is_difference_minus_allocated(raw):
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '2760.00',
                          'memo': 'P002 P003'}])
    for row in state['remittance']['net_by_policy']:
        assert Decimal(row['difference']) - Decimal(row['allocated']) == Decimal(row['net'])
    totals = state['remittance']['totals']
    assert totals['difference_after'] == totals['net']
    assert Decimal(totals['paid_after']) == Decimal(totals['paid_before']) + Decimal(totals['allocated'])


def test_sole_reference_may_partially_settle(raw):
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '1000.00', 'memo': 'P005'}])
    receipt = state['remittance']['receipts'][0]
    assert receipt['allocated'] == '1000.00' and receipt['suspense'] == '0.00'
    row = [l for l in receipt['lines'] if l['policy'] == 'P005'][0]
    assert row['status'] == 'PARTIAL' and row['net_after'] == '380.00'


def test_multiple_open_policies_stop_at_first_shortfall(raw):
    # 两张待补缴保单，余额 1500 不足结清第二张：第一张足额，余额不拆入第二张。
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '1500.00',
                          'memo': 'P002 P003'}])
    receipt = state['remittance']['receipts'][0]
    assert receipt['allocated'] == '1380.00' and receipt['suspense'] == '120.00'
    assert [l['status'] for l in receipt['lines']] == ['SETTLED', 'UNSETTLED']


def test_explicit_allocations_respected(raw):
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '2000.00',
                          'allocations': {'P002': '1380.00'}}])
    receipt = state['remittance']['receipts'][0]
    assert receipt['allocated'] == '1380.00' and receipt['suspense'] == '620.00'
    assert receipt['lines'][0]['basis'] == 'EXPLICIT'


def test_explicit_allocation_over_due_rejected(raw):
    with pytest.raises(DomainError) as e:
        _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '9000.00',
                      'allocations': {'P002': '9999.00'}}])
    assert e.value.code == 'REMITTANCE'


def test_refund_policy_does_not_absorb(raw):
    # 构造一张退费保单：paid 大于应收。
    content = 'id,policy_id,version,kind,effective,start,end,annual_premium,paid,ref,cover,note\n' \
              '1,R1,1,issue,2026-01-01,2026-01-01,2027-01-01,3650,4000,,基础险,多收\n'
    state = run(content.encode(), {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '100.00', 'memo': 'R1'}]})
    receipt = state['remittance']['receipts'][0]
    assert receipt['scope'] == ['R1']
    assert receipt['allocated'] == '0.00' and receipt['suspense'] == '100.00'
    assert receipt['lines'][0]['status'] == 'REFUND_DUE'


def test_unmatched_reference_kept_as_evidence(raw):
    # 附言只出现本批不存在的保单号：归属无法说明，全额暂挂并保留原始引用。
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '100.00',
                          'memo': 'P999 P998'}])
    receipt = state['remittance']['receipts'][0]
    assert receipt['unmatched_refs'] == ['P999', 'P998']
    assert receipt['scope'] == []
    assert receipt['allocated'] == '0.00' and receipt['suspense'] == '100.00'
    assert state['remittance']['totals']['balanced'] is True


def test_identical_reimport_is_idempotent(raw):
    item = {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'}
    once = _remit(raw, [item])
    thrice = _remit(raw, [item, dict(item), dict(item)])
    assert thrice['remittance']['duplicates'] == 2
    assert thrice['remittance']['totals']['allocated'] == once['remittance']['totals']['allocated']
    assert thrice['remittance']['receipts'] == once['remittance']['receipts']


def test_conflicting_reimport_rejected(raw):
    with pytest.raises(DomainError) as e:
        normalize([{'id': 'B1', 'amount': '100'}, {'id': 'B1', 'amount': '200'}])
    assert e.value.code == 'CONFLICT'


def test_subcent_amount_rejected(raw):
    with pytest.raises(DomainError) as e:
        _remit(raw, [{'id': 'B1', 'amount': '1.005'}])
    assert e.value.code == 'REMITTANCE'


def test_allocation_is_pure(raw):
    state = run(raw, {})
    params = {'remittances': [{'id': 'B1', 'date': '2026-11-19', 'amount': '5000.00',
                               'memo': 'P002 P003'}]}
    state['parameters'] = params
    first = allocate(state)
    second = allocate(state)
    assert first == second


def test_workbench_and_export_show_same_scope():
    # 工作台 state['remittance'] 与结案下载共用同一份结果。
    state = run(load('clean.csv'), {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '5000.00', 'memo': 'P002 P003 P004'}]})
    exported = list(csv.reader(io.StringIO(bundle(state).lstrip('﻿'))))
    receipt_rows = [r for r in exported if r[0] == '来账']
    assert len(receipt_rows) == 1
    body = json.loads(receipt_rows[0][5])
    assert body['scope'] == state['remittance']['receipts'][0]['scope']
    assert body['suspense'] == state['remittance']['totals']['suspense']
    assert body['allocated'] == state['remittance']['totals']['allocated']
    net_rows = [json.loads(r[5]) for r in exported if r[0] == '净额']
    assert {r['id']: r['net'] for r in net_rows} == {
        p['id']: p['net'] for p in state['remittance']['net_by_policy']}
    totals = [json.loads(r[5]) for r in exported if r[0] == '来账合计'][0]
    assert totals['suspense'] == state['remittance']['totals']['suspense']


def test_reimport_and_reorder_share_one_batch(raw):
    # 同一来账重复导入或匹配重试顺序不同，也只算一个批次，已分配不增加。
    first = {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'},
        {'id': 'B2', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P003'}]}
    retry = {'remittances': [
        {'id': 'B2', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P003'},
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'},
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'}]}
    a = run(raw, first)
    b = run(raw, retry)
    assert a['run_id'] == b['run_id']
    assert a['remittance']['totals']['allocated'] == b['remittance']['totals']['allocated'] == '2760.00'
    assert b['remittance']['totals']['duplicates'] == 1


def test_replace_keeps_old_version_on_stale(client):
    created = client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'}]}}).get_json()
    run_id = created['run_id']
    assert created['remittance']['totals']['allocated'] == '1380.00'
    # 先复核一次，版本前移；再拿旧版本号替换必须 409，且旧正文不变。
    revised = client.post('/api/runs/%s/review' % run_id, json={'version': 1, 'decision': '通过'}).get_json()
    assert revised['review_version'] == 2
    stale = client.post('/api/runs/%s/remittance' % run_id, json={'version': 1, 'remittances': [
        {'id': 'B2', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P003'}]})
    assert stale.status_code == 409
    after = client.get('/api/runs/' + run_id).get_json()
    assert after['review_version'] == 2
    assert after['remittance']['totals']['allocated'] == '1380.00'
    assert [r['id'] for r in after['remittance']['receipts']] == ['B1']


def test_replace_rejects_invalid_and_keeps_old(client):
    created = client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'}]}}).get_json()
    run_id = created['run_id']
    bad = client.post('/api/runs/%s/remittance' % run_id, json={'version': 1, 'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '9999.00',
         'allocations': {'P002': '9999.00'}}]})
    assert bad.status_code == 422
    after = client.get('/api/runs/' + run_id).get_json()
    assert after['review_version'] == 1
    assert after['remittance']['receipts'][0]['amount'] == '1380.00'
    assert after['remittance']['totals']['allocated'] == '1380.00'


def test_replace_success_keeps_old_and_recomputes_without_accumulation(client):
    created = client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'}]}}).get_json()
    run_id = created['run_id']
    replaced = client.post('/api/runs/%s/remittance' % run_id, json={'version': 1, 'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'},
        {'id': 'B2', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P003'}]})
    assert replaced.status_code == 200
    body = replaced.get_json()
    # 替换生成接替批次，旧批次保留并标记已替换。
    assert body['run_id'] != run_id and body['parent'] == run_id
    assert body['review_version'] == 1
    old = client.get('/api/runs/' + run_id).get_json()
    assert old['status'] == '已替换' and old['replaced_by'] == body['run_id']
    assert old['remittance']['totals']['allocated'] == '1380.00'
    # 替换是整笔重算：B1 不会因为原来已分配一次而再分配一次。
    assert body['remittance']['totals']['allocated'] == '2760.00'
    assert [r['id'] for r in body['remittance']['receipts']] == ['B1', 'B2']


def test_reimport_after_replace_does_not_allocate_again(client):
    # 替换后再次导入同一来账：落到接替批次，已分配不增加3。
    created = client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'}]}}).get_json()
    params = {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'},
        {'id': 'B2', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P003'}]}
    replaced = client.post('/api/runs/%s/remittance' % created['run_id'],
                           json={'version': 1, 'remittances': params['remittances']}).get_json()
    before = len(client.get('/api/runs').get_json())
    again = client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': params}).get_json()
    assert again['run_id'] == replaced['run_id']
    assert again['remittance']['totals']['allocated'] == '2760.00'
    # 再次导入不会再建批次，已分配金额也不会翻倍。
    assert len(client.get('/api/runs').get_json()) == before


def test_replace_then_export_matches_workbench(client):
    created = client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '5000.00', 'memo': 'P002 P003'}]}}).get_json()
    run_id = created['run_id']
    export = client.get('/api/archive/%s/export.csv' % run_id)
    assert export.status_code == 200
    rows = list(csv.reader(io.StringIO(export.data.decode('utf-8-sig'))))
    receipt = [r for r in rows if r[0] == '来账'][0]
    body = json.loads(receipt[5])
    assert body['allocated'] == created['remittance']['totals']['allocated'] == '2760.00'
    assert body['suspense'] == created['remittance']['totals']['suspense'] == '2240.00'


def test_partial_explicit_then_sole_open_policy_allows_remainder(raw):
    # 逐保单只指定 P002，剩余金额只剩 P003 一张待补缴：归属无歧义，可部分核销。
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '2000.00',
                          'memo': 'P002 P003', 'allocations': {'P002': '1380.00'}}])
    receipt = state['remittance']['receipts'][0]
    assert receipt['allocated'] == '2000.00' and receipt['suspense'] == '0.00'
    status = {l['policy']: l['status'] for l in receipt['lines']}
    assert status['P002'] == 'SETTLED' and status['P003'] == 'PARTIAL'


def test_refund_policy_not_offset_by_allocation(raw):
    # 退费保单 P0 与补缴保单 P1：来账只能核销 P1，不能冲抵扣 P0 的退费。
    content = ('id,policy_id,version,kind,effective,start,end,annual_premium,paid,ref,cover,note\n'
               '1,R1,1,issue,2026-01-01,2026-01-01,2027-01-01,3650,4000,,基础险,多收\n'
               '2,P1,1,issue,2026-01-01,2026-01-01,2027-01-01,3650,1000,,基础险,欠缴\n').encode()
    state = run(content, {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '9000.00', 'memo': 'R1 P1'}]})
    receipt = state['remittance']['receipts'][0]
    assert receipt['allocated'] == '2650.00'
    assert receipt['suspense'] == '6350.00'
    assert state['remittance']['totals']['identities']['no_over_allocation'] is True
    assert state['remittance']['totals']['balanced'] is True


def test_old_version_export_still_explains_original_receipt(client):
    # 被替换的旧批次仍可下载，原始来账与当次分配范围完整可解释。
    created = client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '5000.00', 'memo': 'P002 P003'}]}}).get_json()
    old_id = created['run_id']
    new = client.post('/api/runs/%s/remittance' % old_id, json={'version': 1, 'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '5000.00', 'memo': 'P002 P003'}]}).get_json()
    assert new['run_id'] == old_id  # 完全相同即幂等，不另建批次
    changed = client.post('/api/runs/%s/remittance' % old_id, json={'version': 1, 'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '3000.00', 'memo': 'P002 P003'}]}).get_json()
    old_export = client.get('/api/archive/%s/export.csv' % old_id)
    assert old_export.status_code == 200
    rows = list(csv.reader(io.StringIO(old_export.data.decode('utf-8-sig'))))
    receipt = json.loads([r for r in rows if r[0] == '来账'][0][5])
    assert receipt['amount'] == '5000.00' and receipt['allocated'] == '2760.00'
    assert receipt['suspense'] == '2240.00'
    assert changed['remittance']['receipts'][0]['amount'] == '3000.00'
    assert [r[3] for r in rows if r[0] == '来账'] == ['B1']


def test_replace_twice_rejected_old_preserved(client):
    created = client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': {'remittances': [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'}]}}).get_json()
    old_id = created['run_id']
    client.post('/api/runs/%s/remittance' % old_id, json={'version': 1, 'remittances': [
        {'id': 'B2', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P003'}]})
    again = client.post('/api/runs/%s/remittance' % old_id, json={'version': 1, 'remittances': [
        {'id': 'B3', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P005'}]})
    assert again.status_code == 409
    old = client.get('/api/runs/' + old_id).get_json()
    assert old['remittance']['receipts'][0]['id'] == 'B1'


def test_two_receipts_settle_one_policy_cumulatively(raw):
    # 同一保单分两笔来账足额结清：净额按累计核销推进，不会重复分配。
    state = _remit(raw, [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '500.00', 'memo': 'P002'},
        {'id': 'B2', 'date': '2026-11-20', 'amount': '880.00', 'memo': 'P002'}])
    receipts = state['remittance']['receipts']
    assert [r['allocated'] for r in receipts] == ['500.00', '880.00']
    assert [r['suspense'] for r in receipts] == ['0.00', '0.00']
    row = [p for p in state['remittance']['net_by_policy'] if p['id'] == 'P002'][0]
    assert row['allocated'] == '1380.00' and row['net'] == '0.00'


def test_receipt_without_reference_is_fully_suspended(raw):
    state = _remit(raw, [{'id': 'B1', 'date': '2026-11-19', 'amount': '800.00', 'memo': ''}])
    receipt = state['remittance']['receipts'][0]
    assert receipt['scope'] == []
    assert receipt['allocated'] == '0.00' and receipt['suspense'] == '800.00'
    assert state['remittance']['totals']['balanced'] is True


def test_second_receipt_for_settled_policy_is_suspended(raw):
    # 第二笔付给已结清保单的钱归属无法说明，转入暂挂而不是重复核销。
    state = _remit(raw, [
        {'id': 'B1', 'date': '2026-11-19', 'amount': '1380.00', 'memo': 'P002'},
        {'id': 'B2', 'date': '2026-11-20', 'amount': '1380.00', 'memo': 'P002'}])
    assert [r['allocated'] for r in state['remittance']['receipts']] == ['1380.00', '0.00']
    assert [r['suspense'] for r in state['remittance']['receipts']] == ['0.00', '1380.00']
    assert state['remittance']['totals']['allocated'] == '1380.00'
