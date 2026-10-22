# Concurrent generation

The TUI now uses a persistent SQLite queue with isolated worker processes. Defaults
are **two active jobs, one render, and one GPU Whisper stage**. Job A can render
while job B shapes a script, synthesizes narration, or downloads footage. A worker
requests a permit before each stage; it waits in its own process without holding
a render or GPU slot. The existing compiler, codec, preset, thread count, caption
behavior, and atomic output publication remain in place.

## Queue controls

Open Queue with **Ctrl+7**. Arrow keys select a job; Enter opens its monitor and
independent log. Ctrl+G adds the Create form as a new job during active work.
Ctrl+X cancels only the selected job; Ctrl+R retries or recovers it. Alt+Up and
Alt+Down reorder queued jobs. Buttons expose the same actions through Tab/Enter.
Bulk add accepts one topic per line and uses Create settings with uploads off.
The concurrency controls change separate active-job, render, and GPU limits.
Reducing a limit affects future admissions; healthy work is allowed to finish.

Waiting messages reflect actual admission decisions: active job slot, render
slot, GPU resource, upload slot, or available memory. The interface reports stages,
status and waiting reasons, without invented progress percentages. Upload stages
are serialized to protect the shared OAuth token file.

## Persistence and recovery

Default state lives at `$XDG_STATE_HOME/slopengine`, or
`~/.local/state/slopengine`: `jobs.sqlite3`, a queue ownership lock, and `logs/`.
SQLite uses WAL and FULL synchronous commits. One application owns each queue;
a second application cannot schedule from the same database. Manager methods
serialize state changes and reserve resources before starting or permitting work.
The application polls the manager every 200 ms; it must remain open to advance
new stages. This is not a background service.

Job states are QUEUED, RUNNING, WAITING, CANCELLING, COMPLETED, FAILED, CANCELLED,
INTERRUPTED and HELD. Stages have PENDING, WAITING, RUNNING, COMPLETED, FAILED,
CANCELLED and INTERRUPTED states. Job IDs are persistent UUIDs; queued priority
is stable, and reordering affects queued jobs only.

On shutdown, running work is stopped and recorded as INTERRUPTED. After restart,
pending work is HELD until an explicit retry/recovery action. Interrupted work
never resumes automatically, even if its final MP4 was already published. Inspect
its output before retrying. Recovery creates a new job ID and output, preserves
the original record, and disables uploads. It regenerates from the beginning;
there is no checkpoint replay or automatic retry of provider failures.

Configuration snapshots include all non-secret pipeline settings and resolved
paths. Credentials are kept in memory and sent through the private worker input
pipe, never saved in SQLite or logs. Recovered jobs resolve current credentials
at explicit retry; other settings come from their original snapshot. Pending
jobs in the same session keep their original credential values. Logs are
redacted JSON lines and remain independent for each worker.

## Isolation and cleanup

Each worker starts a new POSIX session and gets a private job-specific temporary
root under the configured temp directory, a unique destination, and hidden
job-specific output staging. SQLite reserves destinations; filesystem publication
also refuses existing files. A completion message is accepted only after a
successful process exit and nonempty MP4 plus metadata publication. Upload
failure can leave a valid local MP4 with a FAILED job; inspect it before retrying.

Cancellation signals only that worker's process group, escalates after four
seconds, and cleans its temporary/staged media. A worker also detects closure of
its manager control pipe and cleans up its descendants. Recovery checks stored
PID creation times and inherited job identity before removing orphan groups,
then cleans abandoned staging. Published outputs are retained. The existing
`keep_temp` option retains temporary assets for successfully completed jobs;
cancellation and recovery still clean abandoned temporary media. Job and log
history are not automatically deleted.

## Memory admission

Admission uses `psutil.virtual_memory().available`, process-tree RSS, outstanding
reservations, and configurable system headroom. Available RAM already excludes
resident allocations, so only unconsumed reservations and incremental stage demand
are charged. Admission and permit delivery are serialized to avoid two decisions
spending the same headroom. Defaults reserve **3 GiB for the system** and these
process-tree estimates:

| Stage | Reservation |
| --- | ---: |
| Startup, script, narration, footage, save, upload | 1.5 GiB |
| CPU/GPU word timestamps, base model | 3 GiB |
| Render at 1080×1920 | 7 GiB |

The render estimate includes margin above the measured 5.99 GiB production peak.
Other estimates are conservative starting assumptions, not measured guarantees.
Larger Whisper models reserve more host RAM (small 4, medium/turbo 7, large 12
GiB, including English-only and named aliases); unknown local models reserve 12
GiB. Higher output resolutions scale render admission upward. Completed stages
release scarce slots and retain a reservation for their measured resident memory.
Memory-blocked requests prevent newer expanding admissions from jumping ahead;
reservation-reducing transitions can drain and finish to avoid deadlock.

Resource estimates and headroom are configurable through `ResourceLimits` or
`JobManager.configure`; configuration persists in SQLite. For example, while the
TUI is closed, use the project's Python environment:

```python
from slopengine.jobs import JobManager
manager = JobManager()
try:
    manager.configure(system_headroom_gib=4.0)
    estimates = dict(manager.limits.stage_memory_gib)
    estimates["render"] = 8.0
    manager.configure(stage_memory_gib=estimates)
finally:
    manager.close()
```

Scheduling delays new allocations under memory pressure; it does not evict healthy
jobs or use swap as capacity. These are admission controls, not hard memory
limits: an unusually long narration, many source clips, third-party allocations,
GPU VRAM exhaustion, or unrelated applications can exceed estimates after a
permit. Increase estimates for heavier workloads. No VRAM accounting or cgroup
memory enforcement is implemented. Stop/cancel explicitly if a running job is
unhealthy; the scheduler never kills it to admit another one.

## Compatibility and validation

`shorts_core.py` remains the synchronous legacy CLI and uses the same pipeline.
Its optional stage gate defaults to absent. Separate legacy CLI invocations do
not participate in the TUI queue or share its resource limits. Do not launch
unmanaged generators alongside a controlled production workload or benchmark.
POSIX process groups and file locking are required for the concurrent manager.
External services retain their own quotas/timeouts; no global provider rate
limiter is introduced. CPU Whisper remains the code default.

Tests use independent lightweight OS workers and mocked providers to check
concurrency, slot handoff, memory decisions, event routing, cancellation, crash
isolation, output publication, queue ownership, recovery and keyboard navigation.
They establish scheduler behavior, not real rendering memory safety or throughput.
See [paired benchmark instructions](paired-render-benchmark.md) for the explicit
cached-real-asset comparison. Production render concurrency remains one until
successful representative measurements establish enough headroom, stability and
better total throughput. No new full-length paired benchmark has been executed
for this milestone.

## Milestone verification

On 2026-10-21, the complete suite passed **103 tests** (the 72-test baseline plus
31 new tests). `python3 -m py_compile shorts_core.py` and `git diff --check` passed.
The existing offline rendering validator compared all 120 frames and 176,400
audio samples with the saved pre-optimization compiler: pixels and audio were
exactly equal, including empty-caption/fallback checks. The generated caption
preview was inspected. The comparison report is
[concurrent-generation-validation.json](benchmarks/concurrent-generation-validation.json).
No live Gemini, TTS, Whisper inference, Pexels or upload calls were made for this
milestone. No new full-length production timing or paired-render safety result
is claimed; the historical 198.27-second render remains the supplied reference.
