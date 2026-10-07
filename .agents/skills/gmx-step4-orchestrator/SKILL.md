---
name: gmx-step4-orchestrator
description: Coordinate implementor and critic subagents through a caller-supplied GMX flow, with durable stage state and bounded review returns.
---

# GMX workflow orchestrator

Use this skill when the user asks the primary agent to run or resume an agentic GMX workflow. The caller supplies a flow document path and a status document path. This skill does not select a flow by itself or start agents merely by existing on disk.

1. Read the supplied flow and status files, their linked plan or design, and the role files specified by the flow. Check the working tree and code state before selecting the next open stage. Respect newer user instructions.
2. Spawn or reuse the implementor type assigned to that stage. Include its role instructions, the supplied flow path, stage, acceptance focus, repository state, and any critic findings. Wait for its handoff.
3. Spawn or reuse a separate critic of the type assigned to that stage. Include its role instructions, the same stage and acceptance focus, the implementor handoff, and changed files. The critic inspects the actual work and runs relevant checks independently.
4. If the critic returns `CHANGES_REQUESTED`, increment that stage's return count in the supplied status file and hand the findings back. Use the return limit stated in the supplied flow. Do not reset a stage's count silently. If the limit is exhausted and another review still has blocking findings, record the blocker and ask the human to decide.
5. If the critic returns `ACCEPTED`, record its evidence in the supplied status file and advance to the next stage. Keep each stage's implementation and review sequential. Report transitions to the user.
6. Apply the same review loop to integration. Mark the workflow complete only when its acceptance gates and required evidence pass. Record blockers and stop dependent work when evidence or a needed agent is unavailable.

The primary agent makes handoffs with the available subagent tools. Repository Markdown files are durable instructions and state, not executable agent registrations. GMX work remains read-only with respect to the chain and wallets.
