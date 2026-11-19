import hashlib,json,time
from datetime import datetime,timezone
from app.config import RULE_VERSION
from app.modules.m6_reconcile.remit import canonical_parameters

def identity(content, parameters):
    # 同一来账重复导入或顺序不同的匹配重试规范成同一参数，复用同一批次，
    # 从而不会在库里多出一份分配证据。
    canonical = canonical_parameters(parameters)
    raw = content + json.dumps(canonical,sort_keys=True,ensure_ascii=False).encode() + RULE_VERSION.encode()
    return hashlib.sha256(raw).hexdigest()[:20]

def manifest(content, parameters, elapsed):
    return {'sha256':hashlib.sha256(content).hexdigest(),'rule_version':RULE_VERSION,
        'parameters':parameters,'elapsed_ms':round(elapsed*1000),
        'generated_at':datetime.now(timezone.utc).isoformat(),'timezone':'Asia/Shanghai'}
