from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Key = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,49}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Criterion(Contract):
    id: Key
    outcome: Text
    verification: Text


class Brief(Contract):
    schema_version: Literal[1] = 1
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    goal: Text
    users: Text
    scope: Text
    exclusions: str = Field(default="", max_length=4000)
    constraints: str = Field(default="", max_length=4000)
    target: Text
    open_questions: str = Field(default="", max_length=4000)
    criteria: list[Criterion] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_criteria(self):
        if len({item.id for item in self.criteria}) != len(self.criteria):
            raise ValueError("Acceptance criterion IDs must be unique.")
        return self


class Task(Contract):
    id: Key
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    role: Literal["planner", "implementer", "integrator", "verifier"]
    instructions: Text
    criteria: list[Key] = Field(default_factory=list, max_length=30)
    depends_on: list[Key] = Field(default_factory=list, max_length=50)


class Plan(Contract):
    schema_version: Literal[1] = 1
    brief_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    tasks: list[Task] = Field(min_length=1, max_length=50)

    def validate_against(self, brief: Brief) -> list[list[str]]:
        if self.brief_digest != digest(brief):
            raise ValueError("The plan must reference the exact brief revision.")
        tasks = {task.id: task for task in self.tasks}
        criteria = {criterion.id for criterion in brief.criteria}
        if len(tasks) != len(self.tasks):
            raise ValueError("Task IDs must be unique.")
        for task in self.tasks:
            if len(set(task.depends_on)) != len(task.depends_on):
                raise ValueError(f"{task.id}: prerequisites must be unique.")
            if set(task.depends_on) - tasks.keys() or task.id in task.depends_on:
                raise ValueError(f"{task.id}: prerequisites must name other tasks in this plan.")
            if len(set(task.criteria)) != len(task.criteria) or set(task.criteria) - criteria:
                raise ValueError(f"{task.id}: criteria must be unique IDs from this brief.")
        waves, done = [], set()
        while len(done) < len(tasks):
            ready = [
                task.id
                for task in self.tasks
                if task.id not in done and set(task.depends_on) <= done
            ]
            if not ready:
                raise ValueError("Task prerequisites form a cycle.")
            waves.append(ready)
            done.update(ready)
        ancestors = {}
        for wave in waves:
            for key in wave:
                ancestors[key] = set(tasks[key].depends_on)
                for dependency in tasks[key].depends_on:
                    ancestors[key].update(ancestors[dependency])
        for criterion in sorted(criteria):
            implementations = {
                task.id
                for task in self.tasks
                if task.role == "implementer" and criterion in task.criteria
            }
            verifiers = [
                task
                for task in self.tasks
                if task.role == "verifier" and criterion in task.criteria
            ]
            if not implementations or not verifiers:
                raise ValueError(
                    f"{criterion}: include both implementation and verification tasks."
                )
            if not any(implementations <= ancestors[task.id] for task in verifiers):
                raise ValueError(
                    f"{criterion}: verification must follow all its implementation tasks."
                )
        return waves


def digest(contract: Contract) -> str:
    encoded = json.dumps(contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def starter_plan(brief: Brief) -> Plan:
    criteria = [criterion.id for criterion in brief.criteria]
    tasks = [
        Task(
            id="design",
            title="Review the approach and shared interfaces",
            role="planner",
            criteria=criteria,
            instructions=(
                "Review the brief, repository, constraints, and verification methods. "
                "Define shared interfaces and file ownership before implementation. "
                "Resolve ambiguity with the operator. Record decisions and dependencies."
            ),
        )
    ]
    for index, criterion in enumerate(brief.criteria, 1):
        tasks.append(
            Task(
                id=f"build-{index}",
                title=criterion.outcome[:200],
                role="implementer",
                criteria=[criterion.id],
                depends_on=["design"],
                instructions=(
                    f"Implement {criterion.id} within the brief's scope and agreed interfaces. "
                    "Add development checks and return a clean contribution for integration. "
                    "Report blockers and changes outside the agreed scope."
                ),
            )
        )
    tasks.extend(
        [
            Task(
                id="integrate",
                title="Integrate the contributions",
                role="integrator",
                criteria=criteria,
                depends_on=[task.id for task in tasks if task.role == "implementer"],
                instructions=(
                    "Combine accepted contributions, resolve overlaps, and run the "
                    "project's mandatory checks on the integrated candidate. "
                    "Record the exact source identity."
                ),
            ),
            Task(
                id="verify",
                title="Verify every acceptance criterion",
                role="verifier",
                criteria=criteria,
                depends_on=["integrate"],
                instructions=(
                    "Independently apply each criterion's verification method to the integrated "
                    "candidate. Record criterion-specific results and evidence. Report missing "
                    "checks or failures; do not infer deployment readiness from task completion."
                ),
            ),
        ]
    )
    plan = Plan(brief_digest=digest(brief), tasks=tasks)
    plan.validate_against(brief)
    return plan
