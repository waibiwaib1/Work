# eval

使用 swe-bench-evaluator skill。用户只粘贴一个 `instance_id`。不要向用户要 prompt、patch、diff 或日志。

从 `GSB\runs\<instance_id>\task.json` 取题面、`base_commit`、`test_cmd` 和两边的 side 记录。工作区是 `GSB\task\<instance_id>\a` 和 `\b`。测试日志是 `GSB\runs\<instance_id>\logs\a-test.log` 和 `b-test.log`。不要搜索 candidates 或 SWE-bench 数据集，也不要重跑测试。

按 skill 先读题面和两边的 diff，再读测试日志的摘要（`logs\a-test.log` 和 `b-test.log`，很长时只看末尾和 FAILED/ERROR 汇总）。过程只扫 `logs\a.log` 和 `logs\b.log`，不要读 rollout，也不要为了读轨迹写脚本。然后打分、写测试结论、写 GSB。

八项定稿后、回答之前，按 skill 把这一题 upsert 进 `GSB\runs\_harness\evals.json`（和 `status.json`、`sheet.csv` 同目录，所有 eval 共用这一份）。只改 `evals.<instance_id>`，重评覆盖该题，别的题不动。不要写 `sheet.csv` 或 `task.json`。八项之后不要再提这个文件。

回答前面可以有别的内容。最后按 skill 的 Output 写这 8 项，不要放进代码块，后面不要再加总结。每一项标签单独一行，内容从下一行开始，项与项之间空一行。不要用反引号、单引号或「」包文件名、函数名和报错。

不要写 harness。模型改完之后的测试叫运行环境。改代码的一方叫模型或 AI。模型自己说测过了、运行环境却没过时，用白话写与模型宣称的全部通过不一致，不要写成口号。

`GSB 结论` 只能是 `A 更好`、`Same`、`B 更好`。测试结论写出命令、退出码、通过或失败数量，以及是否打在题目要求的行为上，报错用白话带过。交付完整性描述和 GSB 理由写短，不要逐个函数罗列。描述以「AI在思考中」起句，再说改了哪一块、实现了什么行为；做到了就写「题目要求的……基本实现」，没做到就写「但是……导致……」。GSB 理由按「两边都……。按题目要求的……」收束。测试只提跟这次修改相关或新加的那条，没有就不写，不要写整库通过数量、退出码或无关失败。有高下时写较差一侧导致什么、较好一侧接上了哪一点，打平时写两边相同以及方向相反但不影响功能的差别。

This command will be available in chat with /eval
