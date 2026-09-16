import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_mac(value: str) -> str:
    value = value.strip()
    delimited = r"(?:[0-9a-fA-F]{2}([:-]))(?:[0-9a-fA-F]{2}\1){4}[0-9a-fA-F]{2}"
    if not re.fullmatch(delimited, value) and not re.fullmatch(r"[0-9a-fA-F]{12}", value):
        raise ValueError("Ingresá una MAC válida, por ejemplo 52:54:00:12:34:56.")
    compact = value.replace(":", "").replace("-", "").lower()
    if compact == "000000000000" or int(compact[:2], 16) & 1:
        raise ValueError("La MAC debe identificar una interfaz unicast y no puede ser cero.")
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


ShortText = Annotated[str, Field(max_length=200, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, le=2**60, strict=True)]


class HostInput(Schema):
    name: str = Field(min_length=1, max_length=100)
    mac_address: str
    notes: str = Field(default="", max_length=4000)

    _mac = field_validator("mac_address")(normalize_mac)


class OperatingSystem(Schema):
    name: ShortText = "Linux"
    id: ShortText = "linux"
    version: ShortText = ""


class CPU(Schema):
    model: ShortText = ""
    logical_cores: Annotated[int, Field(gt=0, le=65536, strict=True)] | None = None


class Memory(Schema):
    total_bytes: PositiveInt | None = None


class System(Schema):
    manufacturer: ShortText = ""
    model: ShortText = ""
    serial_number: ShortText = ""


class Disk(Schema):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[\w./:-]+$")
    size_bytes: PositiveInt
    model: ShortText = ""
    serial_number: ShortText = ""
    transport: ShortText = ""
    logical_sector_bytes: Annotated[int, Field(gt=0, le=65536, strict=True)] | None = None
    removable: bool = Field(default=False, strict=True)


class Interface(Schema):
    name: str = Field(min_length=1, max_length=100)
    mac_address: str
    state: ShortText = "unknown"

    _mac = field_validator("mac_address")(normalize_mac)


class Inventory(Schema):
    schema_version: Literal[1]
    report_id: UUID
    collected_at: datetime
    hostname: str = Field(min_length=1, max_length=253)
    os: OperatingSystem
    kernel: ShortText
    architecture: ShortText
    cpu: CPU
    memory: Memory
    system: System = Field(default_factory=System)
    disks: list[Disk] = Field(default_factory=list, max_length=128)
    interfaces: list[Interface] = Field(min_length=1, max_length=128)
    warnings: list[Annotated[str, Field(max_length=500, strict=True)]] = Field(
        default_factory=list, max_length=64
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_is_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version debe ser el entero 1.")
        return value

    @field_validator("collected_at")
    @classmethod
    def valid_date(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("La fecha debe incluir su zona horaria.")
        if value > datetime.now(UTC) + timedelta(minutes=10):
            raise ValueError("La fecha de recolección está en el futuro; verificá el reloj.")
        return value.astimezone(UTC)

    @field_validator("interfaces")
    @classmethod
    def unique_interfaces(cls, value: list[Interface]) -> list[Interface]:
        if len({i.name for i in value}) != len(value):
            raise ValueError("Hay nombres de interfaces repetidos.")
        return value
