# 玻璃回炉混料质量谱系

**数据工程 · 数仓与数据湖。** 将称量批次与熔次转换成可追溯的物料谱系，按来源批次分解成品及损耗，并比较前后发布版本的事实。它不管理库存或炉温设备。

系统在原有的玻璃质量谱系之上增加 **成分化验谱系**：除玻璃克数守恒外，还按同一套称量事实守恒声明的痕量成分（默认 ppm），并把每次放行、待验与复验结论冻结在版本里。

在仓库根目录以 Python 3.9+ 运行：

```sh
# 批式质量谱系（原有口径）
python3 -B -m cullet --db demo.sqlite publish examples/batch.json
python3 -B -m cullet --db demo.sqlite read amber-trial bench-01

# 带成分化验的炉次
python3 -B -m cullet --db demo.sqlite publish examples/assay.json
python3 -B -m cullet --db demo.sqlite read dust-trial day1

# 验证
python3 -B examples/smoke.py
python3 -B examples/smoke_assay.py
```

批式样例第一次回炉各由两个来源贡献 `41/2` 克。更正上游配比后归因变为 `123/5` 克和 `82/5` 克，总产出仍为 41 克，旧版本不变。

`examples/assay.json` 是一炉偏粉尘的硫成分例子：150 克玻璃与 50 克分相损耗都来自同一批投料，损耗的硫含量可远高于按玻璃克数平摊的值。两条下游支线共用同一份待验粉尘复验 `rt-first`；复验回来前，它们都只能排定可用克数、不能作为合格料投用。详见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
