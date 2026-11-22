# 玻璃回炉混料质量谱系与跨厂交割核对

**数据工程 · 数仓与数据湖。** 把称量批次与熔次转换成可追溯的物料谱系，按来源批次分解成品及损耗，并在甲乙两厂之间整组核对出厂磅单、合装入厂单、分装合装、损耗、退回与实际投料。它不管理库存或炉温设备。

在仓库根目录以 Python 3.9+ 运行：

```sh
# 单厂基线（向后兼容）
python3 -B -m cullet --db demo.sqlite publish examples/batch.json
python3 -B -m cullet --db demo.sqlite read amber-trial bench-01
python3 -B examples/smoke.py

# 跨厂叙事：先冻结交换契约，再发布两厂炉次，最后结算互证
python3 -B -m cullet --db demo.sqlite publish examples/exchange-x1.json
python3 -B -m cullet --db demo.sqlite publish examples/site-a-a1.json
python3 -B -m cullet --db demo.sqlite publish examples/site-b-b1.json
python3 -B -m cullet --db demo.sqlite settle ab-flow x1
python3 -B -m cullet --db demo.sqlite read-exchange ab-flow x1

# 第二版：磅单更正（签注损耗/保留待核）+ 两厂切换配方 + 待归料核明身份
python3 -B -m cullet --db demo.sqlite publish examples/exchange-x2.json
python3 -B -m cullet --db demo.sqlite publish examples/site-a-a2.json
python3 -B -m cullet --db demo.sqlite publish examples/site-b-b2.json
python3 -B -m cullet --db demo.sqlite settle ab-flow x2

python3 -B examples/smoke_exchange.py
```

## 叙事数据说明

甲厂两张出厂磅单（`A-WB-1`、`A-WB-2`）对应乙厂一张合装入厂单（`B-IN-1`），交割 `D1` 整组核对：

- P1 出厂 100 / 入厂 98：x1 时 2g 缺口无法区分少收与途中损耗，保留为待核；x2 凭双方签注证据确认为途中损耗。
- P2 出厂 100 / 入厂 96：4g 缺口始终没有联合签注，两个版本都保留在 `unexplained_g`，不会被写成已确认损耗。
- 乙厂 10g 已称量但身份未核明的待归料在 x1 即按实际入炉发布实测成品（`pending_origins` 精确分数 25/3 成品、5/3 损耗），但不计入已验证来源；x2 核明为 P1，同一份额换标签、无新增克数。
- 乙未开包的 P1/P2 各 10g 退回甲厂；退回只恢复甲厂余额，乙厂已耗用份额不恢复。
- 乙厂把成品 `b-m1` 交还甲厂，甲厂以 `from_external` 引用——这是跨站点边而非物料成环；测试另构造回到已耗用祖先的真环验证冲突检测。
- P3 出厂仅 40g，两厂各自申报超量（同一百克料两侧各报六十克的变体）：结算拒绝两侧同时认证，冲突在报告中保留到用量被改正。

详见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
