from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from flask import Flask, jsonify, request
from flask_cors import CORS


app = Flask(__name__)
CORS(app)

SCHEDULING_STEP_MINUTES = 15
MINIMUM_WORK_BLOCK_MINUTES = 60
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
STATUS_RANK = {"in_progress": 0, "new": 1, "completed": 2}
COGNITIVE_LOAD_CAP_MINUTES = {
    "high": 90,
    "medium": 120,
    "low": 180,
}
SAME_TASK_RECOVERY_MINUTES = {
    "high": 180,
    "medium": 90,
    "low": 0,
}
DIFFERENT_TASK_RECOVERY_MINUTES = {
    "high": 120,
    "medium": 0,
    "low": 0,
}
MAX_PLANS_PER_TASK = 8
EMERGENCY_OVERLOAD_DUE_DAYS = 2


@dataclass(frozen=True)
class TimeBlock:
    start: datetime
    end: datetime

    @property
    def duration_minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))


@dataclass(frozen=True)
class Segment:
    task_id: str
    title: str
    cognitive_load: str
    start: datetime
    end: datetime
    block_start: datetime
    block_end: datetime

    @property
    def allocated_minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    estimate_minutes: int
    due_date: date
    priority: str
    cognitive_load: str
    status: str

    @property
    def priority_rank(self) -> int:
        return PRIORITY_RANK.get(self.priority, PRIORITY_RANK["medium"])

    @property
    def status_rank(self) -> int:
        return STATUS_RANK.get(self.status, STATUS_RANK["new"])

    @property
    def cognitive_cap_minutes(self) -> int:
        return COGNITIVE_LOAD_CAP_MINUTES.get(
            self.cognitive_load,
            COGNITIVE_LOAD_CAP_MINUTES["medium"],
        )

    def due_cutoff_for(self, reference: datetime) -> datetime:
        if reference.tzinfo is not None:
            return datetime.combine(self.due_date, time.min, tzinfo=reference.tzinfo) + timedelta(minutes=1)
        return datetime.combine(self.due_date, time.min) + timedelta(minutes=1)

    def sort_score(self, today: date) -> tuple:
        days_until_due = (self.due_date - today).days
        if days_until_due < 4:
            return (
                0,
                self.due_date,
                self.priority_rank,
                self.status_rank,
                self.estimate_minutes,
                self.title.lower(),
            )

        return (
            1,
            self.priority_rank,
            self.due_date,
            self.status_rank,
            self.estimate_minutes,
            self.title.lower(),
        )

    def can_use_emergency_overload(self, today: date) -> bool:
        return (self.due_date - today).days <= EMERGENCY_OVERLOAD_DUE_DAYS


def parse_time_block(raw_block: dict) -> TimeBlock:
    start = datetime.fromisoformat(raw_block["start"])
    end = datetime.fromisoformat(raw_block["end"])
    return TimeBlock(start=start, end=end)


def parse_task(raw_task: dict) -> Task:
    return Task(
        id=str(raw_task["id"]),
        title=raw_task["title"].strip(),
        estimate_minutes=int(raw_task["estimateMinutes"]),
        due_date=date.fromisoformat(raw_task["dueDate"]),
        priority=raw_task.get("priority", "medium"),
        cognitive_load=raw_task.get("cognitiveLoad", "medium"),
        status=raw_task.get("status", "new"),
    )


def is_step_aligned(moment: datetime) -> bool:
    return moment.second == 0 and moment.microsecond == 0 and moment.minute % SCHEDULING_STEP_MINUTES == 0


def can_partition_minutes(total_minutes: int, cap_minutes: int) -> bool:
    if total_minutes == 0:
        return True
    if total_minutes < MINIMUM_WORK_BLOCK_MINUTES:
        return False
    if total_minutes % SCHEDULING_STEP_MINUTES != 0:
        return False

    min_segments = max(1, math.ceil(total_minutes / cap_minutes))
    max_segments = total_minutes // MINIMUM_WORK_BLOCK_MINUTES
    return min_segments <= max_segments


