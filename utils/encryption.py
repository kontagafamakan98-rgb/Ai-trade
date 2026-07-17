from typing import Optional
from cryptography.fernet import Fernet
from config import ENCRYPTION_KEY

_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not ENCRYPTION_KEY:
            raise RuntimeError(
                "ENCRYPTION_KEY manquante. Génère-en une avec :\n"
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"\n"
                "puis ajoute-la comme variable d'environnement sur Render."
            )
        key = ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY
        _fernet = Fernet(key)
    return _fernet


def encrypt(data: str) -> str:
    """Chiffre une chaîne, retourne une chaîne stockable telle quelle en BDD."""
    return _get_fernet().encrypt(data.encode()).decode()


def decrypt(token: str) -> str:
    """Déchiffre une chaîne préalablement chiffrée avec encrypt()."""
    return _get_fernet().decrypt(token.encode()).decode()