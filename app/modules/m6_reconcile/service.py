from app.modules.m6_reconcile.compare import balance
from app.modules.m6_reconcile.remit import allocate

def execute(state):
    # 来账分配是 calculations 的纯函数：重跑、重试或重复导入都从这里重算，
    # 不在旧结果上累加，因此不会新增已分配金额。
    state['remittance']=allocate(state)
    state['reconcile']=balance(state)
    return state
