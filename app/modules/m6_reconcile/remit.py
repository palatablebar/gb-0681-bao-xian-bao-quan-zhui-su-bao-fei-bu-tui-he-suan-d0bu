"""银行来账分配与暂挂核算。

裁决边界（完整定义见 docs/DATA-DICTIONARY.md 「银行来账」）：

1. 分配范围 = 附言/显式列出的保单号 ∩ 本批已采信保单。未列出的保单一律不吸收来账。
2. 退费保单（补退为负）与已结清保单（补退为零）不吸收来账。
3. 只有“归属可说明”的金额才允许分配：
   - 来账给出逐保单金额时，按该金额核销，且不得超过该保单应补额；
   - 仅有一张待补缴保单时，可部分核销；
   - 列出多张待补缴保单且未给逐保单金额时，按声明顺序逐张足额核销，
     不足以足额结清下一张的余额不再拆分，全部转入暂挂。
4. 暂挂款保留原始来账（号、到账日、附言、金额）与匹配依据（命中/未命中的引用），
   不回摊到任何已匹配保单。
5. 同一来账号同一内容重复导入只记一次；同一来账号内容冲突直接拒绝，不取最后一行。
6. 分配是 calculations 与参数的纯函数，重复导入或匹配重试只重算，不在旧结果上累加。

恒等式：整笔到账 = Σ已分配 + 暂挂。
"""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.errors import DomainError

CENT = Decimal('0.01')
_TOKEN = re.compile(r'([A-Za-z]{1,6})(\d{1,12})')


def _money(value, label):
    """把输入金额归一为精确到分的 Decimal；超过两位小数视为不可对平。"""
    if isinstance(value, bool) or value is None:
        raise DomainError('REMITTANCE', f'{label}无效')
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise DomainError('REMITTANCE', f'{label}无效')
    if not amount.is_finite() or amount < 0:
        raise DomainError('REMITTANCE', f'{label}无效')
    if amount != amount.quantize(CENT):
        raise DomainError('REMITTANCE', f'{label}必须精确到分')
    return amount.quantize(CENT)


def _text(value):
    return '' if value is None else str(value).strip()


def _allocations(raw, rid):
    """逐保单金额。支持 {保单: 金额} 或 [{policy, id, amount}]。"""
    value = raw.get('allocations')
    if value in (None, '', []):
        return []
    pairs = []
    if isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, list):
        for entry in value:
            if not isinstance(entry, dict):
                raise DomainError('REMITTANCE', f'来账 {rid} 的逐保单金额必须是对象')
            pid = _text(entry.get('policy') or entry.get('id'))
            pairs.append((pid, entry.get('amount')))
    else:
        raise DomainError('REMITTANCE', f'来账 {rid} 的逐保单金额格式无效')
    seen, out = set(), []
    for pid, amount in pairs:
        if not pid:
            raise DomainError('REMITTANCE', f'来账 {rid} 的逐保单金额缺少保单号')
        if pid in seen:
            raise DomainError('CONFLICT', f'来账 {rid} 对保单 {pid} 重复指定金额')
        seen.add(pid)
        out.append({'policy': pid, 'amount': _money(amount, f'来账 {rid} 保单 {pid} 的金额')})
    return out


