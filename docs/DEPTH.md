# 深度登记 保险保全追溯与保费补退核算

规模：中型。核心规则：按保全有效日拆分责任区间，撤销回滚指向原变更；实收补退按日精确分配尾差。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 保单采信 | 查看并复算保单采信 | `/api/m1_ingest` | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py` |
| M2 责任映射 | 查看并复算责任映射 | `/api/m2_mapping` | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py` |
| M3 保全选版 | 查看并复算保全选版 | `/api/m3_versions` | `app/modules/m3_versions/rules.py` | `tests/test_versions.py` |
| M4 承保切片 | 查看并复算承保切片 | `/api/m4_segments` | `app/modules/m4_segments/rules.py` | `tests/test_segments.py` |
| M5 保费重算 | 查看并复算保费重算 | `/api/m5_calculate` | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py` |
| M6 补退对账 | 查看并复算补退对账、分配银行来账 | `/api/m6_reconcile` | `app/modules/m6_reconcile/compare.py`、`remit.py` | `tests/test_reconcile.py`、`tests/test_remittance.py` |
| M7 结案归档 | 查看并复算结案归档 | `/api/m7_archive` | `app/modules/m7_archive/export_out.py` | `tests/test_archive.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030中文 | 固定UTF-8失败 | 按编码链回退 | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_legacy_encoding` |
| TRAP-02 | 表头BOM | 首列键失配 | 去除BOM | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom` |
| TRAP-03 | 引号内换行 | 分行误计 | csv逻辑记录 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_preserves_multiline_note` |
| TRAP-05 | 列数错位 | 静默补列 | WIDTH隔离 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_width_isolation` |
| TRAP-06 | 全角千分位 | float失败 | NFKC与Decimal | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_thousands` |
| TRAP-07 | 空值哨兵 | 空值当零 | MISSING隔离 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_missing_numeric` |
| TRAP-08 | 前导零ID | 编码失真 | 字符串保形 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_identifier_zeroes` |
| TRAP-10 | 浮点误差 | 累计失准 | Decimal求和 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_decimal_precision` |
| TRAP-14 | 同版冲突 | 最后覆盖 | 整组隔离 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_real_conflict_quarantine` |
| TRAP-18 | 三种空态 | 除零或伪成功 | 三个错误码 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_empty_three_states` |
| TRAP-26 | 旧版本复核 | 覆盖新结论 | 事务条件更新 | `app/store.py` | `tests/test_archive.py::test_stale_review` |
| TRAP-27 | 一笔来账多保单 | 按比例摊入已匹配保单 | 仅足额核销，余额暂挂 | `app/modules/m6_reconcile/remit.py` | `tests/test_remittance.py::test_unexplained_amount_goes_to_suspense_not_prorated` |
| TRAP-28 | 同一来账重复导入 | 再次累加已分配 | 来账号去重 + 纯函数重算 | `app/modules/m6_reconcile/remit.py` | `tests/test_remittance.py::test_identical_reimport_is_idempotent` |
| TRAP-29 | 替换来账失败 | 留下半笔新分配 | 先重算校验，再条件写入 | `app/store.py` `app/main.py` | `tests/test_remittance.py::test_replace_keeps_old_version_on_stale` |
| TRAP-23 | 生效日端点 | 双计生效日 | 半开区间 | `app/modules/m4_segments/rules.py` | `tests/test_segments.py::test_halfopen_conservation` |
| TRAP-24 | 分币尾差 | 段合计不等 | 最大余额法 | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py::test_largest_remainder_conserves` |

## 对账规则

输入行数=采纳+隔离+空行+重复表头+完全重复；业务总数与计算证据独立求和。隔离行保留来源行、ID、原因码与原值。
来账对账额外要求：整笔到账=已分配+暂挂；分配后实收=原实收+已分配；分配后补退=原补退−已分配=各保单净额合计；
已分配不超过正补缴合计。工作台（M6）、结案页（M7）与结案下载共用 `state['remittance']`。

## 已知不覆盖

不做多租户权限和外部服务联调。行业规则以项目数据字典为准。文件上限2MB，逐行解析后在上限内完成分组计算。
