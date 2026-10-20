# 评测统计对抗审稿

日期：2026-10-21。范围：README、CONTRACTS、P0、ACCEPTANCE、tasks.json，对照现有eval_answer/eval_real/eval_public及169道检索题、20道答题题。审稿只读原代码和数据，未调用答题模型或生成新题。以下处置已获主执行agent接受；文档修订覆盖P0，跨文档合同与阶段验收由主执行agent同步。

## STAT-01：四次评分不能校准未来误报率

严重度：阻塞。旧位置：CONTRACTS第119–122行、P0-02第35–39行。两份答案各判两次并非四份独立答案，逐题波动率也不包含运行级相关性。

可复核反例：60题共享每轮答题质量，每轮以0.8概率全对、0.2概率全错，judge确定。两轮同分的概率为0.8²+0.2²=0.68，此时p_score=0。原binomial模型预测误报0；在校准通过条件下，后续无改动比较误拒概率为`0.8²*(1-0.8²)/(0.8²+0.2²)=0.3388235294`。即便增加同一轮内题目数，这个反例仍成立。

处置：ACCEPTED。默认0.10的原始0..2均分退步阈值改为运行前预登记的工程最小实际差异；候选值上限不变。p_score仅作稳定性诊断，不再推导门禁阈值或宣称<5%误报。独立null audit为可选后续证据，无证据固定uncalibrated，不引入大规模新强制前置任务；缺真实答案或评分仍为MISSING。

验收：覆盖共同运行冲击、p_score=0仍uncalibrated、非法或被替换预登记、缺评分/来源、schema2不得升格，以及历史baseline字节不变。

## STAT-02：每个任务一次仍构成反复使用同一holdout

严重度：阻塞。旧位置：CONTRACTS第91行、P1-02/03/04/05/06/07/08的任务级holdout验收。失败后回train改实现同样已消费holdout信息。

处置：ACCEPTED。日常开发及任务级质量检查只看dev；现有train名称仅兼容别名。全部算法、参数及可选实验规则冻结后，全计划一次联合holdout发布审计。参考holdout可提前计算并密封，只暴露完整性哈希；exposure ledger记录每次解封、候选和协议身份。失败后不能继续把该集合称为未见holdout，也不靠改标签或清空ledger重新获得通过。

验收：默认dev、无最终冻结身份不能解封、重复曝光有记录、失败后改算法须新未见来源；420题和原数值门槛均保留。M1/M2的开发结果与最终发布质量验收分别记状态。

## STAT-03：位置与事实覆盖schema不足以防止静默错判

严重度：阻塞。旧位置：P0-05第111/115行、CONTRACTS第109行；现有`scripts/eval_real.py::_loc_satisfied`仅识别pages/lines。

复核命令：

```python
import sys
from types import SimpleNamespace
sys.path.insert(0, "scripts")
import eval_real
print(eval_real._loc_satisfied(
    SimpleNamespace(kind="audio", loc=0, loc_end=2),
    {"time": [100, 110]},
))  # 旧实现返回True：未知time键被忽略
```

处置：ACCEPTED。先固定JSON schema再建题：显式page/line/second单位、起止和kind一致性、未知键拒绝；事实原子固定ID及充分证据AND/OR关系，pack覆盖以实际回读citation完整覆盖正确位置的原文span判定。正文与name_metadata分开；supporting citations不得重复刷排名。

验收：错页、错时标、缺一个合取span、未知单位/键、非法范围、缺事实各自exit2或事实未覆盖，不能以文件命中、范围相交或关键词命中替代。

## STAT-04：逐题bootstrap会把同源题当独立重复

严重度：高。旧位置：P0-03第66行、P1-10第205行、CONTRACTS第85行。分割按来源组并不自动使组内题目独立。

固定seed20261021、20,000次重采样的反例：8组各8题，7组每题候选提升0.5分，1组每题退步1分。总体均值+0.3125。逐题bootstrap的95%CI约为[0.171875,0.4296875]，来源组bootstrap为[-0.0625,0.5]。前者会错误通过现有LIST/PACK显著提升门槛。

处置：ACCEPTED。新product/answer的paired CI整组重采样，保留组内全部题并重算题均值；报告题数、来源组数、算法/seed/reps，少于12个独立来源组不作显著提升宣称。0.85/0.90等目标保留为冻结集工程门槛，不能据此宣称已证明总体比例。现有legacy逐题检验保留历史诊断用途。

验收：上述反例组级CI必须覆盖0；空组、缺组映射、组身份不一致拒绝；同run自身比较delta=0。真实显著提升证据不足不能用mock数据补齐。
