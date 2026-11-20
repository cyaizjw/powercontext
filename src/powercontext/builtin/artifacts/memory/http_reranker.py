from __future__ import annotations

import math
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from powercontext.builtin.inference import InferenceUsage
from powercontext.builtin.artifacts.memory.reranking import (
    MemoryReranker,
    MemoryRerankDecision,
)
from powercontext.builtin.artifacts.memory.models import MemoryHit


class HTTPMemoryReranker(MemoryReranker):
    policy_id: str

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        # 修复 1：在初始化时校验 batch_size
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")
            
        self.endpoint = endpoint
        self.client = client
        self.batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        # 参数边界校验
        if not candidates:
            raise ValueError("rerank requires at least one candidate")
        if limit < 1 or limit > len(candidates):
            raise ValueError("limit must be between 1 and candidate count")

        # 生成全局 rank 和 text 的映射
        global_candidates = [
            {"rank": i + 1, "text": hit.text} 
            for i, hit in enumerate(candidates)
        ]

        # 分批发送
        batches = [
            global_candidates[i : i + self.batch_size]
            for i in range(0, len(global_candidates), self.batch_size)
        ]

        all_scores = {}  # rank -> score
        request_count = 0

        for batch in batches:
            payload = {"query": query, "candidates": batch}
            
            request_count += 1
            try:
                response = await self.client.post(self.endpoint, json=payload)
                response.raise_for_status()
            except httpx.HTTPError as e:
                # 网络错误直接抛出
                raise e

            try:
                data = response.json()
            except Exception:
                raise ValueError("Invalid JSON response")

            if "scores" not in data or not isinstance(data["scores"], list):
                raise ValueError("Response missing 'scores'")

            batch_ranks = {c["rank"] for c in batch}
            returned_ranks = set()

            for score_item in data["scores"]:
                # 修复 2：必须恰好包含 rank 和 score 两个键
                if not isinstance(score_item, dict) or set(score_item.keys()) != {"rank", "score"}:
                    raise ValueError("Invalid score item schema")
                
                rank = score_item["rank"]
                score = score_item["score"]

                if rank not in batch_ranks:
                    raise ValueError(f"Unexpected rank {rank} in batch response")
                if rank in returned_ranks:
                    raise ValueError(f"Duplicate rank {rank} in batch response")
                
                # 必须是有限数值
                if not isinstance(score, (int, float)) or math.isnan(score) or math.isinf(score):
                    raise ValueError(f"Invalid score for rank {rank}")

                all_scores[rank] = float(score)
                returned_ranks.add(rank)

            # 确保每个批次的所有 rank 都被返回了
            if returned_ranks != batch_ranks:
                raise ValueError("Batch response incomplete: missing some ranks")

        # 全局排序：按分数降序，同分按原 rank 升序
        sorted_ranks = sorted(
            all_scores.keys(),
            key=lambda r: (-all_scores[r], r)
        )

        # 截取前 limit 个
        selected_ranks = tuple(sorted_ranks[:limit])

        usage = InferenceUsage(requests=request_count)
        
        return MemoryRerankDecision(
            selected_ranks=selected_ranks,
            usage=usage,
            discarded_rank_count=0,
        )