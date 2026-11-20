# 玻璃回炉混料质量谱系

**数据工程 · 数仓与数据湖。** 将称量批次与熔次转换成可追溯的物料谱系，按来源批次分解成品及损耗，并比较前后发布版本的事实。它不管理库存或炉温设备。

在仓库根目录以 Python 3.9+ 运行：

```sh
python3 -B -m cullet --db demo.sqlite publish examples/batch.json
python3 -B -m cullet --db demo.sqlite read amber-trial bench-01
python3 -B examples/smoke.py
```

样例第一次回炉各由两个来源贡献 `41/2` 克。更正上游配比后归因变为 `123/5` 克和 `82/5` 克，总产出仍为 41 克，旧版本不变。详见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