def subtract_segments_from_blocks(
    time_blocks: list[TimeBlock],
    segments: list[Segment],
) -> list[TimeBlock]:
    ordered_segments = sorted(segments, key=lambda segment: (segment.start, segment.end))
    free_blocks: list[TimeBlock] = []

    for block in sorted(time_blocks, key=lambda item: item.start):
        parts = [block]
        for segment in ordered_segments:
            next_parts: list[TimeBlock] = []
            for part in parts:
                if segment.end <= part.start or segment.start >= part.end:
                    next_parts.append(part)
                    continue
                if segment.start > part.start:
                    next_parts.append(TimeBlock(start=part.start, end=segment.start))
                if segment.end < part.end:
                    next_parts.append(TimeBlock(start=segment.end, end=part.end))
            parts = next_parts

        free_blocks.extend(part for part in parts if part.duration_minutes >= MINIMUM_WORK_BLOCK_MINUTES)

    return sorted(free_blocks, key=lambda block: block.start)


def build_segment_length_candidates(remaining_minutes: int, cap_minutes: int) -> list[int]:
    if remaining_minutes < MINIMUM_WORK_BLOCK_MINUTES:
        return []

    minimum_segments = max(1, math.ceil(remaining_minutes / cap_minutes))
    target_minutes = remaining_minutes / minimum_segments
    candidates = []

    for length in range(cap_minutes, MINIMUM_WORK_BLOCK_MINUTES - 1, -SCHEDULING_STEP_MINUTES):
        next_remaining = remaining_minutes - length
        if next_remaining < 0:
            continue
        if next_remaining and not can_partition_minutes(next_remaining, cap_minutes):
            continue
        candidates.append(length)

    return sorted(
        candidates,
        key=lambda length: (abs(length - target_minutes), -length),
    )


def build_overload_length_candidates(remaining_minutes: int, cap_minutes: int) -> list[int]:
    if remaining_minutes < MINIMUM_WORK_BLOCK_MINUTES or remaining_minutes % SCHEDULING_STEP_MINUTES != 0:
        return []

    candidates = []
    for length in range(remaining_minutes, cap_minutes - 1, -SCHEDULING_STEP_MINUTES):
        if length < MINIMUM_WORK_BLOCK_MINUTES:
            continue
        next_remaining = remaining_minutes - length
        if next_remaining and not can_partition_minutes(next_remaining, cap_minutes):
            continue
        candidates.append(length)
    return candidates


def recovery_gap_minutes(task: Task, other_segment: Segment) -> int:
    if task.cognitive_load != other_segment.cognitive_load:
        return 0
    if task.cognitive_load == "high":
        if task.id == other_segment.task_id:
            return SAME_TASK_RECOVERY_MINUTES["high"]
        return DIFFERENT_TASK_RECOVERY_MINUTES["high"]
    if task.cognitive_load == "medium" and task.id == other_segment.task_id:
        return SAME_TASK_RECOVERY_MINUTES["medium"]
    return 0


def violates_recovery_gap(task: Task, start: datetime, end: datetime, segments: list[Segment]) -> bool:
    for other in segments:
        required_gap = recovery_gap_minutes(task, other)
        if required_gap == 0:
            continue

        gap = timedelta(minutes=required_gap)
        if end <= other.start:
            if other.start - end < gap:
                return True
            continue
        if start >= other.end:
            if start - other.end < gap:
                return True
            continue
        return True

    return False


def to_segment_dict(segment: Segment) -> dict:
    return {
        "blockStart": segment.block_start.isoformat(),
        "blockEnd": segment.block_end.isoformat(),
        "start": segment.start.isoformat(),
        "end": segment.end.isoformat(),
        "allocatedMinutes": segment.allocated_minutes,
    }


def plan_fragmentation_penalty(segments: list[Segment]) -> int:
    if len(segments) <= 1:
        return 0
    lengths = [segment.allocated_minutes for segment in segments]
    return (max(lengths) - min(lengths)) + (len(segments) - 1) * 15


