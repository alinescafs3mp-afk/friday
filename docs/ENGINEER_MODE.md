# Autonomous Engineer mode

Engineer mode is Friday's installation-owner workbench for autonomous technical
work on the primary VM. This document is the normative shipped contract.
Architecture references under `outer_sol/` do not silently change it; current
work and release state live only in `outer_sol/PROJECT_BACKLOG.md`.

## Product contract

With both `FRIDAY_ENGINEER_MODE_ENABLED=1` and
`FRIDAY_ENGINEER_COMMAND_ENABLED=1`, Friday may plan and execute any shell
command that the Friday service account could run from an ordinary terminal.
The model chooses the programs, arguments, sequence and stopping condition. The
runtime does not use command, executable, argument, path, network-destination or
file-operation allowlists and does not insert a `/approvals` step.

The retained authority boundary is the authenticated installation owner in the
current private Telegram chat. The process then has exactly the service user's
real operating-system permissions: no privilege escalation is added, but its
normal environment, `PATH`, filesystem, credentials and network are not hidden
from the command. If an executable is absent, Friday reports the exact program
or package it needs instead of pretending that the task is impossible.

This freedom is local to autonomous Engineer mode. Dialogue, knowledge-work and
research modes keep their existing authorization, privacy and side-effect
contracts. Legacy bounded Engineer scanners, compiler and decompiler remain
registered for compatibility when the autonomous command contour is disabled,
but they are not offered to the model in the autonomous contour: the universal
shell supersedes them.

## Admission

One `engineer_command_run` call is admitted only when all of these facts are
fresh and exact:

- the outer request came from Friday's Telegram bridge;
- the linked Telegram identity and private delivery chat belong to the active
  installation owner;
- the current user row belongs to the same conversation and original Telegram
  update;
- Engineer mode and its command runner are enabled;
- the current owner still has `engineer.use` and `engineer.command.run`.

Runtime injects the conversation, source-message, update and step identities.
The model cannot provide or replace them. Distinct tool calls from one message
receive distinct deterministic step identities; a replay of the same step is
idempotent. Old pending Engineer command approvals and their pending Telegram
buttons are made inert at startup. Direct API attempts and stale approval
callbacks cannot become a second command-entry surface.

## Execution

The model-facing start tool accepts:

- `command`: an arbitrary Bash command string;
- `timeout_sec`: an optional task deadline. Omitting it creates a job with no
  Friday wall-clock deadline; it runs until completion, explicit cancellation,
  an OS/service failure or an operator action.

The command is executed by held `/usr/bin/bash` as the Friday service user. It
may use shell syntax, pipelines, redirections, installed interpreters,
compilers, decompilers, network tools, package clients available to that user,
and binaries installed later by the operator. There is no synthetic command
classifier and no wrapper-selected executable catalogue.

Each job still receives a dedicated killable systemd cgroup. Its CPU, memory,
swap and task ceilings are derived from all resources actually available to the
service, not from the old small sandbox profile. These are lifecycle and
resource-containment mechanics, not semantic policy. Stdout/stderr collection,
Telegram upload size and generated-file inventory have finite transport bounds;
they do not restrict what the command may do elsewhere on the VM.

## Files and iterative work

The command environment publishes four absolute directories:

- `FRIDAY_INPUT_DIR` — immutable copies of files uploaded with the exact current
  Telegram message;
- `FRIDAY_WORK_DIR` — persistent private workbench for this owner and
  conversation, shared by later Engineer steps;
- `FRIDAY_JOB_DIR` — ephemeral directory and evidence for this one job;
- `FRIDAY_OUTPUT_DIR` — files to package and return to the owner's Telegram.

Current-message inputs are authorized, hashed, reauthorized immediately before
spawn and materialized without following paths. Ambient, quoted and old
conversation attachments do not silently enter a new command. The model is
expected to enumerate `FRIDAY_INPUT_DIR` instead of guessing filenames.

