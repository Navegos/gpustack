from datetime import datetime
from typing import List, Optional
from sqlalchemy import Column
from sqlmodel import Field, SQLModel, JSON

from gpustack.schemas.common import PaginatedList
from gpustack.mixins import BaseModelMixin


class TaskBase(SQLModel):
    name: str
    method_path: str
    args: Optional[List] = Field(sa_column=Column(JSON), default_factory=list)
    node_id: Optional[int] = None
    pid: Optional[int] = None


class Task(TaskBase, BaseModelMixin, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class TaskCreate(TaskBase):
    pass


class TaskUpdate(TaskBase):
    pass


class TaskPublic(
    TaskBase,
):
    id: int
    created_at: datetime
    updated_at: datetime


TasksPublic = PaginatedList[TaskPublic]
