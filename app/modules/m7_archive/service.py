from app.modules.m7_archive.export_out import checksum

def execute(state):
    remit=state.get('remittance') or {}
    state['archive']={'sha256':checksum(state),
        'evidence_count':len(state.get('calculations',[]))+len(state['quarantine'])+len(remit.get('receipts',[]))+len(remit.get('net_by_policy',[])),
        'ready':state.get('reconcile',{}).get('balanced',False)}
    return state