`FRIDAY_WORK_DIR` survives normal job cleanup, so Friday can inspect, modify,
compile and retest a project across several model/tool rounds. It is separated
by tenant, actor and conversation. Results intended for Telegram must be copied
below `FRIDAY_OUTPUT_DIR`; terminal delivery packages the verified tree with the
command receipt, stdout and stderr. Large working trees may stay in the
workbench while Friday returns a transport-sized archive or report.

## Autonomous loop and other tools

Engineer mode uses a short operational system prompt instead of the ordinary
response-policy prompt. Ordinary approval-free tools already authorized for the
owner remain available on equal terms, including web search, archive search,
Obsidian and file creation. Legacy approval-capable effect tools and the old
bounded Engineer wrappers are not offered: the unrestricted host-user shell is
the one effect lane, so an internal tool cannot quietly reintroduce
`/approvals` or steer execution back onto a narrower wrapper.

Friday should:

1. choose a useful first command or another appropriate tool;
2. inspect real stdout, stderr, exit status and generated files;
3. choose the next step from that evidence;
4. continue until the requested outcome is verified or an actual external
   blocker is identified;
5. state the exact missing executable/package when operator help is required.

The runtime permits multiple dependent command calls in one turn. A short job
returns its terminal receipt directly to the next model round. A longer job
returns a `job_id`; `engineer_command_status` and
`engineer_command_cancel` operate on the current conversation's owned job.
Fact-only progress notifications are durable, and terminal artifact delivery
does not require another model turn.

The overall request clock and model/tool-round budgets remain finite so a
broken model or connector cannot occupy the Telegram request forever. They do
not terminate an already admitted unbounded command job, which continues in
the background and is published by the worker when terminal.

## Evidence and delivery

Terminal receipts record the command/request binding, owner source, isolation
profile, exit status, signal, timing, stdout/stderr hashes, truncation flags and
generated-file inventory. A successful process exit is not rewritten into a
semantic success claim: the model must check whether the user's actual outcome
exists. Unknown cleanup or restart state remains `unknown`, never success.

Publication rechecks the owner, Telegram route and exact job identity. It sends
at most one deterministic archive through a durable delivery fence; ambiguous
Telegram delivery is not blindly repeated. After the configured retention
period, proven-delivered per-job evidence may be retired while receipts,
publication identity, hashes and the persistent conversation workbench remain.

## Operator use

1. Enable Engineer and its command runner for the immutable release.
2. In the owner's private Telegram chat, switch to Engineer mode with
   `/engineer`.
3. Describe the outcome naturally, for example: "проверь мою подсеть и пришли
   отчёт", "разбери этот бинарник, исправь функцию, собери и пришли архив" or
   "скомпилируй проект и отдай готовый бинарник вместе с исходниками".
4. Attach source files in that same message when they are inputs. Friday sees
   them in `FRIDAY_INPUT_DIR` and returns deliverables from
   `FRIDAY_OUTPUT_DIR`.
5. For long work, ask for status or cancellation; Friday resolves the current
   conversation job without requiring an approval ID.
6. If Friday names a missing package, install it for the VM/service account and
   repeat or continue the task. No tool-registry edit is required.

## Release acceptance

An autonomous Engineer candidate must prove, without Docker certification:

1. non-owner, non-Telegram, stale-source and cross-conversation execution are
   denied before spawn;
2. owner execution creates no `action_approvals` row and an old Engineer
   approval cannot execute;
3. arbitrary Bash composition, an executable discovered through the real
   `PATH`, host filesystem access and network access work as the service user;
4. an exact current upload can be transformed and returned from
   `FRIDAY_OUTPUT_DIR`;
5. a second command in the same conversation can read and modify the first
   command's `FRIDAY_WORK_DIR`, while another actor/tenant/conversation cannot;
6. a long command returns a job, reports real progress, can be cancelled and
   publishes its terminal receipt/artifacts exactly once;
7. web search, Obsidian and ordinary file tools remain callable in Engineer
   mode; legacy response guards do not replace a valid tool result;
8. focused command/runtime/delivery tests, Ruff, mypy and the compact native
   release gate pass on the exact candidate.

The companion plugin and Docker contour are outside this rollout.