def plan_uses_emergency_overload(task: Task, segments: list[Segment]) -> bool:
    return any(segment.allocated_minutes > task.cognitive_cap_minutes for segment in segments)


def sort_plan_key(plan: list[Segment]) -> tuple:
    ordered_plan = sorted(plan, key=lambda segment: segment.start)
    return (
        len(ordered_plan),
        plan_fragmentation_penalty(ordered_plan),
        ordered_plan[0].start,
    )


def generate_task_plans(
    task: Task,
    time_blocks: list[TimeBlock],
    committed_segments: list[Segment],
    *,
    today: date,
) -> list[list[Segment]]:
    if not time_blocks:
        return []

    due_cutoff = task.due_cutoff_for(time_blocks[0].start)
    eligible_blocks = []
    for block in time_blocks:
        if block.start >= due_cutoff:
            continue
        eligible_blocks.append(
            TimeBlock(
                start=block.start,
                end=min(block.end, due_cutoff),
            )
        )

    eligible_blocks = [block for block in eligible_blocks if block.duration_minutes >= MINIMUM_WORK_BLOCK_MINUTES]
    if not eligible_blocks or not can_partition_minutes(task.estimate_minutes, task.cognitive_cap_minutes):
        return []

    plans: dict[tuple[tuple[str, str], ...], list[Segment]] = {}

    def search(remaining_minutes: int, chosen_segments: list[Segment], *, allow_overload: bool) -> None:
        if len(plans) >= MAX_PLANS_PER_TASK:
            return
        if remaining_minutes == 0:
            ordered = sorted(chosen_segments, key=lambda segment: segment.start)
            key = tuple((segment.start.isoformat(), segment.end.isoformat()) for segment in ordered)
            plans[key] = ordered
            return

        cap_minutes = task.cognitive_cap_minutes
        free_blocks = subtract_segments_from_blocks(eligible_blocks, committed_segments + chosen_segments)
        length_candidates = build_segment_length_candidates(remaining_minutes, cap_minutes)
        if allow_overload:
            for overload_length in build_overload_length_candidates(remaining_minutes, cap_minutes):
              if overload_length not in length_candidates:
                  length_candidates.append(overload_length)

        for free_block in free_blocks:
            for length in length_candidates:
                if free_block.duration_minutes < length:
                    continue

                latest_start = free_block.end - timedelta(minutes=length)
                start = free_block.start
                while start <= latest_start:
                    end = start + timedelta(minutes=length)
                    if end > due_cutoff:
                        break
                    if violates_recovery_gap(task, start, end, committed_segments + chosen_segments):
                        start += timedelta(minutes=SCHEDULING_STEP_MINUTES)
                        continue

                    segment = Segment(
                        task_id=task.id,
                        title=task.title,
                        cognitive_load=task.cognitive_load,
                        start=start,
                        end=end,
                        block_start=free_block.start,
                        block_end=free_block.end,
                    )
                    search(remaining_minutes - length, chosen_segments + [segment], allow_overload=allow_overload)
                    if len(plans) >= MAX_PLANS_PER_TASK:
                        return
                    start += timedelta(minutes=SCHEDULING_STEP_MINUTES)

    search(task.estimate_minutes, [], allow_overload=False)
    if not plans and task.can_use_emergency_overload(today):
        search(task.estimate_minutes, [], allow_overload=True)
    return sorted(plans.values(), key=sort_plan_key)


