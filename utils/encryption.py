from cryptography.fernet import Fernet
from config import ENCRYPTION_KEY

fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)

def encrypt(data: str | bytes) -> bytes:
    if isinstance(data, str):
        data = data.encode()
    return fernet.encrypt(data)

def decrypt(token: bytes) -> str:
    return fernet.decrypt(token).decode()