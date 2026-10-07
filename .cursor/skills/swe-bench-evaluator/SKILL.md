---
name: swe-bench-evaluator
description: Pair-wise GSB evaluation of two Codex runs (A and B). The user pastes only an instance_id. Score each side's delivery completeness 1-5 with a description, write a test-result conclusion for each side from its test log, then write one GSB verdict and one GSB reason. Use when the user runs /eval or asks to annotate a GSB instance.
---

# Pair-wise GSB Evaluator

Evaluate two runs of the same prompt. The user pastes only an `instance_id`. Load the prompt, both diffs, both rollouts, and both test logs yourself. Other text may come first. End the answer with these eight fields, in this order, as plain text. Do not wrap them in a code block:

1. `A-测试结论`
2. `A-交付完整性（1-5）`
3. `A-交付完整性描述`
4. `B-测试结论`
5. `B-交付完整性（1-5）`
6. `B-交付完整性描述`
7. `GSB 结论`
8. `GSB 理由`

Do the checklist, file reading, test-log reading, and devil's advocate before writing. Anything else in the answer comes before the eight fields. Do not add a 正确/部分正确/错误 verdict or a recap after them.

## Inputs

The only input is an `instance_id`. Do not ask the user for the prompt, the patch, the diffs, or the logs.

Read:

`GSB\runs\<instance_id>\task.json`

Take `problem_statement`, `base_commit`, `test_cmd`, and `sides.a` / `sides.b` from that file. Do not search `candidates-3000.jsonl` or any SWE-bench dataset. There is no reference patch. Do not grade either side by similarity to some other fix.

Worktrees:

- A: `GSB\task\<instance_id>\a`
- B: `GSB\task\<instance_id>\b`

Trajectories, when the process must be judged. Read the plain-text session log, not the rollout JSONL:

- `GSB\runs\<instance_id>\logs\a.log`
- `GSB\runs\<instance_id>\logs\b.log`

`artifacts\a-rollout.jsonl` and `artifacts\b-rollout.jsonl` are the same session as raw events. Do not parse them and do not write a script to read them. Open a rollout only when the matching `logs\<side>.log` is missing.

Test logs, one per side. These are the re-run after the model finished, not the model's own session:

- `GSB\runs\<instance_id>\logs\a-test.log`
- `GSB\runs\<instance_id>\logs\b-test.log`

Ignore `logs\a-record.log` and `logs\b-record.log`. Those are ffmpeg output from the test recording.

If `task.json` is missing, stop and say so. If one worktree is missing, still score the side that exists and say which path is missing. Do not invent a score or a test result.

## What to ignore

Do not lower a score or cite these in the reason:

- Wall-clock time, token count, or how long the model thought
- A run that stops with no error message (the run was cut off)
- Network failures, HTTP errors, rate limits, or other request failures

Judge model behavior and the code it left behind.

## Workflow

### 1. Checklist from the statement

Read `problem_statement` and list every required behavior: goal, expected behavior, explicit constraints, and what "done" means. If the statement states a rule that applies to a class of entry points, list that class. Do not list files just because a reference patch touched them.

A different implementation that meets the statement is correct.

### 2. Read each side's real diff

In each worktree run:

```
git diff <base_commit> HEAD
```

Then read the changed files and the untouched siblings needed to judge coverage (other exporters, handlers, callers). Do not re-run tests.

If `sides.<side>.empty` is true or `head` is missing, that side changed no files.

Compare the rollout's completion claim with the diff. Saying a file was changed when the diff does not contain it is false success.

### 3. Conclude each side's test log

Do this before scoring. For each side read `test_cmd`, `sides.<side>.test_exit`, and that side's `logs/<side>-test.log`. Logs can be tens of thousands of lines. Read the last 150 lines first. If that tail has no summary, search the file for `FAILED`, `ERROR`, `AssertionError`, `TIMEOUT`, and a final count line. Do not transcribe a dump of repeated lint or font warnings.

`A-测试结论` and `B-测试结论` each state, in plain words and without quoting the log:

- The command that ran, and the exit code. `0` is the command exited clean. A positive code is a failure. Missing `test_exit` plus `TIMEOUT` in the log is a timeout. No log and no `test_exit` means the test was not recorded.
- What passed and what failed. Name the test and paraphrase the error. Do not paste a traceback or an error string.
- Whether that failure is in behavior this side was asked to change, or is outside the diff (tooling, line endings, a dependency the change did not touch).
- What this means for delivery: a passing command does not prove the statement is covered; a failing assertion on the required behavior means that behavior does not work.

