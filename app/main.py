import importlib,base64,json
from flask import Flask,jsonify,request,redirect,Response
from app.config import MODULES
from app.errors import DomainError
from app.workflow import run,load
from app import store
from app.web import payload
from app.modules.m7_archive.export_out import bundle

def create_app():
    app=Flask(__name__);app.config['MAX_CONTENT_LENGTH']=2_000_000
    app.add_template_filter(lambda value: json.dumps(value,ensure_ascii=False,indent=1),'payload_json')
    for entry in MODULES:app.register_blueprint(importlib.import_module('app.modules.'+entry[0]+'.handler').bp)
    @app.errorhandler(DomainError)
    def domain_error(error):return jsonify(code=error.code,message=error.message),error.status
    @app.errorhandler(404)
    def missing(error):return jsonify(code='NOT_FOUND',message='入口不存在'),404
    @app.get('/')
    def index():return redirect('/'+MODULES[0][0])
    @app.get('/healthz')
    def health():return jsonify(status='ok')
    @app.post('/api/runs')
    def submit():
        content,params=payload();state=run(content,params);return jsonify(store.save(state['run_id'],state))
    @app.get('/api/runs')
    def list_runs():return jsonify(store.history())
    @app.get('/api/runs/<run_id>')
    def get_run(run_id):return jsonify(store.get(run_id))
    @app.post('/api/runs/<run_id>/review')
    def review(run_id):
        data=request.get_json(silent=True) or {}
        if not isinstance(data.get('version'),int):raise DomainError('VERSION','请提交复核版本')
        return jsonify(store.review(run_id,data['version'],data.get('decision')))
    @app.post('/api/runs/<run_id>/remittance')
    def replace_remittance(run_id):
        """替换某批次的来账分配。

        成功时生成接替批次并保留旧版；版本不符、批次已替换或重算校验失败，
        旧正文与旧分配原样保留，不会出现半笔新分配。
        """
        data=request.get_json(silent=True) or {}
        if not isinstance(data.get('version'),int):raise DomainError('VERSION','请提交替换版本')
        if not isinstance(data.get('remittances'),list):raise DomainError('PARAMETERS','remittances 必须是数组')
        stored=store.get(run_id)
        content=base64.b64decode(stored['source_b64'])
        parameters=dict(stored.get('manifest',{}).get('parameters',{}) or {})
        parameters['remittances']=data['remittances']
        # 先完整重算并校验，任何异常都在写入前抛出；旧版因此原样保留。
        state=run(content,parameters)
        return jsonify(store.replace(run_id,state,data['version']))
    @app.get('/api/archive/<run_id>/export.csv')
    def download(run_id):
        state=store.get(run_id)
        if not state.get('reconcile',{}).get('balanced'):raise DomainError('UNBALANCED','对账未平')
        return Response(bundle(state),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=evidence.csv'})
    with app.app_context():
        if not store.history():
            state=run(load('dirty.csv'),{});store.save(state['run_id'],state)
    return app

app=create_app()
if __name__=='__main__':app.run(host='0.0.0.0',port=8000)
