"""Rotating ``TOKEN_ENCRYPTION_KEY`` without anything going dark.

``test_credential_decryption_failures.py`` is the other side of this file: it
pins what each caller does when a stored secret will not decrypt, and every one
of its scenarios is a key *replaced*. That was the only rotation the setting
could express — one key in, one key out, and at that instant every platform
credential, webhook signing secret and inbound trigger secret in the database
became unreadable at once. Publishing stopped until somebody went and fetched
new API tokens; every webhook receiver rejected every delivery until its
operator was handed a new secret. ``deploy/DEPLOYMENT.md`` said as much.

So the setting now holds a *list*, newest first: the head encrypts, all of them
decrypt. A rotation is three steps, and this file is one test per step —

1. prepend the new key. Nothing is re-encrypted yet and everything still reads,
   because the old key is still in the list;
2. run the rewrap sweep. Each stored secret is read under whichever key wrote it
   and written back under the head one;
3. drop the old key. This is the step that used to be the outage, and after (2)
   there is nothing left encrypted under the key being removed.

The load-bearing assertion is the *third* one. A test that rotates and then
checks the credential still reads proves only that the old key is still in the
list doing the work — the rotation is not finished until the old key can come
out, and that is what makes step 2 more than decoration.
"""
from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet

from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.models.trigger import Trigger, TriggerKind
from app.models.webhook import Webhook, WebhookEvent
from app.services import credential_rotation, crypto, publishing_service, webhooks
from app.services import triggers as trigger_service
from app.services.crypto import (
    CredentialEncryptionError,
    decrypt_credentials,
    encrypt_credentials,
)

#: The three secrets planted, one per encrypted column. Distinct strings so a
#: test that finds one where another belongs says so.
API_KEY = "devto-api-key-9e1f"
WEBHOOK_SECRET = "webhook-signing-secret-4a2c"
TRIGGER_SECRET = "trigger-signing-secret-77bd"


@pytest.fixture
def old_key(monkeypatch) -> str:
    """The key everything is encrypted under before the rotation."""
    value = Fernet.generate_key().decode()
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", value)
    return value


@pytest.fixture
def set_keys(monkeypatch):
    """Point the setting at a key list, newest first."""

    def _set(*keys: str) -> None:
        monkeypatch.setattr(
            "app.services.crypto.settings.token_encryption_key", ",".join(keys)
        )

    return _set


@pytest.fixture
def secrets(db, user, project, old_key) -> dict:
    """One row in each of the three tables that hold ciphertext."""
    connection = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": API_KEY}),
        display_name="@r2st",
    )
    webhook = Webhook(
        user_id=user.id,
        url="https://hooks.example.com/pulse",
        description="Slack",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret(WEBHOOK_SECRET),
    )
    trigger = Trigger(
        project_id=project.id,
        kind=TriggerKind.WEBHOOK,
        name="Inbound",
        is_active=True,
        encrypted_secret=trigger_service.store_secret(TRIGGER_SECRET),
    )
    db.add_all([connection, webhook, trigger])
    db.commit()
    for row in (connection, webhook, trigger):
        db.refresh(row)
    return {"connection": connection, "webhook": webhook, "trigger": trigger}


def _all_three_read(db, user, secrets) -> None:
    """Every stored secret, through the caller that actually uses it.

    Deliberately not ``decrypt_credentials`` three times: the point of a
    rotation surviving is that *publishing* and *signing* keep working, and both
    of those go through a service that swallows an unreadable secret into ``""``
    rather than raising. Asserting on the value is what catches that.
    """
    assert publishing_service._credentials_for(db, user.id, Platform.DEVTO) == {
        "api_key": API_KEY
    }
    assert webhooks.read_secret(secrets["webhook"]) == WEBHOOK_SECRET
    assert trigger_service.read_secret(secrets["trigger"]) == TRIGGER_SECRET


# --------------------------------------------------------------------------- #
# Step 1 — the new key goes on the front                                       #
# --------------------------------------------------------------------------- #


def test_a_prepended_key_leaves_every_stored_secret_readable(
    db, user, secrets, old_key, set_keys
):
    """The moment of the deploy. Nothing has been re-encrypted yet."""
    new_key = Fernet.generate_key().decode()
    set_keys(new_key, old_key)

    _all_three_read(db, user, secrets)


def test_new_writes_go_under_the_head_key_not_the_old_one(old_key, set_keys):
    """Otherwise the list would keep the old key alive forever.

    Fernet ciphertext carries no key id, so the check is by construction: the
    blob must decrypt under the new key alone and must not under the old one.
    """
    new_key = Fernet.generate_key().decode()
    set_keys(new_key, old_key)

    stored = encrypt_credentials({"api_key": API_KEY})

    set_keys(new_key)
    assert decrypt_credentials(stored) == {"api_key": API_KEY}
    set_keys(old_key)
    with pytest.raises(CredentialEncryptionError):
        decrypt_credentials(stored)