### 4. Score delivery completeness separately

Score A only against the statement. Then score B only against the statement. Do not score one side relative to the other. The rubric is the task outcome: were the required behaviors implemented, does the code work, and did the model claim work it did not do.

| 分数 | 锚点 |
|---|---|
| 5 | 明示需求都实现，代码能跑，还补上了 statement 已暗示的边界；没有虚假成功 |
| 4 | 主要需求都达成，代码能跑，只有极少量细节遗漏；没有虚假成功 |
| 3 | 核心功能能跑，但有明显 bug，需要人工改；或有轻微虚假成功（非核心文件声称改了但没落下） |
| 2 | 主要需求没达成；或逻辑/依赖问题导致跑不起来；或大比例虚假成功（声称改了多处，实际只改了一部分） |
| 1 | 编译或启动即失败且完全不可用；答非所问；或宣称做完但没有落下对应改动 |

Hard caps:

- A missed core requirement cannot be 4 or 5.
- Claimed edits that are absent from the diff: a non-critical miss caps the score at 3; a large share of the claimed edit missing caps it at 2; a completion claim with no relevant diff is 1.
- One happy path, while the statement's other same-category entry points are untouched, is not 4 or 5.
- A test failure on the required behavior caps the score at 2 when the main path fails, and at 3 when only a non-core case fails and the main path works. A passing `test_exit` is not a 4 or a 5 by itself.

Before a 4 or 5, try to disprove it: soft wording in the statement is still required when the intent is mandatory; examples in the statement that are unimplemented are misses; a test that asserts the wrong behavior is a miss, not extra credit.

### 5. Skim each trajectory for a real miss

Read each side's `logs\<side>.log` only far enough to answer one question: did the model do something plainly wrong while thinking or editing? Look for a repeated failed command, an edit to the wrong file, a completion claim the diff does not contain, or a loop on the same change. Skip duration and transport failures. Do not open the rollout JSONL for this.

The delivery description opens with that finding in one clause. If nothing like that happened, the clause is only what it actually did, such as 看了现有 client 后直接写接口. Do not invent a process complaint, and do not write a second paragraph about the trajectory.

### 6. Pick the GSB verdict

`GSB 结论` is exactly one of `A 更好`, `Same`, `B 更好`.

- If one side's delivery score is higher because it meets more of the statement, pick that side.
- Pick the lower-scoring side only when the higher score was surface completion that breaks an existing behavior the other side kept.
- Use process only as a tie-break when the delivery scores are equal and the product gaps do not already decide it.
- `Same` is only for a real tie. The reason must say which points are equivalent and which trade off against each other. A one-line Same is not allowed.

### 7. Write the result

After the eight fields are final, and before the answer, upsert this instance into one file next to `status.json` and `sheet.csv`:

`GSB\runs\_harness\evals.json`

One file holds every eval. Read it first. If it is missing, start from `{"updated_at": null, "evals": {}}`. Set `updated_at` to UTC ISO-8601. Write only `evals.<instance_id>`. A re-eval overwrites that key and leaves every other instance as it is. Do not write `sheet.csv` or `task.json`.

Scores are integers from 1 to 5. `GSB 结论` is exactly `A 更好`, `Same`, or `B 更好`. The strings are the same text as the eight fields, not a second rewrite. If a side could not be scored, leave that side's three fields null and still write the file. Do not invent a score.

```json
{
  "instance_id": "<instance_id>",
  "evaluated_at": "<UTC ISO-8601>",
  "A-测试结论": "<same text as the field>",
  "A-交付完整性（1-5）": 4,
  "A-交付完整性描述": "<same text as the field>",
  "B-测试结论": "<same text as the field>",
  "B-交付完整性（1-5）": 2,
  "B-交付完整性描述": "<same text as the field>",
  "GSB 结论": "A 更好",
  "GSB 理由": "<same text as the field>"
}
```

Do not mention this file after the eight fields.

The batch script downloads the diff, sends the prompt, deletes the diff, and writes this file. When the prompt says the caller saves the JSON, return one JSON object and do not write the file yourself.

## How the fields are written

