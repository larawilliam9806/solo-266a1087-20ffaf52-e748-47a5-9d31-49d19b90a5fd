"""请求/响应模型。编号一律按字符串处理（允许数字输入，自动转为字符串）。"""
from __future__ import annotations

from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)


class SpecimenIn(_Base):
    """单件标本入库：标本号 + 种号。"""

    specimen_no: str
    species_no: str


class SpecimenBatch(_Base):
    """批量入库。"""

    items: List[SpecimenIn]


# 入库请求：单件、{"items": [...]} 或裸数组均可
IntakeBody = Union[SpecimenIn, SpecimenBatch, List[SpecimenIn]]


class SwitchRequest(_Base):
    """换版：注明现行版号，并给每个旧种号填一个或多个新种号。"""

    current_version: str
    new_version: Optional[str] = None
    mapping: dict[str, List[str]]

    @field_validator("mapping", mode="before")
    @classmethod
    def _normalize_mapping(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = {}
        for old, news in value.items():
            if isinstance(news, (str, int, float)):
                news = [news]
            normalized[str(old)] = [str(n) for n in news]
        return normalized


class ReidentifyRequest(_Base):
    """馆员凭当前版号，从候选中选定一种。"""

    version: str
    specimen_no: str
    species_no: str
