# 玻璃回炉混料质量谱系

**数据工程 · 数仓与数据湖。** 将称量批次与熔次转换成可追溯的物料谱系，按来源批次分解成品及损耗，并比较前后发布版本的事实。它不管理库存或炉温设备。

系统支持两种并存口径：

- **批式口径**：每个熔次是封闭物料 DAG（`examples/batch.json`）。
- **连续班次口径**：回炉线换班不停炉，罐内残料跨班混合；交班称量进共享台账而不是新原料，来源未明的残量只确认质量、不确认来源（`examples/line.json`、`examples/line_bounded.json`）。

在仓库根目录以 Python 3.9+ 运行：

```sh
# 批式口径
python3 -B -m cullet --db demo.sqlite publish examples/batch.json
python3 -B -m cullet --db demo.sqlite read amber-trial bench-01

# 连续班次口径（formal：交接量必须被质量账钉死）
python3 -B -m cullet --db demo.sqlite publish examples/line.json
python3 -B -m cullet --db demo.sqlite read cullet-line day1
python3 -B -m cullet --db demo.sqlite report cullet-line day1
python3 -B -m cullet --db demo.sqlite report cullet-line day1 --shifts S1 S2

# provisional：交接量只有上下界时先签能确认的分量，未归部分单列
python3 -B -m cullet --db demo.sqlite publish examples/line_bounded.json

# 验证
python3 -B examples/smoke.py
python3 -B examples/smoke_line.py
```

批式样例第一次回炉各由两个来源贡献 `41/2` 克；更正上游配比后归因变为 `123/5` 克和 `82/5` 克，总产出仍为 41 克，旧版本不变。

连续线样例中，有界交班称量 H2（50..60 克）被同一份质量账钉死为 55 克；开口残量 H0 以 `pending` 来源传递，清空后的 p4 只含新料 lotF、来源完全可证。次日共同重确认 H1（80→70 克）后，读回的 `impact` 指出 p2/p3 来源变化、各出料实测质量不变，并以清空 c1 作为传播截断点。详见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
