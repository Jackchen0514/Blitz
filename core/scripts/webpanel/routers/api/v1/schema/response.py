from typing import Optional
from pydantic import BaseModel, Field


class DetailResponse(BaseModel):
    detail: str

class IPLimitConfig(BaseModel):
    block_duration: Optional[int] = Field(None, example=60)
    max_ips: Optional[int] = Field(None, example=1)

class IPLimitConfigResponse(BaseModel):
    block_duration: Optional[int] = Field(None, description="Current block duration in seconds for IP Limiter")
    max_ips: Optional[int] = Field(None, description="Current maximum IPs per user for IP Limiter")

class TrafficCoefficientConfig(BaseModel):
    coefficient: float = Field(..., gt=0, example=1.9, description="Multiplier applied to counted upload/download traffic")

class TrafficCoefficientConfigResponse(BaseModel):
    coefficient: float = Field(1.9, description="Current global traffic coefficient (default 1.9)")

class SetupDecoyRequest(BaseModel):
    domain: str = Field(..., description="Domain name associated with the web panel")
    decoy_path: str = Field(..., description="Absolute path to the directory containing the decoy website files")

class DecoyStatusResponse(BaseModel):
    active: bool = Field(..., description="Whether the decoy site is currently configured and active")
    path: Optional[str] = Field(None, description="The configured path for the decoy site, if active")

class ConnLimitConfig(BaseModel):
    max_connections: int = Field(..., gt=0, example=2, description="Maximum concurrent connections per user")

class ConnLimitConfigResponse(BaseModel):
    max_connections: Optional[int] = Field(None, description="Current maximum concurrent connections per user (None = not configured, default 2)")