def _normalize(raw, index):
    if not isinstance(raw, dict):
        raise DomainError('REMITTANCE', f'第{index}笔来账必须是对象')
    rid = _text(raw.get('id') or raw.get('reference') or raw.get('serial'))
    if not rid:
        raise DomainError('REMITTANCE', f'第{index}笔来账缺少来账号')
    amount = _money(raw.get('amount'), f'来账 {rid} 的到账金额')
    memo = _text(raw.get('memo') or raw.get('remark') or raw.get('附言'))
    value_date = _text(raw.get('date') or raw.get('value_date') or raw.get('到账日'))
    if value_date:
        try:
            value_date = date.fromisoformat(value_date).isoformat()
        except ValueError:
            raise DomainError('DATE', f'来账 {rid} 的到账日不是 ISO 日期')
    policies = raw.get('policies') or []
    if not isinstance(policies, list):
        raise DomainError('REMITTANCE', f'来账 {rid} 的保单清单必须是数组')
    policies = [str(p).strip() for p in policies if str(p).strip()]
    item = {'id': rid, 'amount': amount, 'memo': memo, 'value_date': value_date,
            'policies': policies, 'allocations': _allocations(raw, rid)}
    item['hash'] = hashlib.sha256(
        json.dumps(item,sort_keys=True,ensure_ascii=False,default=lambda v: format(v,'f') if isinstance(v,Decimal) else str(v)).encode('utf-8')
    ).hexdigest()[:16]
    return item


def normalize(raw_items):
    """校验并按来账号去重。内容相同的重复导入只保留第一条；内容冲突拒绝。"""
    if raw_items in (None, ''):
        raw_items = []
    if not isinstance(raw_items, list):
        raise DomainError('PARAMETERS', 'remittances 必须是数组')
    items, seen, duplicates = [], {}, 0
    for index, raw in enumerate(raw_items, 1):
        item = _normalize(raw, index)
        if item['id'] in seen:
            if seen[item['id']]['hash'] != item['hash']:
                raise DomainError('CONFLICT', f'来账 {item["id"]} 号内容冲突，拒绝覆盖')
            duplicates += 1
            continue
        seen[item['id']] = item
        items.append(item)
    return items, duplicates


def _refs(item, known, prefixes):
    """附言解析：显式清单与附言中的保单号按声明/出现顺序取并集。

    只把前缀与本批保单一致的数字串当作保单引用，避免把来账号、日期误记为未匹配。
    """
    found, unmatched = [], []

    def take(token):
        key = token.strip().lower()
        if not key:
            return
        canonical = known.get(key)
        if canonical is None:
            if token.strip() not in unmatched:
                unmatched.append(token.strip())
        elif canonical not in found:
            found.append(canonical)

    for policy in item['policies']:
        take(policy)
    for prefix, digits in _TOKEN.findall(item['memo']):
        if prefix.lower() in prefixes:
            take(prefix + digits)
    return found, unmatched


def _fmt(value):
    return format(Decimal(value).quantize(CENT), 'f')


