"""Typed wire contracts; engine validation checks mission semantics."""
from typing import Literal

from openenv.core.env_server.types import Action, Observation, State
from pydantic import BaseModel, ConfigDict, Field


class ReportDisposition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    report_id: str
    status: Literal['investigate', 'defer', 'dismiss']
    supporting_report_ids: list[str] = Field(default_factory=list)


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipient: str
    purpose: Literal['inform', 'investigate', 'evacuate']
    channel: Literal['radio', 'broadcast', 'call']
    report_ids: list[str] = Field(default_factory=list)


class Fleet(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scout_orders: list[dict]
    extinguisher_orders: list[dict]
    truck_orders: list[dict]
    mission: str = ''
    reason: str = ''


class WildfireAction(Action):
    fleet: Fleet
    report_dispositions: list[ReportDisposition] = Field(default_factory=list, max_length=100)
    messages: list[Message] = Field(default_factory=list, max_length=12)
    explanation: str = Field(default='', max_length=2000)


class WildfireObservation(Observation):
    data: dict
    terminated: bool = False
    truncated: bool = False


class WildfireState(State):
    tick: int = 0
    terminated: bool = False
    truncated: bool = False
