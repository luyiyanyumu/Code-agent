"""Editable development SOP documents, stored outside the project directory.

This graph records human decisions and deliverables. Saving or changing a status
never launches commands, tools, a model request, or a release.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from code_agent.storage.db import Database


NodeType = Literal[
    "requirements", "ui_design", "architecture", "planning", "implementation",
    "verification", "release", "maintenance", "custom",
]
NodeStatus = Literal["pending", "active", "done"]
Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-100_000, le=100_000)]
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowNode(StrictModel):
    id: Identifier
    type: NodeType
    title: str = Field(strict=True, min_length=1, max_length=120)
    x: Coordinate
    y: Coordinate
    objective: str = Field(default="", strict=True, max_length=4000)
    deliverable: str = Field(default="", strict=True, max_length=4000)
    acceptance: str = Field(default="", strict=True, max_length=4000)
    status: NodeStatus = "pending"
    output: str = Field(default="", strict=True, max_length=40_000)

    @field_validator("title")
    @classmethod
    def nonempty_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("节点名称不能为空")
        return value


class WorkflowEdge(StrictModel):
    id: Identifier
    source: Identifier
    target: Identifier


class WorkflowViewport(StrictModel):
    x: Coordinate = 0
    y: Coordinate = 0
    zoom: float = Field(default=1, strict=True, allow_inf_nan=False, ge=0.05, le=2.5)


class WorkflowDocument(StrictModel):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    name: str = Field(strict=True, min_length=1, max_length=120)
    nodes: list[WorkflowNode] = Field(max_length=128)
    edges: list[WorkflowEdge] = Field(max_length=512)
    viewport: WorkflowViewport

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("工作流名称不能为空")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> WorkflowDocument:
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("节点 ID 不能重复")
        if len({edge.id for edge in self.edges}) != len(self.edges):
            raise ValueError("连线 ID 不能重复")
        pairs: set[tuple[str, str]] = set()
        successors: dict[str, list[str]] = {key: [] for key in nodes}
        predecessors: dict[str, list[str]] = {key: [] for key in nodes}
        for edge in self.edges:
            if edge.source not in nodes or edge.target not in nodes:
                raise ValueError("连线必须连接现有节点")
            if edge.source == edge.target:
                raise ValueError("节点不能连接自身")
            pair = (edge.source, edge.target)
            if pair in pairs:
                raise ValueError("同一方向的节点连线不能重复")
            pairs.add(pair)
            successors[edge.source].append(edge.target)
            predecessors[edge.target].append(edge.source)
        indegree = {key: len(items) for key, items in predecessors.items()}
        ready = [key for key, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for target in successors[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if visited != len(nodes):
            raise ValueError("工作流不能包含循环连线")
        for node in self.nodes:
            if node.status == "done" and not node.output.strip():
                raise ValueError(f"完成「{node.title}」前，请填写实际产出")
            if node.status in ("active", "done") and any(
                nodes[source].status != "done" for source in predecessors[node.id]
            ):
                raise ValueError(f"「{node.title}」的前置阶段尚未完成")
        if len(self.model_dump_json().encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ValueError("工作流内容过大，请精简节点记录")
        return self


class WorkflowSave(StrictModel):
    expected_revision: StrictInt = Field(ge=0, le=2**53 - 1)
    workflow: WorkflowDocument


def default_workflow() -> dict:
    """Return a fresh graph each time; merely opening a project stores nothing."""
    stages = [
        ("requirements", "需求确认", "明确使用者、问题和核心需求。", "需求清单与优先级", "范围清晰，每项需求都有完成标准。"),
        ("ui_design", "UI与流程", "设计核心操作流程和界面状态。", "流程图、页面设计与组件规范", "核心场景及加载、空数据、失败状态齐全。"),
        ("architecture", "技术方案", "明确模块、接口、数据与部署方式。", "技术方案和关键验证结果", "关键风险已验证，接口与模块职责明确。"),
        ("planning", "任务拆分", "将需求拆成可以独立验收的任务。", "任务列表与完成条件", "每项任务具备输入、输出和验收标准。"),
        ("implementation", "开发实现", "先跑通核心场景，再补齐功能。", "可运行的实现与代码变更", "核心流程完整，代码可审查。"),
        ("verification", "测试验收", "验证功能、异常、性能和界面。", "测试结果与验收记录", "需求验收通过，已有功能无回归。"),
        ("release", "发布交付", "准备构建、配置、变更说明和回退方式。", "交付版本与发布记录", "发布条件满足，回退方式可用。"),
        ("maintenance", "维护迭代", "根据使用反馈和运行数据确定改进。", "问题记录与下一轮计划", "重要问题有结论，迭代任务已排序。"),
    ]
    positions = [(40 + index * 344, 60) for index in range(len(stages))]
    nodes = [
        {"id": kind, "type": kind, "title": title, "x": x, "y": y,
         "objective": objective, "deliverable": deliverable, "acceptance": acceptance,
         "status": "pending", "output": ""}
        for (kind, title, objective, deliverable, acceptance), (x, y) in zip(stages, positions)
    ]
    return {
        "schema_version": 1, "name": "项目开发 SOP", "nodes": nodes,
        "edges": [{"id": f"edge_{i + 1}", "source": nodes[i]["id"], "target": nodes[i + 1]["id"]} for i in range(7)],
        "viewport": {"x": 0, "y": 0, "zoom": 1}, "revision": 0, "updated_at": "",
    }


class WorkflowConflict(Exception):
    def __init__(self, current_revision: int):
        super().__init__("工作流已在其他窗口更新，请重新加载后再保存")
        self.current_revision = current_revision


class WorkflowStore:
    def __init__(self, db: Database):
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS sop_workflows ("
            "project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, "
            "tree_id TEXT NOT NULL, document_json TEXT NOT NULL, "
            "revision INTEGER NOT NULL CHECK(revision > 0), updated_at TEXT NOT NULL, "
            "PRIMARY KEY(project_id, tree_id))"
        )

    def get(self, project_id: str, tree: str) -> dict:
        row = self.db.query_one(
            "SELECT document_json, revision, updated_at FROM sop_workflows WHERE project_id=? AND tree_id=?",
            (project_id, tree),
        )
        if row is None:
            return default_workflow()
        return {**json.loads(row["document_json"]), "revision": row["revision"], "updated_at": row["updated_at"]}

    def save(self, project_id: str, tree: str, document: WorkflowDocument, expected_revision: int) -> dict:
        content = document.model_dump_json()
        revision = expected_revision + 1
        updated_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        # One SQL statement provides compare-and-swap across threads and processes.
        # A stale revision may neither create a missing row nor overwrite a newer one.
        cursor = self.db.execute(
            "INSERT INTO sop_workflows (project_id, tree_id, document_json, revision, updated_at) "
            "SELECT ?, ?, ?, ?, ? WHERE ?=0 OR EXISTS ("
            "SELECT 1 FROM sop_workflows WHERE project_id=? AND tree_id=? AND revision=?) "
            "ON CONFLICT(project_id, tree_id) DO UPDATE SET "
            "document_json=excluded.document_json, revision=excluded.revision, updated_at=excluded.updated_at "
            "WHERE sop_workflows.revision=?",
            (project_id, tree, content, revision, updated_at, expected_revision,
             project_id, tree, expected_revision, expected_revision),
        )
        if cursor.rowcount != 1:
            raise WorkflowConflict(self.get(project_id, tree)["revision"])
        return {**document.model_dump(), "revision": revision, "updated_at": updated_at}