def test_key_order_decides_which_one_writes(old_key, set_keys):
    """The list is newest-first, and putting it the other way round is a
    plausible mistake that must not silently keep writing under the old key.
    """
    new_key = Fernet.generate_key().decode()

    set_keys(old_key, new_key)  # backwards on purpose
    stored = encrypt_credentials({"api_key": API_KEY})

    set_keys(old_key)
    assert decrypt_credentials(stored) == {"api_key": API_KEY}


@pytest.mark.parametrize("separator", [",", " ", "\n", ", ", "\n  "])
def test_keys_may_be_separated_by_commas_or_whitespace(monkeypatch, separator):
    """A value pasted across two lines of a ``.env`` is still two keys, not one
    unusable string — and a Fernet key contains neither a comma nor a space, so
    splitting on both can never cut one in half.
    """
    first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(
        "app.services.crypto.settings.token_encryption_key",
        separator.join([first, second]),
    )

    assert crypto.configured_keys() == [first, second]


# --------------------------------------------------------------------------- #
# Step 2 — the sweep moves the rows                                            #
# --------------------------------------------------------------------------- #


def test_the_sweep_reports_what_is_still_on_an_old_key(
    db, secrets, old_key, set_keys
):
    new_key = Fernet.generate_key().decode()
    set_keys(new_key, old_key)

    assert credential_rotation.pending(db) == 3

    credential_rotation.rewrap_all(db)

    assert credential_rotation.pending(db) == 0


def test_the_sweep_reencrypts_all_three_tables(db, secrets, old_key, set_keys):
    new_key = Fernet.generate_key().decode()
    set_keys(new_key, old_key)

    result = credential_rotation.rewrap_all(db)

    assert result.rewrapped == 3
    assert result.unreadable == 0
    assert result.by_table == {
        "platform_connections": 1,
        "webhooks": 1,
        "triggers": 1,
    }


def test_the_sweep_is_a_no_op_when_nothing_has_rotated(db, secrets, old_key):
    """It runs nightly on installs that will never rotate. It must not rewrite
    every stored secret every night just for having run.
    """
    before = {
        "connection": secrets["connection"].encrypted_credentials,
        "webhook": secrets["webhook"].encrypted_secret,
        "trigger": secrets["trigger"].encrypted_secret,
    }

    result = credential_rotation.rewrap_all(db)

    assert result.rewrapped == 0
    assert result.by_table == {}
    assert secrets["connection"].encrypted_credentials == before["connection"]
    assert secrets["webhook"].encrypted_secret == before["webhook"]
    assert secrets["trigger"].encrypted_secret == before["trigger"]


def test_the_sweep_does_nothing_on_a_keyless_box(db, user, project, monkeypatch):
    """Development stores plaintext deliberately. "Not encrypted under the
    current key" is true of every row there, and acting on it would rewrite the
    same plaintext nightly.
    """
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", "")
    row = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": API_KEY}),
    )
    db.add(row)
    db.commit()
    assert not row.encrypted_credentials.startswith("fernet:")

    result = credential_rotation.rewrap_all(db)

    assert result.rewrapped == 0
    assert credential_rotation.pending(db) == 0


def test_the_sweep_encrypts_a_plaintext_row_once_a_key_exists(
    db, user, monkeypatch, old_key
):
    """The other half of the keyless case: a row written before the key was
    configured is exactly what this sweep should pick up first.
    """
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", "")
    row = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": API_KEY}),
    )
    db.add(row)
    db.commit()

    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", old_key)
    result = credential_rotation.rewrap_all(db)

    assert result.rewrapped == 1
    assert row.encrypted_credentials.startswith("fernet:v1:")
    assert decrypt_credentials(row.encrypted_credentials) == {"api_key": API_KEY}


def test_a_row_no_key_can_read_is_counted_and_left_alone(
    db, user, secrets, set_keys, caplog
):
    """The dangerous mistake is dropping the old key *before* the sweep ran.

    The ciphertext is then the only copy of a secret nobody can read — and it
    becomes readable again the moment the old key goes back in the setting. So
    the sweep must not blank, delete or "reset" the row. It counts it, says so,
    and moves on to the rows it can help.
    """
    unrelated = Fernet.generate_key().decode()
    set_keys(unrelated)  # old key dropped too early
    before = secrets["connection"].encrypted_credentials

    with caplog.at_level(logging.WARNING):
        result = credential_rotation.rewrap_all(db)

    assert result.rewrapped == 0
    assert result.unreadable == 3
    assert secrets["connection"].encrypted_credentials == before


def test_an_empty_secret_is_not_rewrapped_into_a_real_one(db, project, old_key):
    """A trigger with signature checking switched off stores ``""``. Encrypting
    that would turn "no secret" into a perfectly good ciphertext of ``{}`` and
    make every nightly sweep find work to do.
    """
    row = Trigger(
        project_id=project.id,
        kind=TriggerKind.WEBHOOK,
        name="No secret",
        is_active=True,
        encrypted_secret="",
    )
    db.add(row)
    db.commit()

    result = credential_rotation.rewrap_all(db)

    assert result.rewrapped == 0
    assert row.encrypted_secret == ""


# --------------------------------------------------------------------------- #
# Step 3 — the old key comes out. The one that used to be an outage.           #
# --------------------------------------------------------------------------- #


