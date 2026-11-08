import hashlib
import secrets
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

HASHER=PasswordHasher(time_cost=2,memory_cost=65536,parallelism=1)
# Missing-account checks perform the same KDF; no credential is encoded here.
DUMMY_HASH=HASHER.hash('not-a-user-'+secrets.token_urlsafe(32))

def hash_password(password):return HASHER.hash(password)

def verify_password(encoded,password):
    try:return HASHER.verify(encoded,password)
    except (Argon2Error,ValueError,TypeError):return False

def digest(value):return hashlib.sha256(value.encode()).hexdigest()

def new_token():return secrets.token_urlsafe(48)
