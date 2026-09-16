import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pyfog.models import Host, InventoryReport
from pyfog.schemas import Inventory
from pyfog.security import digest


def ingest_inventory(
    db: Session, host: Host, inventory: Inventory, source: str
) -> tuple[InventoryReport, bool]:
    if host.mac_address not in {interface.mac_address for interface in inventory.interfaces}:
        raise HTTPException(422, "El inventario no contiene la MAC principal de este equipo.")
    data = inventory.model_dump(mode="json")
    fingerprint = digest(json.dumps(data, sort_keys=True, separators=(",", ":")))
    query = select(InventoryReport).where(
        InventoryReport.host_id == host.id, InventoryReport.report_id == str(inventory.report_id)
    )
    previous = db.scalar(query)
    if previous:
        if previous.fingerprint != fingerprint:
            raise HTTPException(409, "Este report_id ya existe con otro contenido.")
        return previous, False
    report = InventoryReport(
        host_id=host.id,
        report_id=str(inventory.report_id),
        collected_at=inventory.collected_at.replace(tzinfo=None),
        source=source,
        fingerprint=fingerprint,
        data=data,
    )
    db.add(report)
    try:
        db.flush()
        # An old offline report is kept in history, but cannot replace newer hardware.
        latest = db.scalar(
            select(InventoryReport)
            .where(InventoryReport.host_id == host.id)
            .order_by(InventoryReport.collected_at.desc(), InventoryReport.received_at.desc())
            .limit(1)
        )
        if latest and latest.id == report.id:
            host.hostname = inventory.hostname
            host.os_name = inventory.os.name
        host.last_inventory_at = report.received_at
        db.commit()
    except IntegrityError:
        db.rollback()
        # A concurrent retry may have inserted the same immutable report first.
        previous = db.scalar(query)
        if previous and previous.fingerprint == fingerprint:
            return previous, False
        if previous:
            raise HTTPException(409, "Este report_id ya existe con otro contenido.") from None
        raise
    db.refresh(report)
    return report, True