def test_the_old_key_can_be_dropped_once_the_sweep_has_run(
    db, user, secrets, old_key, set_keys
):
    """The whole point. Publishing, webhook signing and inbound verification all
    keep working across a completed rotation, with the old key gone from the
    setting and nobody having re-entered anything.
    """
    new_key = Fernet.generate_key().decode()

    set_keys(new_key, old_key)
    credential_rotation.rewrap_all(db)
    set_keys(new_key)

    _all_three_read(db, user, secrets)


def test_dropping_the_old_key_before_the_sweep_is_what_breaks(
    db, user, secrets, old_key, set_keys
):
    """The negative control for the test above.

    Without it, that test proves only that a Fernet key decrypts what it
    encrypted — it has to be possible to get this wrong for the sweep to be
    doing anything.
    """
    from app.services.publishers.base import CredentialError

    set_keys(Fernet.generate_key().decode())

    with pytest.raises(CredentialError):
        publishing_service._credentials_for(db, user.id, Platform.DEVTO)
    assert webhooks.read_secret(secrets["webhook"]) == ""
    assert trigger_service.read_secret(secrets["trigger"]) == ""


# --------------------------------------------------------------------------- #
# The sweep's coverage of the schema, and what it says out loud                #
# --------------------------------------------------------------------------- #


def test_every_encrypted_column_in_the_schema_is_swept():
    """A table added with a secret in it and not added to the sweep keeps its
    rows on the old key forever — and the failure is silent, because the rotation
    looks complete right up until the key is dropped and that one table breaks.

    Naming convention is the hook: every such column in this schema is called
    ``encrypted_*``, which is what makes this checkable rather than a list
    somebody has to remember to update.
    """
    import app.models  # noqa: F401 - registers every table
    from app.database import Base

    in_schema = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name.startswith("encrypted_")
    }
    swept = {
        (table, attribute)
        for table, _, attribute in credential_rotation._ENCRYPTED_COLUMNS
    }

    assert in_schema == swept, (
        "columns holding ciphertext that the rotation sweep does not walk: "
        f"{sorted(in_schema - swept)}; entries in the sweep with no such column: "
        f"{sorted(swept - in_schema)}. Add it to "
        "app.services.credential_rotation._ENCRYPTED_COLUMNS."
    )


def test_the_beat_task_runs_the_sweep_and_returns_what_it_did(
    db, secrets, old_key, set_keys, monkeypatch, task_session
):
    """The scheduled entry point, not just the service under it — an operator
    running this by hand after a rotation is the documented step 2, and the
    counts it prints are how they know the old key is safe to drop.
    """
    from app.tasks import maintenance_tasks

    set_keys(Fernet.generate_key().decode(), old_key)
    monkeypatch.setattr(
        "app.tasks.maintenance_tasks.SessionLocal", task_session
    )

    assert maintenance_tasks.rewrap_credentials() == {
        "rewrapped": 3,
        "unreadable": 0,
        "by_table": {"platform_connections": 1, "webhooks": 1, "triggers": 1},
    }


def test_nothing_it_says_contains_the_secret(db, user, secrets, set_keys, caplog):
    """Every message here is written while handling a credential, and log files
    are not held to the standard the column is. Covers both branches: the rows
    it re-encrypts and the ones it cannot read.
    """
    unrelated = Fernet.generate_key().decode()
    ciphertext = secrets["connection"].encrypted_credentials

    with caplog.at_level(logging.DEBUG):
        credential_rotation.rewrap_all(db)  # readable: the success branch
        set_keys(unrelated)
        credential_rotation.rewrap_all(db)  # unreadable: the warning branch

    spoken = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (API_KEY, WEBHOOK_SECRET, TRIGGER_SECRET, ciphertext):
        assert secret not in spoken


def test_a_bad_key_further_down_the_list_is_named_by_position(monkeypatch):
    """"TOKEN_ENCRYPTION_KEY is not a valid Fernet key" does not say enough when
    the setting holds two of them, and the typo is likelier in the one being
    pasted back in from an old deploy.
    """
    good = Fernet.generate_key().decode()
    monkeypatch.setattr(
        "app.services.crypto.settings.token_encryption_key", f"{good},not-a-key"
    )

    with pytest.raises(CredentialEncryptionError, match="key 2 of TOKEN_ENCRYPTION_KEY"):
        encrypt_credentials({"api_key": API_KEY})


def test_production_validates_every_key_in_the_list():
    """Startup already refuses a malformed key. A malformed *second* key is the
    one nobody would notice, because it only matters while rows are still
    encrypted under it — which is exactly the window the sweep needs it in.
    """
    from pydantic import ValidationError

    from app.config import Settings

    base = {
        "environment": "production",
        "jwt_secret": "x" * 48,
        "database_url": "postgresql+psycopg://u:p@localhost/pulse",
    }
    good = Fernet.generate_key().decode()

    Settings(**base, token_encryption_key=f"{good},{Fernet.generate_key().decode()}")

    with pytest.raises(ValidationError, match="key 2 of TOKEN_ENCRYPTION_KEY"):
        Settings(**base, token_encryption_key=f"{good},not-a-key")
