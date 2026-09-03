# AOU runtime interface

## 为什么与分析项目解耦

AOU 决定“给 agent 看见什么、何时注入、如何判定机会是否出现”；本项目决定
“如何从冻结轨迹重建证据并评估 Verification Resilience”。把二者写死在同一
靶场路径会导致 AOU、模型、数据修复和论文分析相互污染。

## 新 AOU 至少需要冻结的内容

1. **AOU identity**：稳定名称、版本、target adapter、C0/C1 条件；
2. **Opportunity contract**：触发端点、方法、响应形状、可观察窗口；
3. **Runtime program**：注入规则、动态值、重放/会话约束；
4. **Registered finding**：agent 最终应建立或否定的精确安全事实；
5. **Primary evidence contract**：哪些 target-side event 足以形成正/负证据；
6. **Verification predicates**：复探、交叉验证、替代端点/载荷等行为事实；
7. **Report semantic contract**：什么算 closure，什么只是 mention/attempt；
8. **Pairing metadata**：同一 unit/block 下的 Native C0 与 ATO C1；
9. **Provenance**：AOU hash、runtime hash、target image/version、instruction hash。

## Campaign 最小交付

一个冻结 campaign 应让 census 能解析：

- campaign manifest；
- pair/episode assignment；
- episode lifecycle 与 terminal status；
- model、AOU、condition、unit/block；
- final report 及哈希；
- target-side event 文件与 source hash；
- agent session 中可定位的 episode/campaign identity。

## 加入新 AOU 的顺序

1. 先写 registered finding 与 evidence/semantic contracts；
2. 再实现 RuntimeProgram 和 target adapter；
3. 用单 pair smoke 同时验证 C0 与 C1；
4. 冻结 AOU/runtime/target/instruction 哈希；
5. 生成 campaign 并登记到 registry；
6. 运行 census，确认 pair/episode/session/event 全覆盖；
7. 才进入 facts、Judge、semantic、statistics。

如果 registered finding 在实验后才改变，应像 JWT Stage 20 一样建立新的修复
workspace、限定影响范围、保持其他 AOU 对象恒等，并重新生成所有受影响的下游
统计与审计，不能原地修改旧结果。
