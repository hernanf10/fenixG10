"""Firma de apoyo y verificación a completar por el integrante 3."""
import hashlib  # Selecciona SHA-256 para autenticar el contenido.
import hmac  # Usa una comparación segura para las firmas.
import re  # Valida el formato estricto de la marca de tiempo y del hexadecimal.
import time  # Comprueba la ventana temporal.
from fenix.utils.errors import ValidationError  # Rechazo de autenticación público.


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """Firma exactamente timestamp, punto y cuerpo binario original.

    Args:
        secret: Secreto compartido de al menos 16 caracteres, leído del entorno.
        timestamp: Unix epoch en segundos, igual al enviado en la cabecera.
        body: Bytes exactos enviados por HTTP; no volver a serializar el JSON.
    Returns:
        HMAC-SHA256 hexadecimal de 64 caracteres.
    Raises:
        ValidationError: Si el secreto es demasiado corto.
    """
    if len(secret) < 16:  # Se comprueba también al enviar la prueba.
        raise ValidationError("Configurar WEBHOOK_SECRET de 16 caracteres o más")
    signed = str(timestamp).encode("ascii") + b"." + body  # Prefijo autenticado.
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify(secret: str, header: str, body: bytes, now: int | None = None) -> None:
    """Rechaza cabeceras ambiguas y firmas o fechas inválidas.

    Args:
        secret: Secreto compartido del entorno, nunca del payload.
        header: Cabecera exacta t=<epoch>,v1=<hex> sin claves duplicadas.
        body: Bytes del cuerpo antes de llamar a json.loads.
        now: Reloj inyectable para pruebas; None usa time.time.
    Returns:
        None si las comprobaciones son satisfactorias.
    Raises:
        ValidationError: Formato inválido, firma incorrecta o diferencia mayor a 300 s.
        NotImplementedError: Mientras no se complete TODO I3-A.
    Notes:
        La ventana de tiempo mitiga replay antiguo. No impide repetir un evento
        válido dentro de la ventana; event_id e idempotencia cubren ese caso.
    """
    # TODO I3-A: separar por comas; exigir exactamente t y v1 una sola vez.
    # Rechazar timestamp no entero y v1 que no tenga 64 caracteres hexadecimales.
    # Usar abs((int(time.time()) if now is None else now) - timestamp) <= 300.
    # Calcular signature y comparar con hmac.compare_digest; no usar ==.
    # Convertir fallos de parseo en ValidationError sin mostrar header ni secreto.
    if not isinstance(header, str):
        raise ValidationError("Cabecera de firma inválida.")
    parts = header.split(",")
    if len(parts) != 2:
        raise ValidationError("Cabecera de firma inválida.")
    fields: dict[str, str] = {}
    for part in parts:
        key, separator, value = part.partition("=")
        if not separator or key in fields:
            raise ValidationError("Cabecera de firma inválida.")
        fields[key] = value
    if set(fields) != {"t", "v1"}:
        raise ValidationError("Cabecera de firma inválida.")
    timestamp_text = fields["t"]
    received = fields["v1"]
    if re.fullmatch(r"[0-9]+", timestamp_text) is None:
        raise ValidationError("Marca de tiempo de firma inválida.")
    if re.fullmatch(r"[0-9a-fA-F]{64}", received) is None:
        raise ValidationError("Firma con formato inválido.")
    timestamp = int(timestamp_text)
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > 300:
        raise ValidationError("Firma fuera de la ventana de 300 segundos.")
    expected = signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, received.lower()):
        raise ValidationError("Firma incorrecta.")
