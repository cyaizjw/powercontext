from __future__ import annotations

import hashlib
import json
import base64
from typing import Tuple, Any


class ContextPackError(Exception):
    """协议或分块校验错误"""
    pass


class ContextPack:
    def __init__(self, schema: str, items: Tuple[bytes, ...], digest: str, byte_count: int):
        self.schema = schema
        self.items = items
        self.digest = digest
        self.byte_count = byte_count

    def __eq__(self, other):
        if not isinstance(other, ContextPack):
            return NotImplemented
        return (self.schema == other.schema and 
                self.items == other.items and 
                self.digest == other.digest and 
                self.byte_count == other.byte_count)

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> ContextPack:
        if chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be > 0")

        raw_bytes = content.encode("utf-8")
        items = []
        
        while raw_bytes:
            chunk = raw_bytes[:chunk_bytes]
            while True:
                try:
                    chunk.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    if len(chunk) <= 1:
                        raise ContextPackError("Unable to form valid UTF-8 chunk")
                    chunk = chunk[:-1]
            items.append(chunk)
            raw_bytes = raw_bytes[len(chunk):]

        pack = cls.build(tuple(items))
        pack.to_bytes()
        return pack

    @classmethod
    def build(cls, items: Tuple[bytes, ...]) -> ContextPack:
        schema = "powercontext.context-pack.v1"
        b64_items = [base64.b64encode(item).decode("ascii") for item in items]
        
        unsigned_json = json.dumps(
            {"schema": schema, "items": b64_items},
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':')
        )
        digest = "sha256:" + hashlib.sha256(unsigned_json.encode("utf-8")).hexdigest()
        
        return cls(schema=schema, items=items, digest=digest, byte_count=0)

    def to_bytes(self) -> bytes:
        if not self.digest:
            rebuilt = self.build(self.items)
            self.digest = rebuilt.digest
            
        b64_items = [base64.b64encode(item).decode("ascii") for item in self.items]
        
        payload = {
            "schema": self.schema,
            "items": b64_items,
            "digest": self.digest,
            "byte_count": self.byte_count
        }
        
        payload["byte_count"] = 0
        while True:
            json_str = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            current_length = len(json_str.encode("utf-8"))
            if payload["byte_count"] == current_length:
                break
            payload["byte_count"] = current_length
            
        self.byte_count = payload["byte_count"]
        return json_str.encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception as e:
            raise ContextPackError(f"Invalid JSON: {e}")

        expected_keys = {"schema", "items", "digest", "byte_count"}
        if set(payload.keys()) != expected_keys:
            raise ContextPackError("Invalid pack fields")

        if payload["schema"] != "powercontext.context-pack.v1":
            raise ContextPackError("Invalid schema")

        try:
            items = tuple(base64.b64decode(item_str) for item_str in payload["items"])
        except Exception:
            raise ContextPackError("Invalid Base64 items")

        if len(data) != payload["byte_count"]:
            raise ContextPackError("Byte count mismatch")

        pack = cls(
            schema=payload["schema"],
            items=items,
            digest=payload["digest"],
            byte_count=payload["byte_count"]
        )

        b64_items = [base64.b64encode(item).decode("ascii") for item in items]
        unsigned_json = json.dumps(
            {"schema": payload["schema"], "items": b64_items},
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':')
        )
        expected_hash = "sha256:" + hashlib.sha256(unsigned_json.encode("utf-8")).hexdigest()
        if pack.digest != expected_hash:
            raise ContextPackError("Digest mismatch")

        return pack


class ContextPackDelta:
    def __init__(self, base_digest: str, target_digest: str, prefix_items: int, appended: Tuple[bytes, ...]):
        self.schema = "powercontext.context-pack-delta.v1"
        self.base_digest = base_digest
        self.target_digest = target_digest
        self.prefix_items = prefix_items
        self.appended = appended
        self.byte_count = 0

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        prefix_count = 0
        for old_item, new_item in zip(base.items, target.items):
            if old_item == new_item:
                prefix_count += 1
            else:
                break
        
        appended_items = target.items[prefix_count:]
        
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix_count,
            appended=appended_items
        )

    def to_bytes(self) -> bytes:
        b64_appended = [base64.b64encode(item).decode("ascii") for item in self.appended]
        payload = {
            "schema": self.schema,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": b64_appended
        }
        
        json_str = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.byte_count = len(json_str.encode("utf-8"))
        return json_str.encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            raise ContextPackError("Invalid JSON")

        expected_keys = {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
        if set(payload.keys()) != expected_keys:
            raise ContextPackError("Invalid delta fields")
            
        if payload["schema"] != "powercontext.context-pack-delta.v1":
            raise ContextPackError("Invalid delta schema")
            
        prefix = payload["prefix_items"]
        if not isinstance(prefix, int) or isinstance(prefix, bool) or prefix < 0:
            raise ContextPackError("Invalid prefix_items")

        try:
            appended = tuple(base64.b64decode(item_str) for item_str in payload["appended"])
        except Exception:
            raise ContextPackError("Invalid Base64 appended items")

        delta = cls(
            base_digest=payload["base_digest"],
            target_digest=payload["target_digest"],
            prefix_items=prefix,
            appended=appended
        )
        delta.byte_count = len(data)
        return delta

def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    if base.digest != delta.base_digest:
        raise ContextPackError("Base pack digest mismatch")

    if delta.prefix_items < 0 or delta.prefix_items > len(base.items):
        raise ContextPackError("Invalid prefix_items")

    new_items = base.items[:delta.prefix_items] + delta.appended
    new_pack = ContextPack.build(new_items)

    if new_pack.digest != delta.target_digest:
        raise ContextPackError("Target digest mismatch after applying delta")

    return new_pack