from decimal import Decimal

def balance(state):
    accepted=len(state['rows']);isolated=len(state['quarantine'])
    accounted=accepted+isolated+state['blank_count']+state['header_count']+state['duplicate_count']
    remittance=state.get('remittance') or {}
    totals=remittance.get('totals') or {}
    remittance_balanced=totals.get('balanced',True) and all(r.get('balanced',True) for r in remittance.get('receipts',[]))
    return {'input':state['input_count'],'accepted':accepted,'isolated':isolated,
        'blank':state['blank_count'],'headers':state['header_count'],'duplicates':state['duplicate_count'],
        'accounted':accounted,'count_balanced':accounted==state['input_count'],
        'remittance_balanced':remittance_balanced,
        'balanced':accounted==state['input_count'] and remittance_balanced,
        'business':state.get('business',{}),
        'remittance':totals}
