from __future__ import annotations

import json
import urllib.parse
from typing import Tuple, Any, AsyncIterator

from pydantic import BaseModel
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources import ConnectorBinding

class PiSessionInput(BaseModel):
    content: bytes
    leaf_id: str | None = None

class PiEventInput(BaseModel):
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: Any 

def parse_pi_session(value: PiSessionInput) -> Tuple[PiEventInput, ...]:
    try:
        text = value.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Invalid UTF-8 encoding")

    lines = text.splitlines()
    if not lines:
        raise ValueError("Empty session file")

    try:
        first_record = json.loads(lines[0])
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON in first line")

    if not isinstance(first_record, dict):
        raise ValueError("First line must be a JSON object")

    if first_record.get("type") != "session" or first_record.get("version") != 3:
        raise ValueError("First line must be a session node with version 3")
    
    session_id = first_record.get("id")
    if not session_id or not isinstance(session_id, str):
        raise ValueError("Session id must be a non-empty string")

    nodes = {}
    nodes[session_id] = first_record 

    for line in lines[1:]:
        if not line.strip():
            continue
        
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON")

        if not isinstance(record, dict):
            raise ValueError("Event must be a JSON object")

        event_id = record.get("id")
        parent_id = record.get("parentId")

        if not event_id or not isinstance(event_id, str):
            raise ValueError("Event id must be a non-empty string")
        
        if event_id in nodes:
            raise ValueError(f"Duplicate event id: {event_id}")

        if parent_id is not None:
            if not isinstance(parent_id, str) or parent_id not in nodes:
                raise ValueError(f"Missing parent: {parent_id}")

        nodes[event_id] = record

    if value.leaf_id is not None:
        leaf_id = value.leaf_id
        if leaf_id not in nodes:
            raise ValueError(f"Leaf id '{leaf_id}' not found in session")
    else:
        leaf_id = list(nodes.keys())[-1]

    path = []
    current_id = leaf_id
    visited = set()

    while current_id is not None:
        if current_id in visited:
            raise ValueError("Cycle detected in parent chain")
        visited.add(current_id)
        node = nodes[current_id]
        path.append(node)
        current_id = node.get("parentId")

    path.reverse()
    
    events = []
    for node in path:
        node_type = node.get("type")
        if node_type == "session":
            continue

        event_id = node.get("id")
        parent_id = node.get("parentId")
        
        role = None
        body = ""

        if node_type == "message":
            msg = node.get("message", {})
            role = msg.get("role")
            msg_content = msg.get("content")
            
            if role == "bashExecution":
                command = msg.get("command", "")
                output = msg.get("output", "")
                body = f"$ {command}\n{output}"
            elif role == "toolResult":
                tool_name = msg.get("toolName", "")
                tool_call_id = msg.get("toolCallId", "")
                text_content = ""
                if isinstance(msg_content, str):
                    text_content = msg_content
                elif isinstance(msg_content, list):
                    text_content = "\n".join([b.get("text", "") for b in msg_content if isinstance(b, dict) and b.get("type") == "text"])
                body = f"[tool_result {tool_name} {tool_call_id}] {text_content}"
            elif isinstance(msg_content, str):
                body = msg_content
            elif isinstance(msg_content, list):
                text_blocks = []
                for block in msg_content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_blocks.append(block.get("text", ""))
                        elif block.get("type") == "toolCall":
                            tool_name = block.get("name", "")
                            tool_id = block.get("id", "")
                            args = block.get("arguments", {})
                            compact_args = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                            text_blocks.append(f"[tool_call {tool_name} {tool_id}] {compact_args}")
                body = "\n".join(text_blocks)

        elif node_type == "toolResult":
            role = "toolResult"
            tool_name = node.get("toolName", "")
            tool_call_id = node.get("toolCallId", "")
            text = node.get("content", "")
            body = f"[tool_result {tool_name} {tool_call_id}] {text}"

        elif node_type == "bashExecution":
            role = "bashExecution"
            command = node.get("command", "")
            output = node.get("output", "")
            body = f"$ {command}\n{output}"

        elif node_type in ("compaction", "branch_summary"):
            role = node_type
            body = node.get("summary", "")

        elif node_type == "custom_message":
            role = "custom"
            msg_content = node.get("content")
            if isinstance(msg_content, str):
                body = msg_content
            elif isinstance(msg_content, list):
                text_blocks = [b.get("text", "") for b in msg_content if isinstance(b, dict) and b.get("type") == "text"]
                body = "\n".join(text_blocks)

        if role is None or not body:
            continue

        # 修复1：保留原始大小写
        final_content = f"{role}: {body}"

        events.append(PiEventInput(
            session_id=session_id,
            event_id=event_id,
            parent_id=parent_id,
            event_type=node_type,
            role=role,
            content=final_content,
            record=node
        ))

    return tuple(events)

class PiEventSource(Source):
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: Any

    @property
    def source_id(self) -> str:
        return self.name

def pi_event_source_id(session_id: str, event_id: str) -> str:
    encoded_sess = urllib.parse.quote(session_id, safe="")
    encoded_event = urllib.parse.quote(event_id, safe="")
    sid = f"pi-event:{encoded_sess}:{encoded_event}"
    if len(sid) >= 200:
        raise ValueError("Generated source_id exceeds 256 characters limit")
    return sid

class PiEventSourceAdapter:
    input_class = PiEventInput
    name = "pi-event"
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=pi_event_source_id(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )

class PiEventTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        metadata = {
            "session_id": source.session_id,
            "event_id": source.event_id,
            "parent_id": source.parent_id,
            "event_type": source.event_type,
            "role": source.role,
        }

        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=source.content,
            metadata=metadata
        )

PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)

class PiSessionConnector:
    
    def __init__(self, path: str, leaf_id: str | None = None):
        self.path = path
        self.leaf_id = leaf_id
        self.name = "pi-session-history"
        self.version = "1" 
        # 修复1：必须传入字符串名称
        self.source_definitions = frozenset(["pi-event"])

    async def run(self, session: Any) -> Any:
        import hashlib
        import json

        try:
            with open(self.path, "rb") as f:
                content = f.read()
        except Exception as e:
            raise ValueError(f"Failed to read file: {e}")

        session_input = PiSessionInput(content=content, leaf_id=self.leaf_id)
        events = parse_pi_session(session_input)

        previous_checkpoint = getattr(session, "checkpoint", {}) or {}
        new_checkpoint = dict(previous_checkpoint)

        for event in events:
            # 计算当前事件的完整内容哈希
            record_json = json.dumps(event.record, sort_keys=True)
            current_hash = hashlib.sha256(record_json.encode("utf-8")).hexdigest()

            if event.event_id in previous_checkpoint:
                saved_hash = previous_checkpoint[event.event_id]
                # 【核心修复】比对哈希，发现篡改立刻报错
                if saved_hash != current_hash:
                    raise ValueError(f"Event {event.event_id} was modified! History rewritten.")
                continue

            await session.submit(event.event_id, "pi-event", event)
            new_checkpoint[event.event_id] = current_hash

        from powercontext.sources.connectors import ConnectorRunCompletion
        return ConnectorRunCompletion(
            status="complete",
            checkpoint=new_checkpoint
        )