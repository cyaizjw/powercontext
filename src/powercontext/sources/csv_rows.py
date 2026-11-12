import csv
import io
from typing import Tuple
from pydantic import BaseModel
import urllib.parse

# ==========================================
# 1. 定义数据结构 (对齐赛题 2.2 节)
# ==========================================
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

# ==========================================
# 2. 实现 CSV 解析逻辑 (对齐赛题 2.3 节)
# ==========================================
def parse_csv_document(value: CSVDocumentInput) -> Tuple[CSVRowInput, ...]:
    """
    按主键逐行解析离线 CSV，返回所有有效的行数据。
    """
    # 1. 解码：严格 UTF-8，并处理文件头的 BOM
    try:
        # utf-8-sig 会自动剔除 BOM 头
        text = value.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Invalid UTF-8 encoding")

    # 2. 使用 Python 标准 csv 模块解析
    # 按照赛题要求：逗号分隔，双引号引用，双引号转义，strict=True
    reader = csv.reader(io.StringIO(text), delimiter=',', quotechar='"', doublequote=True, strict=True)
    
    rows = list(reader)
    if not rows:
        raise ValueError("Empty CSV document")

    # 3. 提取并校验表头
    headers = rows[0]
    if not headers:
        raise ValueError("Empty header")

    for h in headers:
        # 表头字段必须非空、非纯空白且唯一
        if not h or not h.strip():
            raise ValueError("Header contains empty or whitespace-only column")
    if len(headers) != len(set(headers)):
        raise ValueError("Duplicate headers found")

    # 4. 校验主键列是否存在
    pk_col_name = value.primary_key
    if pk_col_name not in headers:
        raise ValueError(f"Primary key column '{pk_col_name}' not found in headers")
    
    pk_index = headers.index(pk_col_name)

    parsed_rows = []
    seen_pks = set()

    # 5. 逐行解析数据（从第二行开始，索引 1）
    for row in rows[1:]:
        # 忽略完全空白的物理行
        if not row or all(cell == "" for cell in row):
            continue

        # 字段数必须等于表头宽度
        if len(row) != len(headers):
            raise ValueError(f"Row length mismatch: expected {len(headers)}, got {len(row)}")

        # 提取主键值（不做 trim，保留原样字符串）
        pk_val = row[pk_index]
        
        # 主键值不能是空字符串或纯空白
        if not pk_val or not pk_val.strip():
            raise ValueError("Primary key value cannot be empty or whitespace-only")

        # 同一文档内主键必须唯一
        if pk_val in seen_pks:
            raise ValueError(f"Duplicate primary key value: {pk_val}")
        seen_pks.add(pk_val)

        # 构建单行结果，保留原始字段和列顺序
        parsed_rows.append(CSVRowInput(
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=pk_val,
            columns=tuple(headers),
            values=tuple(row)
        ))

    return tuple(parsed_rows)


# (注意：把上面这行 import 放到文件最顶部，和其他 import 放在一起)

# ==========================================
# 3. 实现 Source ID 生成与类定义 (对齐赛题 2.4 节)
# ==========================================

# 注意：这里假设官方有 Source 基类。如果没有，由于是网页端，我们先用普通类代替，
# 待会儿如果运行报错，我们再改成继承。
from powercontext.core import Source, SourceDefinition 

class CSVRowSource(Source):
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: Tuple[str, ...]
    values: Tuple[str, ...]

def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """
    按固定格式生成稳定的 source_id：
    csv:{percent-encoded document_id}:{percent-encoded primary_key_value}
    """
    # 使用 safe="" 确保所有特殊字符（包括斜杠等）都被编码
    encoded_doc_id = urllib.parse.quote(document_id, safe="")
    encoded_pk_val = urllib.parse.quote(primary_key_value, safe="")
    
    source_id = f"csv:{encoded_doc_id}:{encoded_pk_val}"
    
    # 校验长度限制
    if len(source_id) > 256:
        raise ValueError("Generated source_id exceeds 256 characters limit")
        
    return source_id
