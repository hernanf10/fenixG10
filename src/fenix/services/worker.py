"""Plantilla central de integración: un worker, un mensaje por vez."""
import json  # Convierte Body solo dentro del control por mensaje.
import time  # Evita un bucle intensivo si el emulador no responde.
from botocore.exceptions import BotoCoreError, ClientError
from fenix.services.sqs import receive, acknowledge, retry_later
from fenix.utils.errors import ProjectError, ValidationError


def run(url: str, handler, logger, once: bool = False) -> None:
    """Consume mensajes y confirma únicamente resultados durables.

    Args:
        url: URL de la cola de trabajo, obtenida por get_queue_url.
        handler: Callable handler(event) que revalida y ejecuta el caso de negocio.
        logger: Logger JSON del proceso worker.
        once: Si es True, realiza una sola consulta y termina; útil para pruebas.
    Returns:
        None al terminar la consulta única o al detenerse mediante Ctrl+C.
    Raises:
        NotImplementedError: Si todavía no se completaron las plantillas del alumno.
    Side Effects:
        Recibe mensajes, ejecuta el handler y cambia o confirma su recepción.
    Notes:
        Completar I5-A antes de usar. El parseo debe ocurrir por mensaje para que
        un JSON corrupto no termine el worker ni oculte los otros mensajes.
    """
    # TODO I5-A: implementar el algoritmo de la sección 4 de la consigna.
    # 1. Abrir un bucle; receive(url) queda en un try para fallos de transporte.
    # 2. Para cada mensaje, definir el contexto message_id antes de json.loads.
    # 3. Dentro del try por mensaje: decodificar JSON, exigir dict y llamar handler.
    # 4. acknowledge solo si handler terminó y persistió todos sus pasos.
    # 5. JSON/validación/negocio/red: registrar tipo seguro y llamar retry_later.
    # 6. Un fallo del ACK también deja el evento disponible; handler es idempotente.
    # 7. Nunca ACK en finally, ni borrar antes de notificar/persistir resultado.
    # 8. Fallos del transporte o de retry_later: registrar y volver tras 2 segundos.
    # 9. once termina tras una consulta; Ctrl+C sale y el main cierra el listener.
    # 10. Los NotImplementedError deben propagarse: revelan que falta completar.
    try:
        while True:
            try:
                messages = receive(url)  # El transporte puede fallar sin mensaje.
            except (BotoCoreError, ClientError) as error:
                logger.error("fallo_transporte", extra={
                    "event_id": None,
                    "message_id": None,
                    "attempt": None,
                    "action": "receive",
                    "status": type(error).__name__,
                })  # Solo el tipo de excepción, sin URL ni credenciales.
                if once:
                    return
                time.sleep(2)  # Espera acotada antes de reintentar la consulta.
                continue
            for message in messages:
                message_id = message.get("MessageId")
                attempt = None
                count = (message.get("Attributes") or {}).get("ApproximateReceiveCount")
                if count is not None:
                    try:
                        attempt = int(count)
                    except (TypeError, ValueError):
                        attempt = None  # El contexto queda nulo, no se inventa.
                event_id = None
                try:
                    event = json.loads(message["Body"])  # El parseo es por mensaje.
                    if not isinstance(event, dict):
                        raise ValidationError("El cuerpo no es un objeto JSON.")
                    if isinstance(event.get("event_id"), str):
                        event_id = event["event_id"]  # Correlación pública.
                    handler(event)  # Revalida el contrato y ejecuta el negocio.
                except (json.JSONDecodeError, ProjectError, BotoCoreError,
                        ClientError, OSError, KeyError, TypeError) as error:
                    logger.error("mensaje_rechazado", extra={
                        "event_id": event_id,
                        "message_id": message_id,
                        "attempt": attempt,
                        "action": "handler",
                        "status": type(error).__name__,
                    })  # Sin cuerpo ni ReceiptHandle en el log.
                    try:
                        delay = retry_later(url, message)  # Solo visibilidad.
                    except (BotoCoreError, ClientError, KeyError, TypeError,
                            ValueError) as retry_error:
                        logger.error("fallo_retry_later", extra={
                            "event_id": event_id,
                            "message_id": message_id,
                            "attempt": attempt,
                            "action": "retry_later",
                            "status": type(retry_error).__name__,
                        })  # Sin ACK: la visibilidad devuelve el mensaje solo.
                    else:
                        logger.warning("reintento_programado", extra={
                            "event_id": event_id,
                            "message_id": message_id,
                            "attempt": attempt,
                            "action": "retry_later",
                            "status": f"retry_in_{delay}s",
                        })  # El mensaje original conserva sus recepciones.
                else:
                    try:
                        acknowledge(url, message["ReceiptHandle"])  # ACK final.
                    except (BotoCoreError, ClientError, KeyError) as ack_error:
                        logger.error("fallo_ack", extra={
                            "event_id": event_id,
                            "message_id": message_id,
                            "attempt": attempt,
                            "action": "acknowledge",
                            "status": type(ack_error).__name__,
                        })  # El mensaje reaparecerá por visibilidad.
                    else:
                        logger.info("mensaje_confirmado", extra={
                            "event_id": event_id,
                            "message_id": message_id,
                            "attempt": attempt,
                            "action": "acknowledge",
                            "status": "acknowledged",
                        })  # El ACK se registra después de la confirmación del SDK.
            if once:
                return  # Una única consulta aunque no hubiera mensajes.
    except KeyboardInterrupt:
        logger.info("detenido_por_operador", extra={
            "event_id": None,
            "message_id": None,
            "attempt": None,
            "action": "stop",
            "status": "interrupted",
        })  # El main cierra el listener y ningún finally confirma mensajes.
        return