Write names as plain text. Do not wrap file names, functions, commands, or errors in backticks, single quotes, or 「」. Do not paste a quoted error line.

Do not write harness. The test log is the run in the task environment after the edit. Call that 运行环境, for example 运行环境没加载到补丁. Do not call it 复现.

The party that changed the code is 模型 or AI. Do not name that party by the runner, the session, or a product name.

When the model said its own tests passed and the 运行环境 run did not, say that in plain words: 与模型宣称的全部通过不一致. Do not turn that gap into a slogan. A line that fails: 与 session 里宣称的全绿不一致.

`A-交付完整性（1-5）` and `B-交付完整性（1-5）` are a single integer, 1 through 5, with no other words.

Each 测试结论 is two or three sentences: the command, the exit code, what passed or which test failed, and whether that failure is the behavior the prompt asked for. Paraphrase the error. Do not paste the log.

Each 交付完整性描述 is one short paragraph about that side only. Do not list functions one by one. In this order:

1. `AI在思考中` plus the one-clause trajectory summary from step 5. If there was a bad step, name it here.
2. Which file or area changed, and what behavior it implemented, in one clause.
3. If the prompt's requirements are met: `题目要求的……基本实现。` Fill the blank with the actual requirement, not the word 题目要求.
4. If something required is missing or wrong: `但是` plus the concrete defect, then `导致` plus what breaks. One clause, not a line-by-line account.

A description that passes:

> AI在思考中看了现有 client 后直接写了接口。在 planet/clients/subscriptions.py 里补了创建和更新订阅等接口，题目要求的六个接口都未实现就抛错，基本实现。

A description that fails the voice, even when the facts are right:

> 模型在 utils/date.py 中基本实现了解析逻辑，但存在少量边界遗漏，整体仍有优化空间。

`GSB 理由` is one short paragraph. Do not repeat the command, the exit code, the suite totals, or unrelated failures; those stay in the 测试结论. If you mention a test, name only one that this change added or that asserts the behavior this change was supposed to fix. If neither side has such a test, do not mention tests.

Follow this order:

1. What both sides did. Then, only if there is a related or newly added test, one clause on how that test came out, including a gap when it does not actually hit the new behavior.
2. When one side is worse: the concrete defect and what it `导致`, then which of those points the better side actually has.
3. Close with `按题目要求的……，A实现的更好` (or B). The blank is the requirement you weighed, such as 接口能按现有 client 的方式被引用. When the verdict is `Same`, this sentence says the implementations match and which leftover differences pull in opposite directions without changing the behavior.

A reason that passes when one side is better:

> 两边都补了订阅客户端的六个接口。新加的测试没调用这些接口。B 的列表和结果接口不是异步生成器，类也没导出，导致异步遍历和导入都会失败。A 这两处都接上了。按题目要求的接口骨架能被引用，A实现的更好。

A reason that passes when the verdict is `Same`:

> 两边都在 mbe 里把连续标点按 rustc 粘成一组再匹配，crates/mbe 的修改相同。新加的标点粘连测试两边都过了，A 还多留了一条路径模式回归。按题目要求的路径模式不再误报，两边实现相同，A 多留的回归和 B 删掉展不开的复现方向相反，但是不影响功能实现。

Do not write 看不出差别.

Do not use section labels such as 产物：, 过程：, 权衡：, 综上所述, 总体来看. Do not write 表现一般, 基本可用, 有 bug, 实现不完整, 逻辑有问题, 结果不满意, 整体仍有优化空间, 少量边界遗漏. Do not mention a reference patch.

## Output

Do not put these fields in a code block. Each label is its own line, the value starts on the next line, and there is a blank line between fields. Other text may come first. End the answer with this shape and no other:

A-测试结论：
<命令、退出码、挂了什么或过了什么、和需求的关系>

A-交付完整性（1-5）：
<1-5>

A-交付完整性描述：
<证据>

B-测试结论：
<命令、退出码、挂了什么或过了什么、和需求的关系>

B-交付完整性（1-5）：
<1-5>

B-交付完整性描述：
<证据>

GSB 结论：
<A 更好 | Same | B 更好>

GSB 理由：
<两边都……。测试只提跟这次修改相关或新加的那条，没有就不写。有高下时写较差一侧导致什么、较好一侧接上了哪一点。按题目要求的……，X实现的更好；打平时写两边相同，以及方向相反但不影响功能的差别>
