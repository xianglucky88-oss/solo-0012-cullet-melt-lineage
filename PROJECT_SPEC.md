# 混料、归因与成分放行数据契约

## 批式口径（`materials` 物料图）

- 每个版本用 `series`、`revision` 标识，首版无 `parent`，后续必须在同系列引用已冻结父版。输入 `materials` 为完整快照而不是补丁，顺序可任意。`feed` 提供 `id` 和整数克 `grams`；`melt` 另有 `loss_g` 和非空 `inputs`，每项绑定上游物料 `from` 和取料 `grams`。
- 验证每炉投入克数等于成品加损耗，全图无环，没有未知上游，多条下游取料之和不能超过对应上游产出。验证失败不会入库。
- 假设每炉充分混合，取料按原料组成同比例分配，损耗也按比例从该炉投料扣除。沿拓扑展开，所有比例以精确分数保存，不发生四舍五入。事实保留出料、剩余量、投入边、原料来源的成品克数和本炉损耗克数，不二次累计已在上游消耗的克数。
- SQLite 原子保存原始规范化载荷、摘要和导出事实。重复发布同一版本同摘要为空操作，异摘要拒绝。读取同时比较父版和当前版完整事实，`changed` 包括新增、删除或不同的物料键（含来源序号变化）。

## 成分化验口径（`component` 化验谱系）

玻璃总质量守恒不等于成分守恒：粉尘可能带走远多于按玻璃克数平摊的待验成分。声明 `component` 的版本在原有质量图上叠加逐实物、逐去向的含量区间。

### 输入

- `component`：成分名（或 `{"id", "unit": "ppm"}`），全系列必须一致；单位恒为整数 ppm（`10^6` 为纯物质）。
- 材料（`feed` 或 `melt`）可声明 `limit_ppm`，或 `limit: {"component", "min_ppm", "max_ppm"}`，表示该实物作为下一炉投料时必须满足的含量窗口。
- `samples`：实验室实测实物区间，每项 `id`、`of`（物料）、可选 `stream`（`material` 默认，或 `loss` 分相损耗）、`min_ppm`/`max_ppm`；可选 `label` 与 `retained`。`loss` 只能指向确有 `loss_g > 0` 的熔次。
- `composites`：实验室按比例混合取样，`mix` 各项为 `{"sample"|"material", "parts"}`，`parts` 合计必须为 100；混合区间 `min_ppm`/`max_ppm` 约束各实物含量的加权和。
- `retests`：复验条目，`id`、`condition`（具体复验条件，必填）、`sample`（须标记 `retained`）或 `of`+`stream`，可选 `pending` 或已完成区间。一条复验只重测一个物理流向，不允许两条复验共用同一目标。
- `signoffs`：对声明了限制的熔次签结论，`verdict` 取 `released`（无条件合格）、`hold`（风险未排除继续等）或 `conditional`（绑定一个或多个 `retests`，只排定可用克数）。

### 约束与事实

- 每个物理流向（各物料的 `material`、各熔次的 `loss`）各有一个浓度变量；每条熔次约束成分守恒：`Σ 取料克数 × 上游 material 浓度 = 产出克数 × 本炉 material 浓度 + 损耗克数 × 本炉 loss 浓度`。损耗浓度自由，因此粉尘可远高于玻璃摊分水平。
- 所有浓度以精确分数（`fractions.Fraction`）保存；上下界在**所有相容解释**上用线性规划求极值，不取方便端点、不做区间近似。
- 每个上下界附 `min_support`/`max_support`，列出真正决定该极值的实测样品、混合样、复验与称量记录（可删性检验后仍改变极值的行），字段分别为 `sample`、`composite`、`retest`、`weighing`。`min_g`/`max_g` 给出与该实物克数相乘后的绝对成分质量。
- 只有全部相容解释都不触及限制窗口，且实际所取上游料都有合格依据时，才能签 `released`；否则需要 `hold` 或写明条件的 `conditional`。程序会拒绝把仍有超标风险或依赖未合格上游的批次签成 `released`。
- 合格性沿**实际取料边**传递：批号、放行单号或另附报告一律视为文件引用而非实测，出现在样品、材料、取料边、混合样或复验上即拒绝。称量显示已投入但缺少合格依据的实物保留 `physical_output_g` 与取料事实，但 `qualified_g = 0`，并且不能使下游合格、不会在下一炉自动解禁。
- 相同 `label` 的不同样品可以共存，但各自保留独立实测，绝不共享区间；共享关系单列在 `evidence.shared_labels`。
- 返回的状态：`qualified`（可作为合格投料）、`conditional`（已排定可用克数，未验成前不得当合格料）、`hold`（超标风险未排除）、`unsigned`（有安全上界但尚未签核）、`unqualified`（缺合格依据）。
- `summary` 给出各状态计数、`qualified_output_g`、`conditional_planned_g` 与 `held_output_g`；`qualified_g` 只有合格时为产出克数，`conditional` 时 `planned_g` 为可排定克数而 `qualified_g` 为 0。

### 复验与两炉共用

- `joint_use` 逐条复验说明哪些下游支线受同一次上游分相复验影响，`branches` 列出这些熔次；`jointly_usable` 仅在复验已完成且所有支线都可合格时为真。未能共用时 `reason` 逐条给出各支线为何仍不能用同一结论。
- `branch_status` 逐支线给出上下界、支撑记录、状态与原因，便于质检员分别核对。
- 新版读回 `assay_changed`：`changed` 列出变化的证据/物料/签核键，`status_changed` 给出状态迁移，`retest_impact` 逐复验给出各支线复验前后的状态与当前上下界。

### 版本与可复核

- 已发布版本的实测克数、当时依据与结论冻结可复核；旧版本读回逐字节不变。
- 后续版本不得删除或改写已冻结的样品、混合样、复验条件与签核；已完成的复验不能退回 `pending`。复验结果到来后，可由新版把 `conditional` 重签为 `released`，但必须引用该复验的完成结果；不能凭另一份区间或另一份报告把当时有条件的投料追认为当时已合格。

`cullet/recipe.py` 解析质量与化验合同，`cullet/balance.py` 约束批式物料 DAG 并归因质量，`cullet/assay.py` 提供精确有理数线性规划，`cullet/component.py` 建立成分谱系、放行结论与复验影响，`cullet/releases.py` 保存和比较冻结版本。