def build_unscheduled_reason(
    task: Task,
    time_blocks: list[TimeBlock],
    committed_segments: list[Segment],
    *,
    today: date,
    feasible_plans: list[list[Segment]] | None = None,
) -> dict:
    reference = time_blocks[0].start if time_blocks else datetime.combine(task.due_date, time.min)
    due_cutoff = task.due_cutoff_for(reference)

    pre_deadline_blocks = []
    for block in time_blocks:
        if block.start >= due_cutoff:
            continue
        clipped_end = min(block.end, due_cutoff)
        if clipped_end <= block.start:
            continue
        pre_deadline_blocks.append(TimeBlock(start=block.start, end=clipped_end))

    total_pre_deadline_minutes = sum(block.duration_minutes for block in pre_deadline_blocks)
    eligible_blocks = [
        block for block in pre_deadline_blocks if block.duration_minutes >= MINIMUM_WORK_BLOCK_MINUTES
    ]
    free_blocks = subtract_segments_from_blocks(eligible_blocks, committed_segments) if eligible_blocks else []
    total_free_minutes = sum(block.duration_minutes for block in free_blocks)

    details = {
        "dueCutoff": due_cutoff.isoformat(),
        "totalPreDeadlineMinutes": total_pre_deadline_minutes,
        "freePreDeadlineMinutes": total_free_minutes,
        "minimumBlockMinutes": MINIMUM_WORK_BLOCK_MINUTES,
        "stepMinutes": SCHEDULING_STEP_MINUTES,
        "cognitiveCapMinutes": task.cognitive_cap_minutes,
        "committedSegmentCount": len(committed_segments),
    }

    if task.estimate_minutes < MINIMUM_WORK_BLOCK_MINUTES:
        return {
            "code": "estimate_below_minimum_block",
            "message": "The estimate is shorter than the minimum schedulable block.",
            "details": details,
        }

    if task.estimate_minutes % SCHEDULING_STEP_MINUTES != 0:
        return {
            "code": "estimate_not_step_aligned",
            "message": "The estimate does not align to the scheduler's 15-minute increments.",
            "details": details,
        }

    if not can_partition_minutes(task.estimate_minutes, task.cognitive_cap_minutes):
        return {
            "code": "partition_rules_conflict",
            "message": "The estimate cannot be split into valid work blocks under the current cognitive-load cap.",
            "details": details,
        }

    if not pre_deadline_blocks or total_pre_deadline_minutes == 0:
        return {
            "code": "deadline_conflict",
            "message": "There is no availability before the task's due-date cutoff.",
            "details": details,
        }

    if not eligible_blocks:
        return {
            "code": "subhour_windows_only",
            "message": "There is time before the deadline, but every available window is shorter than 60 minutes.",
            "details": details,
        }

    if total_pre_deadline_minutes < task.estimate_minutes:
        return {
            "code": "not_enough_predeadline_time",
            "message": "There are not enough total minutes available before the deadline.",
            "details": details,
        }

    if feasible_plans:
        return {
            "code": "higher_value_tasks_preferred",
            "message": "This task had valid slots, but the optimizer chose other tasks with a better overall priority and deadline fit.",
            "details": {
                **details,
                "feasiblePlanCount": len(feasible_plans),
            },
        }

    if not free_blocks:
        return {
            "code": "time_taken_by_other_tasks",
            "message": "Other scheduled work consumed the viable pre-deadline windows for this task.",
            "details": details,
        }

    if total_free_minutes < task.estimate_minutes:
        return {
            "code": "not_enough_remaining_time",
            "message": "Enough time existed before the deadline overall, but not enough remained after other scheduled work was placed.",
            "details": details,
        }

    return {
        "code": "fragmentation_or_recovery_gap",
        "message": "Open time exists before the deadline, but it cannot be arranged into a valid plan because of block-size, fragmentation, or recovery-gap rules.",
        "details": details,
    }


def build_task_payload(task: Task, segments: list[Segment]) -> dict:
    allocated_minutes = sum(segment.allocated_minutes for segment in segments)
    missing_minutes = max(0, task.estimate_minutes - allocated_minutes)
    payload = {
        "id": task.id,
        "title": task.title,
        "estimateMinutes": task.estimate_minutes,
        "dueDate": task.due_date.isoformat(),
        "priority": task.priority,
        "cognitiveLoad": task.cognitive_load,
        "status": task.status,
        "completionStatus": "complete" if missing_minutes == 0 else "incomplete",
        "segments": [to_segment_dict(segment) for segment in segments],
    }
    if missing_minutes:
        payload["missingMinutes"] = missing_minutes
    return payload


