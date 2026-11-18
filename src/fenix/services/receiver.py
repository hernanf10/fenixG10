"""Receptor FastAPI: autenticación, validación y aceptación durable por SQS."""
import os  # Recupera el secreto sin incluirlo en el repositorio.
import json  # Decodifica después de verificar los bytes.
from fastapi import FastAPI, Request, HTTPException  # Interfaz HTTP del curso.
from starlette.concurrency import run_in_threadpool  # Evita bloquear el event loop.
from botocore.exceptions import BotoCoreError, ClientError
from fenix.utils.crypto import verify
from fenix.utils.errors import ValidationError
from fenix.utils.state import load
from fenix.services.events import validate
from fenix.services.sqs import queue_url, publish

app = FastAPI(title="Fénix receptor de alertas")  # Un servidor, un worker Uvicorn.

@app.post("/webhook", status_code=202)
async def webhook(request: Request) -> dict:
    """Recibe un evento firmado y contesta 202 solo después de encolarlo.

    Args:
        request: Petición con X-Webhook-Signature y cuerpo de hasta 16 KiB.
    Returns:
        JSON con status=accepted, event_id y message_id de SQS.
    Raises:
        HTTPException: 401 firma; 400 JSON; 413 tamaño; 422 contrato; 503 SQS.
        NotImplementedError: Mientras no se complete TODO I3-B.
    Side Effects:
        Publica en SQS; no repara recursos ni llama a Telegram/Slack aquí.
    Notes:
        Leer bytes completos basta para el laboratorio; un proxy limitaría antes
        el tamaño de la transferencia en un despliegue real.
    """
    raw = await request.body()  # Se conservan los bytes originales.
    if len(raw) > 16384:  # Mantiene acotados los ejemplos didácticos.
        raise HTTPException(413, "Cuerpo demasiado grande")  # No encola.
    # TODO I3-B: verify(os.environ["WEBHOOK_SECRET"], cabecera, raw).
    # Capturar ValidationError de la firma como HTTPException(401, mensaje genérico).
    # Luego json.loads(raw); JSONDecodeError o UnicodeDecodeError -> 400.
    # Validar contra load("data/inventory.json"); ValidationError de esquema -> 422.
    # Usar await run_in_threadpool para load y llamadas Boto3 bloqueantes.
    # Obtener queue_url(os.environ["QUEUE_PREFIX"]) y llamar publish.
    # ClientError/BotoCoreError -> 503, sin prometer aceptación ni mostrar secretos.
    # Retornar accepted/event_id/message_id únicamente después de send_message exitoso.
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "Servicio no configurado")  # No se autentica a ciegas.
    try:
        verify(secret, request.headers.get("X-Webhook-Signature"), raw)
    except ValidationError as error:
        raise HTTPException(401, "Firma inválida") from error  # Sin eco del header.
    try:
        event = json.loads(raw)  # Los bytes firmados son los mismos que se parsean.
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(400, "JSON inválido") from error
    if not isinstance(event, dict):
        raise HTTPException(400, "El evento debe ser un objeto JSON")
    try:
        inventory = await run_in_threadpool(load, "data/inventory.json")
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(503, "Inventario no disponible") from error
    if not inventory:
        raise HTTPException(503, "Servicio no listo: falta el inventario")
    try:
        validated = validate(event, inventory)
    except ValidationError as error:
        raise HTTPException(422, "Contrato del evento inválido") from error
    prefix = os.environ.get("QUEUE_PREFIX", "")
    if not prefix:
        raise HTTPException(503, "Servicio no configurado")
    try:
        url = await run_in_threadpool(queue_url, prefix)
    except (ClientError, BotoCoreError) as error:
        raise HTTPException(503, "Cola no disponible") from error
    try:
        message_id = await run_in_threadpool(publish, url, validated)
    except (ClientError, BotoCoreError) as error:
        raise HTTPException(503, "No se pudo publicar el evento") from error
    return {"status": "accepted", "event_id": validated["event_id"],
            "message_id": message_id}  # Solo hay aceptación si SQS confirmó.
