from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from pyfog.agent_credentials import credential_for_token, issue_credential, revoke_credentials
from pyfog.models import AgentCredential, Host
from pyfog.security import digest


def test_rotations_keep_bounded_grace_and_never_store_raw_tokens(app, host_id):
    settings = replace(app.state.settings, token_rotation_grace_seconds=30)
    with Session(app.state.engine) as db:
        host = db.get(Host, host_id)
        assert host is not None
        first_record, first = issue_credential(db, host, settings)
        db.commit()
        second_record, second = issue_credential(db, host, settings)
        db.commit()
        third_record, third = issue_credential(db, host, settings)
        db.commit()
        assert len({first, second, third}) == 3
        assert host.token_hash == digest(third)
        assert first not in host.token_hash
        assert second not in host.token_hash
        assert first_record.grace_until is not None
        assert second_record.grace_until is not None
        assert first_record.grace_until <= second_record.issued_at + timedelta(seconds=30)
        assert credential_for_token(db, host.id, first) is first_record
        assert credential_for_token(db, host.id, second) is second_record
        assert credential_for_token(db, host.id, third) is third_record
        hashes = db.scalars(select(AgentCredential.token_hash)).all()
        assert first not in hashes
        assert second not in hashes
        assert third not in hashes


def test_revocation_invalidates_every_generation_and_legacy_pointer(app, host_id):
    with Session(app.state.engine) as db:
        host = db.get(Host, host_id)
        assert host is not None
        credential, token = issue_credential(db, host, app.state.settings)
        db.commit()
        assert credential_for_token(db, host.id, token) is credential
        assert revoke_credentials(db, host) == 1
        db.commit()
        assert host.token_hash is None
        assert host.token_expires_at is None
        assert credential_for_token(db, host.id, token) is None
        assert credential.revoked_at is not None