def build_incomplete_payload(task: Task, reason: dict | None = None) -> dict:
    payload = {
        "id": task.id,
        "title": task.title,
        "estimateMinutes": task.estimate_minutes,
        "dueDate": task.due_date.isoformat(),
        "priority": task.priority,
        "cognitiveLoad": task.cognitive_load,
        "status": task.status,
        "completionStatus": "incomplete",
        "missingMinutes": task.estimate_minutes,
    }
    if reason:
        payload["unscheduledReasonCode"] = reason["code"]
        payload["unscheduledReason"] = reason["message"]
        payload["unscheduledDetails"] = reason["details"]
    return payload


def task_score(task: Task, segment_count: int, fragmentation_penalty: int, *, today: date) -> int:
    days_until_due = (task.due_date - today).days
    priority_value = 3 - task.priority_rank
    status_value = 1 if task.status == "in_progress" else 0

    if days_until_due < 4:
        primary_weight = max(0, 30 - max(days_until_due, 0)) * 2_500_000
        secondary_weight = priority_value * 750_000
    else:
        primary_weight = priority_value * 2_000_000
        secondary_weight = max(0, 30 - max(days_until_due, 0)) * 250_000

    return (
        primary_weight
        + secondary_weight
        + status_value * 150_000
        + task.estimate_minutes * 250
        + priority_value * task.estimate_minutes * 100
        - segment_count * 2_000
        - fragmentation_penalty * 20
    )


def plan_score(task: Task, plan: list[Segment], *, today: date) -> tuple[int, bool]:
    emergency_overload_used = plan_uses_emergency_overload(task, plan)
    score = task_score(
        task,
        len(plan),
        plan_fragmentation_penalty(plan),
        today=today,
    )
    if emergency_overload_used:
        score -= 25_000
    return score, emergency_overload_used


def committed_state_key(segments: list[Segment]) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (
                segment.cognitive_load,
                segment.start.isoformat(),
                segment.end.isoformat(),
            )
            for segment in segments
        )
    )


