# 保险保全追溯与保费补退核算

面向保险保全数据复核员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18250 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“运行批次”会重新计算，同源输入复用批次。

## 业务模块

- 保单采信：`/m1_ingest`、`/api/m1_ingest`
- 责任映射：`/m2_mapping`、`/api/m2_mapping`
- 保全选版：`/m3_versions`、`/api/m3_versions`
- 承保切片：`/m4_segments`、`/api/m4_segments`
- 保费重算：`/m5_calculate`、`/api/m5_calculate`
- 补退对账：`/m6_reconcile`、`/api/m6_reconcile`
- 结案归档：`/m7_archive`、`/api/m7_archive`

## 银行来账

`POST /api/runs` 的 `parameters.remittances` 可带入一笔或多笔银行来账（`id`、`amount`、`date`、`memo`、`policies`、`allocations`）。
只有附言/清单中列出的已采信保单才吸收来账，且每张保单不超过应补额；无法说明归属的余额全部暂挂，
不回摊到已匹配保单。整笔到账 = 已分配 + 暂挂。M6 工作台、M7 结案页与 `/api/archive/<id>/export.csv` 共用同一份分配结果。
替换来账用 `POST /api/runs/<id>/remittance`（须带当前 `version`）：成功时生成接替批次，旧批次保留且仍可下载；
版本不符、已替换或校验失败时旧版原样保留；重复导入同一来账不增加已分配金额。
示例：`sample_data/remittance.json`。

## 核心规则

按保全有效日拆分责任区间，撤销回滚指向原变更；实收补退按日精确分配尾差。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、计算证据、隔离原因与对账。复核需提交当前版本，陈旧复核返回409。

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