def _allocate_receipt(item, known, prefixes, due):
    scope, unmatched = _refs(item, known, prefixes)
    # 逐保单金额本身就是匹配依据：即使附言未列出，也纳入本次分配范围。
    for entry in item['allocations']:
        pid = known.get(entry['policy'].lower())
        if pid is None:
            raise DomainError('REMITTANCE', f'来账 {item["id"]} 指定了不属于本批的保单 {entry["policy"]}')
        if pid not in scope:
            scope.append(pid)
    remaining = item['amount']
    allocated = Decimal('0')
    lines = []

    def add_line(pid, take, status, note=''):
        nonlocal remaining, allocated
        take = take.quantize(CENT)
        remaining -= take
        allocated += take
        lines.append({'policy': pid, 'due': _fmt(due_before[pid]), 'allocated': _fmt(take),
                      'net_after': _fmt(due[pid]), 'status': status, 'basis': note})

    due_before = {pid: due[pid] for pid in scope}

    # 1) 逐保单金额：归属由来账自身说明，可直接核销，但不得超过应补额。
    explicit = {}
    for entry in item['allocations']:
        pid = known[entry['policy'].lower()]
        if due_before[pid] <= 0:
            raise DomainError('REMITTANCE',
                              f'来账 {item["id"]} 指定的保单 {pid} 当前无应补缴，不能吸收来账')
        if entry['amount'] > due_before[pid]:
            raise DomainError('REMITTANCE', f'来账 {item["id"]} 对保单 {pid} 的金额超过应补额')
        explicit[pid] = entry['amount']
    if sum(explicit.values(), Decimal('0')) > item['amount']:
        raise DomainError('REMITTANCE', f'来账 {item["id"]} 的逐保单金额合计超过到账金额')
    for pid in scope:
        if pid in explicit:
            take = explicit[pid]
            due[pid] -= take
            add_line(pid, take, 'SETTLED' if due[pid] == 0 else 'PARTIAL', 'EXPLICIT')

    # 2) 其余金额：先按声明顺序尝试足额结清。
    open_due = [pid for pid in scope if due_before[pid] > 0 and pid not in explicit]
    if len(open_due) == 1:
        # 只剩一张待补缴保单：剩余金额归属无歧义，允许部分核销。
        pid = open_due[0]
        take = min(remaining, due[pid])
        due[pid] -= take
        add_line(pid, take, 'SETTLED' if due[pid] == 0 else 'PARTIAL', 'SOLE_REFERENCE')
    else:
        for pid in open_due:
            owed = due[pid]
            if remaining >= owed:
                due[pid] = Decimal('0')
                add_line(pid, owed, 'SETTLED', 'DECLARED_ORDER')
                continue
            # 声明顺序下第一张无法足额结清即停止：不再把余额拆给后面的保单。
            for rest in open_due[open_due.index(pid):]:
                lines.append({'policy': rest, 'due': _fmt(due[rest]), 'allocated': '0.00',
                              'net_after': _fmt(due[rest]), 'status': 'UNSETTLED',
                              'basis': 'INSUFFICIENT'})
            break

    # 3) 已结清或退费保单只做展示，不吸收来账。
    for pid in scope:
        if pid in explicit or pid in open_due:
            continue
        status = 'REFUND_DUE' if due_before[pid] < 0 else 'EVEN'
        lines.append({'policy': pid, 'due': _fmt(due_before[pid]), 'allocated': '0.00',
                      'net_after': _fmt(due_before[pid]), 'status': status, 'basis': 'NOT_ELIGIBLE'})

    order = {pid: i for i, pid in enumerate(scope)}
    lines.sort(key=lambda line: order.get(line['policy'], len(order)))
    suspense = remaining
    return scope, unmatched, lines, allocated, suspense


def canonical_parameters(parameters):
    """把 remittances 规范为“按来账号排序 + 完全重复项只留一条”。

    只用于计算批次幂等键，使同一来账重复导入或仅顺序不同的匹配重试
    落到同一个批次，而不是各生成一份分配证据。无法规范时原样返回。
    """
    if not isinstance(parameters, dict):
        return parameters
    raw = parameters.get('remittances')
    if not isinstance(raw, list):
        return parameters
    deduped, seen = [], set()
    try:
        for entry in raw:
            key = hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False,
                                            default=str).encode('utf-8')).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            rid = _text(entry.get('id') or entry.get('reference') or entry.get('serial')) if isinstance(entry, dict) else ''
            deduped.append((rid, key, entry))
    except Exception:
        return parameters
    deduped.sort(key=lambda item: (item[0], item[1]))
    canonical = dict(parameters)
    canonical['remittances'] = [entry for _, _, entry in deduped]
    return canonical