def optimize_schedule(
    ordered_tasks: list[Task],
    time_blocks: list[TimeBlock],
    *,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    root_plans_by_task = {
        task.id: generate_task_plans(task, time_blocks, [], today=now.date())
        for task in ordered_tasks
    }
    ordered_tasks = sorted(
        ordered_tasks,
        key=lambda task: (
            len(root_plans_by_task[task.id]),
            task.sort_score(now.date()),
        ),
    )
    best_result: dict[str, object] = {
        "score": float("-inf"),
        "scheduled": [],
        "unscheduled": [],
    }
    optimistic_scores = [
        max(
            0,
            max(
                (plan_score(task, plan, today=now.date())[0] for plan in root_plans_by_task[task.id]),
                default=0,
            ),
        )
        for task in ordered_tasks
    ]
    suffix_upper_bounds = [0] * (len(ordered_tasks) + 1)
    for index in range(len(ordered_tasks) - 1, -1, -1):
        suffix_upper_bounds[index] = suffix_upper_bounds[index + 1] + optimistic_scores[index]
    best_seen_scores: dict[tuple[int, tuple[tuple[str, str, str], ...]], int] = {}

    def search(
        index: int,
        committed_segments: list[Segment],
        scheduled_payloads: list[dict],
        unscheduled_payloads: list[dict],
        current_score: int,
    ) -> None:
        if current_score + suffix_upper_bounds[index] < best_result["score"]:
            return
        state_key = (index, committed_state_key(committed_segments))
        previous_best = best_seen_scores.get(state_key)
        if previous_best is not None and previous_best >= current_score:
            return
        best_seen_scores[state_key] = current_score

        if index >= len(ordered_tasks):
            candidate = (
                current_score,
                len(scheduled_payloads),
                -len(unscheduled_payloads),
            )
            best_candidate = (
                best_result["score"],
                len(best_result["scheduled"]),
                -len(best_result["unscheduled"]),
            )
            if candidate > best_candidate:
                best_result["score"] = current_score
                best_result["scheduled"] = list(scheduled_payloads)
                best_result["unscheduled"] = list(unscheduled_payloads)
            return

        task = ordered_tasks[index]
        plans = generate_task_plans(task, time_blocks, committed_segments, today=now.date())
        ranked_plans = sorted(
            (
                (
                    *plan_score(task, plan, today=now.date()),
                    plan_fragmentation_penalty(plan),
                    plan,
                )
                for plan in plans
            ),
            key=lambda item: (
                -item[0],
                item[2],
                len(item[3]),
                item[3][0].start,
            ),
        )

        for bonus, emergency_overload_used, _, plan in ranked_plans:
            payload = build_task_payload(task, plan)
            if emergency_overload_used:
                payload["usedEmergencyOverload"] = True
            search(
                index + 1,
                committed_segments + plan,
                scheduled_payloads + [payload],
                unscheduled_payloads,
                current_score + bonus,
            )

        reason = build_unscheduled_reason(
            task,
            time_blocks,
            committed_segments,
            today=now.date(),
            feasible_plans=plans,
        )

        search(
            index + 1,
            committed_segments,
            scheduled_payloads,
            unscheduled_payloads + [build_incomplete_payload(task, reason)],
            current_score,
        )

    search(0, [], [], [], 0)
    scheduled = sorted(
        best_result["scheduled"],
        key=lambda task: datetime.fromisoformat(task["segments"][0]["start"]),
    )
    unscheduled = sorted(
        best_result["unscheduled"],
        key=lambda task: (task["dueDate"], task["title"].lower()),
    )
    return scheduled, unscheduled


def schedule_tasks(time_blocks: list[TimeBlock], tasks: list[Task], *, now: datetime | None = None) -> dict:
    reference_now = now or datetime.now()
    ordered_blocks = sorted(time_blocks, key=lambda block: block.start)
    ordered_tasks = sorted(tasks, key=lambda task: task.sort_score(reference_now.date()))
    schedule, unscheduled = optimize_schedule(ordered_tasks, ordered_blocks, now=reference_now)

    incomplete_scheduled_count = sum(1 for item in schedule if item["completionStatus"] == "incomplete")
    complete_scheduled_count = len(schedule) - incomplete_scheduled_count

    return {
        "summary": {
            "timeBlockCount": len(ordered_blocks),
            "taskCount": len(tasks),
            "scheduledCount": len(schedule),
            "completeCount": complete_scheduled_count,
            "incompleteCount": incomplete_scheduled_count + len(unscheduled),
            "unscheduledCount": len(unscheduled),
            "totalAvailableMinutes": sum(block.duration_minutes for block in ordered_blocks),
            "totalPlannedMinutes": sum(task.estimate_minutes for task in tasks),
        },
        "schedule": schedule,
        "unscheduled": unscheduled,
    }


@app.get("/health")
def health_check():
    return jsonify({"status": "ok"})


@app.post("/api/schedule")
def create_schedule():
    payload = request.get_json(force=True, silent=False)
    time_blocks = [parse_time_block(item) for item in payload.get("timeBlocks", [])]
    tasks = [parse_task(item) for item in payload.get("tasks", [])]
    app.logger.info(
        "schedule request received: %s blocks, %s tasks",
        len(time_blocks),
        len(tasks),
    )

    if not time_blocks:
        return jsonify({"error": "At least one time block is required."}), 400
    if not tasks:
        return jsonify({"error": "At least one task is required."}), 400
    if any(block.end <= block.start for block in time_blocks):
        return jsonify({"error": "Each time block must end after it starts."}), 400
    if any(task.estimate_minutes <= 0 for task in tasks):
        return jsonify({"error": "Each task estimate must be greater than zero."}), 400
    if any(not is_step_aligned(block.start) or not is_step_aligned(block.end) for block in time_blocks):
        return jsonify({"error": "Time blocks must align to 15-minute boundaries."}), 400

    result = schedule_tasks(time_blocks, tasks)
    app.logger.info(
        "schedule result: %s scheduled, %s unscheduled",
        len(result["schedule"]),
        len(result["unscheduled"]),
    )
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", "5050"))
    app.run(host="127.0.0.1", port=port)
