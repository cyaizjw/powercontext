from __future__ import annotations

import csv
import io
import json
import urllib.parse
from typing import Tuple, Annotated, Literal

from pydantic import BaseModel, Field, JsonValue

from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization


class CSVDocumentInput(BaseModel):
    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: Tuple[str, ...]
    values: Tuple[str, ...]


class CSVRowSource(Source):
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: Tuple[str, ...]
    values: Tuple[str, ...]


def parse_csv_document(value: CSVDocumentInput) -> Tuple[CSVRowInput, ...]:
    try:
        text = value.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Invalid UTF-8 encoding")

    reader = csv.reader(io.StringIO(text), delimiter=',', quotechar='"', doublequote=True, strict=True)
    
    rows = list(reader)
    if not rows:
        raise ValueError("Empty CSV document")

    headers = rows[0]
    if not headers:
        raise ValueError("Empty header")

    for h in headers:
        if not h or not h.strip():
            raise ValueError("Header contains empty or whitespace-only column")
    if len(headers) != len(set(headers)):
        raise ValueError("Duplicate headers found")

    pk_col_name = value.primary_key
    if pk_col_name not in headers:
        raise ValueError(f"Primary key column '{pk_col_name}' not found in headers")
    
    pk_index = headers.index(pk_col_name)

    parsed_rows = []
    seen_pks = set()

    for row in rows[1:]:
        if not row or all(cell == "" for cell in row):
            continue

        if len(row) != len(headers):
            raise ValueError(f"Row length mismatch: expected {len(headers)}, got {len(row)}")

        pk_val = row[pk_index]
        
        if not pk_val or not pk_val.strip():
            raise ValueError("Primary key value cannot be empty or whitespace-only")

        if pk_val in seen_pks:
            raise ValueError(f"Duplicate primary key value: {pk_val}")
        seen_pks.add(pk_val)

        parsed_rows.append(CSVRowInput(
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=pk_val,
            columns=tuple(headers),
            values=tuple(row)
        ))

    return tuple(parsed_rows)


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    encoded_doc_id = urllib.parse.quote(document_id, safe="")
    encoded_pk_val = urllib.parse.quote(primary_key_value, safe="")
    
    source_id = f"csv:{encoded_doc_id}:{encoded_pk_val}"
    
    if len(source_id) > 256:
        raise ValueError("Generated source_id exceeds 256 characters limit")
        
    return source_id


class CSVRowSourceAdapter:
    input_class = CSVDocumentInput
    name = "csv-row"
    source_class = CSVRowSource

    async def resolve(self, value: CSVDocumentInput, /) -> Tuple[CSVRowSource, ...]:
        # Adapter 的 resolve 逻辑：解析出多行，转换成 Source
        parsed_rows = parse_csv_document(value)
        return tuple(
            CSVRowSource(
                name=csv_row_source_id(row.document_id, row.primary_key_value),
                materialization=SourceMaterialization.CAPTURED,
                document_id=row.document_id,
                primary_key=row.primary_key,
                primary_key_value=row.primary_key_value,
                columns=row.columns,
                values=row.values,
            )
            for row in parsed_rows
        )


class CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row_dict = dict(zip(source.columns, source.values))
        content_str = json.dumps(row_dict, ensure_ascii=False, separators=(',', ':'))
        
        metadata = {
            "document_id": source.document_id,
            "primary_key": source.primary_key,
            "primary_key_value": source.primary_key_value,
            "columns": source.columns,
            "row": row_dict
        }

        return TextEvidence(
            source_type="csv-row",
            source_id=source.name,
            content=content_str,
            metadata=metadata
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    projections=(CSVRowTextEvidenceProjection(),),
)
