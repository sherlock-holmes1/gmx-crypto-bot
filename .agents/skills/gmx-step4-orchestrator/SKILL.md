---
name: gmx-step4-orchestrator
description: Coordinate the developer and tester subagents through the GMX Step 4 simulator implementation pipeline, including bounded review returns and human escalation.
---

# GMX Step 4 orchestrator

Use this skill when the user asks the primary agent to run or resume the Step 4 implementation pipeline. This file guides the primary agent; it does not start agents merely by existing on disk.

1. Read `docs/step4-agentic-flow.md`, its linked Step 4 design, `.agents/agents/developer.md`, `.agents/agents/tester.md`, and `docs/step4-agentic-status.md`. Check the working tree and code state before selecting the next open stage. Respect newer user instructions.
2. Spawn or reuse a `developer` subagent. Include the developer role file's instructions, the selected stage, acceptance focus, current repository state, and any tester findings in its task. Wait for the developer's handoff before review.
3. Spawn or reuse a separate `tester` subagent. Include the tester role file's instructions, the same stage and acceptance focus, the developer handoff, and the changed files. The tester inspects the actual diff and runs relevant checks independently.
4. If the tester returns `CHANGES_REQUESTED`, increment that stage's return count in `docs/step4-agentic-status.md`, then hand the findings back to the developer. Allow at most three tester-to-developer returns for a stage. After the third revision, obtain one more tester review; if it still has blocking findings, stop that stage and ask the human to decide. Never silently reset a return count within a stage.
5. If the tester returns `ACCEPTED`, record the acceptance and evidence in the status file, reset the counter for the next stage, and give the developer the next open stage. Keep implementation and review sequential. Report stage transitions to the user.
6. Apply the same review loop to final integration. Mark Step 4 complete only when its design acceptance gates and relevant regression checks pass. If a subagent or needed evidence is unavailable, record the blocker and stop dependent work.

The primary agent makes the handoffs with the available subagent tools. Repository Markdown files are durable instructions and state, not executable agent registrations. The workflow remains offline and read-only with respect to GMX and wallets.
