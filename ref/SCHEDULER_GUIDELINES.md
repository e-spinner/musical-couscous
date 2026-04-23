# Scheduler Guidelines

This document defines how the scheduler should interpret task data and make scheduling decisions. It starts with the task fields because every scheduling rule depends on those inputs being clearly defined.

## Task Parameters

Each task is treated as a schedulable unit of work. The current task model includes the following parameters.

### `id`

- Purpose: uniquely identifies a task across the app.
- Type: string.
- Source: generated on the frontend when a task is created.
- Scheduler use: used to match task records with previously scheduled segments and preserve schedule history across re-optimization.
- Rule: must stay stable for the life of the task.

### `title`

- Purpose: human-readable name of the task.
- Type: string.
- Source: entered by the user.
- Scheduler use: does not change scheduling priority directly, but is returned in schedule results and used as a final tie-breaker when tasks otherwise sort the same.
- Rule: should be non-empty after trimming whitespace.

### `estimateMinutes`

- Purpose: the total amount of work time still required for the task.
- Type: integer number of minutes.
- Source: entered by the user and later reduced as scheduled work is treated as already allocated.
- Scheduler use: determines how much time must be placed into available blocks.
- Rules:
- must be greater than `0` to be schedulable.
- values under `60` mean the whole task may fit in a shorter block.
- values of `60` or more are subject to the minimum `60-minute` work block rule.
- when re-optimizing, the frontend subtracts already-fixed past segments before sending the remaining estimate to the backend.

### `dueDate`

- Purpose: the date by which the task should be completed.
- Type: ISO date string in `YYYY-MM-DD` format.
- Source: entered by the user.
- Scheduler use: primary ordering signal for task selection. Earlier due dates are scheduled first.
- Rules:
- interpreted as starting at `00:01` on that date in the frontend logic.
- used for sorting and for dashboard/task display.
- should always be a valid calendar date.

### `status`

- Purpose: tracks the workflow state of the task.
- Type: enum string.
- Allowed values:
- `new`
- `in_progress`
- `completed`
- Source: entered or updated by the user, and sometimes adjusted by the dashboard review flow.
- Scheduler use: determines whether the task is eligible to be scheduled.
- Rules:
- tasks marked `completed` are excluded from future scheduling.
- `new` and `in_progress` tasks are eligible for scheduling.
- completed tasks may later be pruned from storage once their due date threshold has passed.

### `priority`

- Purpose: describes the user-assigned urgency level of the task.
- Type: enum string.
- Allowed values:
- `high`
- `medium`
- `low`
- Source: entered by the user.
- Scheduler use: currently stored and returned with schedule data, but not yet used as an active sort factor in the backend algorithm.
- Rule: should still be preserved in the task model because it is part of the intended planner behavior and UI.

### `cognitiveLoad`

- Purpose: describes how mentally demanding the task is expected to be.
- Type: enum string.
- Allowed values:
- `high`
- `medium`
- `low`
- Source: entered by the user.
- Scheduler use: currently stored and returned with schedule data, but not yet used as an active placement rule in the backend algorithm.
- Rule: should be preserved for future scheduling heuristics, reporting, and UI decisions.

### `notes`

- Purpose: free-form supporting context for the task.
- Type: string.
- Source: entered by the user.
- Scheduler use: not currently used by the backend scheduler.
- Rule: kept in local task storage and review flows, but not sent as part of the optimizer request payload.

## Which Task Parameters Affect Scheduling Today

The current backend scheduler directly depends on these task parameters:

- `id`
- `title`
- `estimateMinutes`
- `dueDate`
- `priority`
- `cognitiveLoad`

The current frontend scheduling pipeline also depends on:

- `status`, to exclude completed tasks before they are sent to the backend

The current system stores but does not directly schedule against:

- `notes`

## Current Interpretation Summary

- A task must have a stable `id`, a non-empty `title`, a positive `estimateMinutes`, and a valid `dueDate`.
- A task with `status = completed` is not sent for future scheduling.
- The scheduler currently orders tasks mainly by earliest `dueDate`, then by smaller `estimateMinutes`, then alphabetically by `title`.
- `priority` and `cognitiveLoad` are part of the task contract now, even though the backend does not yet use them to change order or placement.

## Next Sections To Expand

This file can be extended next with:

- availability parameter definitions
- scheduler ordering rules
- block allocation rules
- re-optimization and history preservation rules
- unscheduled task behavior
- edge cases and validation rules