def allocate(state):
    """按裁决边界分配来账，返回来账明细、保单净额与合计。

    纯函数：结果只由入参、calculations 与 parameters.remittances 决定；
    重复执行不会累加已分配金额。
    """
    calculations = {c['id']: c for c in state.get('calculations', [])}
    known = {pid.lower(): pid for pid in calculations}
    prefixes = {pid.rstrip('0123456789').lower() for pid in calculations}
    due = {pid: Decimal(str(c['difference'])) for pid, c in calculations.items()}
    raw = (state.get('parameters') or {}).get('remittances')
    items, duplicates = normalize(raw)

    receipts = []
    for item in sorted(items, key=lambda r: (r['value_date'] or '9999-12-31', r['id'])):
        scope, unmatched, lines, allocated, suspense = _allocate_receipt(item, known, prefixes, due)
        receipts.append({
            'id': item['id'],
            'value_date': item['value_date'],
            'memo': item['memo'],
            'amount': _fmt(item['amount']),
            'scope': scope,
            'unmatched_refs': [ref for ref in unmatched if ref.lower() not in known],
            'allocated': _fmt(allocated),
            'suspense': _fmt(suspense),
            'balanced': allocated + suspense == item['amount'],
            'content_hash': item['hash'],
            'evidence': {'policies': item['policies'], 'allocations': item['allocations']},
            'lines': lines,
        })

    net_by_policy = []
    for pid in sorted(calculations):
        calc = calculations[pid]
        expected = Decimal(str(calc['expected']))
        paid = Decimal(str(calc['paid']))
        difference = Decimal(str(calc['difference']))
        net = due[pid]
        net_by_policy.append({
            'id': pid,
            'expected': _fmt(expected),
            'paid': _fmt(paid),
            'difference': _fmt(difference),
            'allocated': _fmt(difference - net),
            'net': _fmt(net),
            'settled': net == 0,
        })

    receipt_total = sum((Decimal(r['amount']) for r in receipts), Decimal('0'))
    suspense_total = sum((Decimal(r['suspense']) for r in receipts), Decimal('0'))
    allocated_total = sum((Decimal(r['allocated']) for r in receipts), Decimal('0'))
    business = state.get('business') or {}
    paid = Decimal(str(business.get('paid', '0')))
    expected = Decimal(str(business.get('total', '0')))
    difference = Decimal(str(business.get('difference', expected - paid)))
    net_total = sum((Decimal(r['net']) for r in net_by_policy), Decimal('0'))
    positive_due = sum((max(Decimal('0'), Decimal(str(c['difference'])))
                        for c in calculations.values()), Decimal('0'))
    # 财务口径分层对平：整笔到账 = 已分配 + 暂挂；
    # 分配后实收 = 原实收 + 已分配；分配后补退 = 原补退 − 已分配 = 各保单净额之和。
    identities = {
        'receipt_split': receipt_total == allocated_total + suspense_total,
        'net_conservation': difference - allocated_total == net_total,
        # 已分配不得超过全部正补缴额（退费保单不能被来账抵消）。
        'no_over_allocation': allocated_total <= positive_due,
    }
    sha = hashlib.sha256(json.dumps(
        {'receipts': [{'id': r['id'], 'hash': r['content_hash']} for r in receipts],
         'net_by_policy': net_by_policy,
         'totals': {'receipt': _fmt(receipt_total), 'allocated': _fmt(allocated_total),
                    'suspense': _fmt(suspense_total)}},
        sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return {
        'decision': {
            'scope': '附言/显式列出的保单号 ∩ 本批已采信保单；退费与已结清保单不吸收来账',
            'rule': '仅分配归属可说明的金额：逐保单金额按额核销；唯一待补缴保单可部分核销；'
                    '多张待补缴保单按声明顺序足额结清，不足部分暂挂，不按比例摊分',
            'order': '来账按到账日、来账号升序；保单按原来账声明顺序',
        },
        'receipts': receipts,
        'net_by_policy': net_by_policy,
        'duplicates': duplicates,
        'totals': {
            'receipts': len(receipts),
            'receipt': _fmt(receipt_total),
            'allocated': _fmt(allocated_total),
            'suspense': _fmt(suspense_total),
            'paid_before': _fmt(paid),
            'paid_after': _fmt(paid + allocated_total),
            'difference_before': _fmt(difference),
            'difference_after': _fmt(difference - allocated_total),
            'net': _fmt(net_total),
            'duplicates': duplicates,
            'identities': {k: bool(v) for k, v in identities.items()},
            'balanced': all(identities.values()) and all(r['balanced'] for r in receipts),
            'evidence_sha256': sha,
        },
    }
