"""Guía ejecutable del integrante 6; matriz F01–F16 sobre el sistema Fénix."""
import gzip  # Lee el histórico comprimido por la rotación.
import json  # Construye eventos, ledgers y verifica cada línea JSON.
import logging  # Configura un pipeline propio de rotación para F16.
import time  # Genera timestamps de firma y espera visibilidad acotada.
import uuid  # Prefijo único para no tocar colas del laboratorio del grupo.
from logging.handlers import RotatingFileHandler  # Fuerza rotación con tamaño reducido.
from pathlib import Path  # Resuelve rutas de datos y temporales.

import pytest  # Organiza casos, fixtures y marcas de integración.
from botocore.exceptions import BotoCoreError, ClientError  # Fallos del emulador.
from fastapi.testclient import TestClient  # Ejercita el receptor sin abrir un puerto.

from fenix.config.settings import client  # Clientes apuntados al loopback.
from fenix.services import notifications, receiver, remediation, worker
from fenix.services.demo import inject_fault  # Fabrica el fallo de forma controlada.
from fenix.services.events import validate  # Contrato cerrado del evento.
from fenix.services.sqs import ensure_queues  # Colas con DLQ reales para integración.
from fenix.utils import state  # Persistencia real sobre tmp_path.
from fenix.utils.crypto import signature, verify  # HMAC-SHA256 del webhook.
from fenix.utils.errors import (PermanentError, RetryableError,
                                ValidationError)  # Errores del dominio.
from fenix.utils.logging_json import (JSONFormatter, gzip_name,
                                      gzip_rotate)  # Piezas de observabilidad.

SECRET = "clave-ficticia-solo-test"  # Fixture pública; nunca un secreto real.

INVENTARIO = {
    "node-01": {
        "type": "ec2",
        "id": "i-prueba-0001",
        "security_group_id": "sg-prueba-0001",
        "expected_tags": {"ManagedBy": "ppa2", "Project": "fenix",
                          "GroupId": "grupo-test", "Owner": "grupo-test"},
    },
    "bucket-01": {
        "type": "s3",
        "id": "fenix-grupo-test-bucket-01",
        "expected_tags": {"ManagedBy": "ppa2", "Project": "fenix",
                          "GroupId": "grupo-test", "Owner": "grupo-test"},
    },
}


def _event(**overrides) -> dict:
    """Devuelve un evento válido del contrato y aplica los cambios pedidos.

    Args:
        overrides: Campos que el caso de prueba necesita alterar.
    Returns:
        Diccionario listo para firmar, validar o inyectar en SQS.
    """
    event = {
        "schema_version": 1,
        "event_id": "evt-node-001",
        "event_type": "instance.stopped",
        "resource_id": "node-01",
        "occurred_at": "2026-10-22T18:00:00Z",
        "simulate_failures": 0,
    }
    event.update(overrides)
    return event


def _header(body: bytes, timestamp: int | None = None) -> str:
    """Firma los bytes exactos con una marca de tiempo verificable.

    Args:
        body: Cuerpo binario que se enviará sin reserializar.
        timestamp: Epoch a firmar; None usa el reloj actual.
    Returns:
        Cabecera con el formato t=<epoch>,v1=<hex>.
    """
    stamp = int(time.time()) if timestamp is None else timestamp
    return f"t={stamp},v1={signature(SECRET, stamp, body)}"


class _LoggerFalso:
    """Registra las llamadas del worker sin escribir archivos reales."""

    def __init__(self):
        self.registros: list[dict] = []

    def _anotar(self, level: str, message: str, extra: dict | None = None) -> None:
        self.registros.append({"level": level, "message": message, **(extra or {})})

    def debug(self, message, extra=None):
        self._anotar("DEBUG", message, extra)

    def info(self, message, extra=None):
        self._anotar("INFO", message, extra)

    def warning(self, message, extra=None):
        self._anotar("WARNING", message, extra)

    def error(self, message, extra=None):
        self._anotar("ERROR", message, extra)

    def critical(self, message, extra=None):
        self._anotar("CRITICAL", message, extra)


class _RespuestaFalsa:
    """Imita la parte de requests.Response que usa notify()."""

    def __init__(self, status_code: int, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("respuesta sin JSON")
        return self._payload


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture
def inventory() -> dict:
    """Entrega una copia profunda del inventario de prueba."""
    return json.loads(json.dumps(INVENTARIO))


@pytest.fixture
def receptor(monkeypatch, inventory):
    """Prepara el TestClient con SQS e inventario sustituidos.

    Returns:
        Tupla (cliente, publicados) para verificar también qué no se encoló.
    """
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("QUEUE_PREFIX", "fenix-test")
    monkeypatch.setattr(receiver, "load", lambda path: inventory)
    monkeypatch.setattr(receiver, "queue_url", lambda prefix: "http://cola-local")
    publicados: list[dict] = []
    monkeypatch.setattr(
        receiver, "publish",
        lambda url, event: publicados.append(event) or "msg-test-001",
    )
    return TestClient(receiver.app), publicados


@pytest.fixture
def ledger_temporal(tmp_path, monkeypatch, inventory):
    """Redirige el ledger de handle() a tmp_path y registra sus efectos.

    Returns:
        Tupla (llamadas, leer_ledger) sin tocar data/events.json del grupo.
    """
    data = tmp_path / "data"
    data.mkdir()
    (data / "inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    monkeypatch.setattr(
        remediation, "load",
        lambda path: state.load(str(data / Path(path).name)),
    )
    monkeypatch.setattr(
        remediation, "save",
        lambda path, value: state.save(str(data / Path(path).name), value),
    )
    llamadas = {"repair": [], "notify": []}
    monkeypatch.setattr(
        remediation, "repair",
        lambda resource, event_type: (
            llamadas["repair"].append((resource["id"], event_type)) or "repaired"
        ),
    )
    monkeypatch.setattr(
        remediation, "notify",
        lambda text: llamadas["notify"].append(text),
    )

    def leer_ledger() -> dict:
        return json.loads((data / "events.json").read_text(encoding="utf-8"))

    return llamadas, leer_ledger


# --- Pruebas pendientes originales, ahora con aserciones --------------------

@pytest.mark.pendiente
def test_duplicate_is_idempotent(ledger_temporal):
    """Comprueba que repetir un evento no repite recursos ni acciones completadas.

    Returns:
        None cuando dos ejecuciones conservan el estado final esperado.
    Notes:
        El handler relee el ledger del disco en cada llamada: representa un reinicio.
    """
    llamadas, leer_ledger = ledger_temporal
    evento = _event(event_id="evt-dup-001")
    remediation.handle(evento)
    remediation.handle(evento)  # No hay estado en memoria: se vuelve a leer el archivo.
    entrada = leer_ledger()["evt-dup-001"]
    assert len(llamadas["repair"]) == 1  # La reparación confirmada no se repite.
    assert len(llamadas["notify"]) == 1  # El aviso confirmado tampoco se repite.
    assert entrada["attempts"] == 2  # Cada recepción queda registrada.
    assert entrada["repair_done"] is True and entrada["notified"] is True
    assert entrada["status"] == "done"


@pytest.mark.pendiente
def test_ack_is_after_success(monkeypatch):
    """Verifica que un handler fallido jamás confirma la entrega de SQS.

    Returns:
        None si fallo=retry sin ACK y éxito=ACK exactamente al finalizar.
    Notes:
        Se reemplaza el transporte, no el worker: se observa el orden real de run().
    """
    entrega = {"MessageId": "msg-ack-1", "ReceiptHandle": "rh-ack-1",
               "Body": json.dumps(_event(event_id="evt-ack-001")),
               "Attributes": {"ApproximateReceiveCount": "1"}}
    orden: list[tuple] = []
    monkeypatch.setattr(worker, "receive", lambda url: [entrega])
    monkeypatch.setattr(worker, "acknowledge",
                        lambda url, receipt: orden.append(("ack", receipt)))
    monkeypatch.setattr(worker, "retry_later",
                        lambda url, message: (
                            orden.append(("retry", message["ReceiptHandle"])) or 2
                        ))
    logger = _LoggerFalso()

    def handler_falla(event):
        orden.append(("handler", "fallo"))
        raise RetryableError("falla simulada")

    worker.run("http://cola", handler_falla, logger, once=True)
    assert ("ack", "rh-ack-1") not in orden
    assert orden == [("handler", "fallo"), ("retry", "rh-ack-1")]

    orden.clear()

    def handler_ok(event):
        orden.append(("handler", "ok"))

    worker.run("http://cola", handler_ok, logger, once=True)
    assert orden == [("handler", "ok"), ("ack", "rh-ack-1")]  # ACK al final.


# --- I1-D: contrato cerrado ------------------------------------------------

def test_validate_contrato_cerrado_acepta_y_rechaza(inventory):
    """Automatiza la verificación manual de I1-D sobre events.validate."""
    assert validate(_event(), inventory) == _event()  # Copia válida sin modificar.
    with pytest.raises(ValidationError):
        validate({**_event(), "extra": 1}, inventory)  # Clave extra.
    sin_fecha = _event()
    del sin_fecha["occurred_at"]
    with pytest.raises(ValidationError):
        validate(sin_fecha, inventory)  # Clave ausente.
    with pytest.raises(ValidationError):
        validate(_event(schema_version=True), inventory)  # bool no es entero.
    with pytest.raises(ValidationError):
        validate(_event(simulate_failures=True), inventory)  # bool no es 0..5.
    with pytest.raises(ValidationError):
        validate(_event(resource_id="node-99"), inventory)  # Alias ajeno.
    with pytest.raises(ValidationError):
        validate(_event(event_type="bucket.tags_missing"), inventory)  # Tipo cruzado.
    with pytest.raises(ValidationError):
        validate(_event(event_id="EVT-NODE-001"), inventory)  # Mayúsculas.
    assert inventory == INVENTARIO  # Validar no altera el inventario.


# --- I3-A: formato y ventana de la firma -----------------------------------

def test_verify_ventana_y_formato_de_firma():
    """Automatiza la verificación manual de I3-A sobre crypto.verify."""
    cuerpo = b'{"x":1}'
    sello = 1_700_000_000
    valido = f"t={sello},v1={signature(SECRET, sello, cuerpo)}"
    verify(SECRET, valido, cuerpo, now=sello)  # Dentro de la ventana.
    with pytest.raises(ValidationError):
        verify(SECRET, valido, cuerpo, now=sello + 301)  # Fuera por el pasado.
    with pytest.raises(ValidationError):
        verify(SECRET, f"t={sello},v1={'0' * 64}", cuerpo, now=sello)  # Alterada.
    with pytest.raises(ValidationError):
        verify(SECRET, f"t={sello},v1=corto", cuerpo, now=sello)  # Hex incompleto.
    with pytest.raises(ValidationError):
        verify(SECRET, f"t={sello},t={sello},v1={signature(SECRET, sello, cuerpo)}",
               cuerpo, now=sello)  # Clave duplicada.
    with pytest.raises(ValidationError):
        verify(SECRET, f"t={sello}", cuerpo, now=sello)  # Falta v1.
    with pytest.raises(ValidationError):
        verify(SECRET, None, cuerpo, now=sello)  # Cabecera ausente.


# --- I5-A: el worker sobrevive a un mensaje corrupto ------------------------

def test_worker_continua_tras_json_corrupto(monkeypatch):
    """El parseo por mensaje no permite que un cuerpo roto termine el worker."""
    corrupto = {"MessageId": "msg-8", "ReceiptHandle": "rh-8", "Body": "{no-json",
                "Attributes": {"ApproximateReceiveCount": "1"}}
    entregas = iter([[corrupto], []])
    orden: list[tuple] = []
    monkeypatch.setattr(worker, "receive", lambda url: next(entregas))
    monkeypatch.setattr(worker, "acknowledge",
                        lambda url, receipt: orden.append(("ack", receipt)))
    monkeypatch.setattr(worker, "retry_later",
                        lambda url, message: (
                            orden.append(("retry", message["ReceiptHandle"])) or 2
                        ))
    logger = _LoggerFalso()
    worker.run("http://cola", lambda event: orden.append(("handler", "no")),
               logger, once=True)
    worker.run("http://cola", lambda event: orden.append(("handler", "no")),
               logger, once=True)  # Segunda consulta: la cola ya está vacía.
    assert ("handler", "no") not in orden  # El JSON roto ni siquiera llega al handler.
    assert ("ack", "rh-8") not in orden  # Un mensaje corrupto no se confirma.
    assert orden == [("retry", "rh-8")]
    assert any(row.get("action") == "retry_later" for row in logger.registros)


# --- F04–F07 y F13: receptor HTTP -----------------------------------------

def test_f04_firma_invalida_401(receptor):
    """Firma ausente, alterada o fuera de ventana no publica ni acepta."""
    cliente, publicados = receptor
    cuerpo = json.dumps(_event(event_id="evt-f04-001")).encode()

    assert cliente.post("/webhook", content=cuerpo).status_code == 401  # Sin cabecera.
    alterada = f"t={int(time.time())},v1={'0' * 64}"
    respuesta = cliente.post("/webhook", content=cuerpo,
                             headers={"X-Webhook-Signature": alterada})
    assert respuesta.status_code == 401
    vieja = _header(cuerpo, timestamp=int(time.time()) - 3600)
    respuesta = cliente.post("/webhook", content=cuerpo,
                             headers={"X-Webhook-Signature": vieja})
    assert respuesta.status_code == 401
    assert publicados == []  # Ningún rechazo llega a SQS.


def test_f05_bytes_distintos_misma_firma_401(receptor):
    """Firmar un texto y enviar otro con el mismo JSON lógico no autentica."""
    cliente, publicados = receptor
    evento = _event(event_id="evt-f05-001")
    original = json.dumps(evento, separators=(",", ":")).encode()
    distinto = json.dumps(evento).encode()  # Mismos datos, otros bytes.
    assert original != distinto
    cabecera = _header(original)
    respuesta = cliente.post("/webhook", content=distinto,
                             headers={"X-Webhook-Signature": cabecera})
    assert respuesta.status_code == 401
    assert publicados == []
    aceptada = cliente.post("/webhook", content=original,
                            headers={"X-Webhook-Signature": cabecera})
    assert aceptada.status_code == 202  # El original firmado sí se acepta.
    assert aceptada.json()["event_id"] == "evt-f05-001"


def test_f06_json_roto_400_y_lista_rechazada(receptor):
    """Un cuerpo firmado pero no parseable o no objeto se rechaza sin publicar."""
    cliente, publicados = receptor
    roto = b'{"schema_version": 1,'
    respuesta = cliente.post("/webhook", content=roto,
                             headers={"X-Webhook-Signature": _header(roto)})
    assert respuesta.status_code == 400
    lista = json.dumps([_event()]).encode()
    respuesta = cliente.post("/webhook", content=lista,
                             headers={"X-Webhook-Signature": _header(lista)})
    assert respuesta.status_code == 400  # El receptor exige un objeto JSON.
    assert publicados == []


def test_f07_alias_ajeno_y_claves_extra_rechazo(inventory):
    """Solo los aliases inventariados y las claves exactas son aceptados."""
    with pytest.raises(ValidationError):
        validate(_event(resource_id="bucket-99"), inventory)  # Alias ajeno.
    with pytest.raises(ValidationError):
        validate({**_event(), "ttl_seconds": 60}, inventory)  # Clave extra.
    assert inventory == INVENTARIO  # Ningún rechazo modifica el inventario.


def test_f13_sqs_caido_503(monkeypatch, inventory):
    """Si SQS no confirma, el receptor no promete aceptación."""
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("QUEUE_PREFIX", "fenix-test")
    monkeypatch.setattr(receiver, "load", lambda path: inventory)
    monkeypatch.setattr(receiver, "queue_url", lambda prefix: "http://cola-local")

    def publish_caido(url, event):
        raise ClientError({"Error": {"Code": "ServiceUnavailable",
                                     "Message": "SQS caído"}}, "SendMessage")

    monkeypatch.setattr(receiver, "publish", publish_caido)
    cuerpo = json.dumps(_event(event_id="evt-f13-001")).encode()
    respuesta = TestClient(receiver.app).post(
        "/webhook", content=cuerpo, headers={"X-Webhook-Signature": _header(cuerpo)})
    assert respuesta.status_code == 503
    assert "message_id" not in respuesta.json()


# --- F09–F12, F15: orquestación durable -----------------------------------

def test_f09_simulate_failures_2_backoff_y_repara(ledger_temporal):
    """Dos intentos fallan antes de reparar y el tercero completa el circuito."""
    llamadas, leer_ledger = ledger_temporal
    evento = _event(event_id="evt-f09-001", simulate_failures=2)
    for intento in (1, 2):
        with pytest.raises(RetryableError):
            remediation.handle(evento)
        assert leer_ledger()["evt-f09-001"]["attempts"] == intento
        assert llamadas["repair"] == []  # No hay efecto externo antes de reparar.
    remediation.handle(evento)
    entrada = leer_ledger()["evt-f09-001"]
    assert entrada["attempts"] == 3
    assert entrada["repair_done"] is True and entrada["notified"] is True
    assert entrada["status"] == "done"
    assert len(llamadas["repair"]) == 1 and len(llamadas["notify"]) == 1


def test_f11_evento_repetido_no_repara_ni_avisa(ledger_temporal):
    """Repetir un evento completo no vuelve a reparar ni a notificar."""
    llamadas, leer_ledger = ledger_temporal
    evento = _event(event_id="evt-f11-001")
    remediation.handle(evento)
    remediation.handle(evento)  # Relee el ledger: equivale a reiniciar el worker.
    entrada = leer_ledger()["evt-f11-001"]
    assert entrada["attempts"] == 2
    assert len(llamadas["repair"]) == 1
    assert len(llamadas["notify"]) == 1
    assert entrada["status"] == "done"


def test_f12_caida_entre_repair_y_notify(ledger_temporal, monkeypatch):
    """Un aviso fallido deja notified=False y no repite la reparación."""
    llamadas, leer_ledger = ledger_temporal
    evento = _event(event_id="evt-f12-001")
    intentos = {"n": 0}

    def notify_que_falla(text):
        llamadas["notify"].append(text)
        if intentos["n"] == 0:
            intentos["n"] = 1
            raise RetryableError("falla transitoria de aviso")

    monkeypatch.setattr(remediation, "notify", notify_que_falla)
    with pytest.raises(RetryableError):
        remediation.handle(evento)
    entrada = leer_ledger()["evt-f12-001"]
    assert entrada["repair_done"] is True and entrada["notified"] is False
    remediation.handle(evento)  # Reanuda solo el aviso pendiente.
    entrada = leer_ledger()["evt-f12-001"]
    assert entrada["repair_done"] is True and entrada["notified"] is True
    assert len(llamadas["repair"]) == 1  # La reparación no se repitió.
    assert len(llamadas["notify"]) == 2  # El aviso pendiente sí se reintentó.


def test_f15_conflicto_event_id(ledger_temporal):
    """El mismo event_id con otro contenido se rechaza sin alterar el original."""
    llamadas, leer_ledger = ledger_temporal
    original = _event(event_id="evt-f15-001")
    remediation.handle(original)
    antes = leer_ledger()
    cambiado = {**original, "occurred_at": "2026-10-23T00:00:00Z"}
    with pytest.raises(PermanentError):
        remediation.handle(cambiado)
    assert leer_ledger() == antes  # No se incrementa ni se reescribe el original.
    assert len(llamadas["repair"]) == 1 and len(llamadas["notify"]) == 1


# --- F14: aviso con 429 ----------------------------------------------------

def test_f14_notify_429_y_luego_exito(monkeypatch):
    """Un 429 con Retry-After corto se espera una vez; el segundo POST confirma."""
    monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-ficticio-de-prueba")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-ficticio")
    respuestas = iter([
        _RespuestaFalsa(429, headers={"Retry-After": "1"}),
        _RespuestaFalsa(200, {"ok": True}),
    ])
    llamadas, esperas = [], []

    def post_falso(url, json=None, timeout=None):
        llamadas.append({"timeout": timeout, "texto": json["text"]})
        return next(respuestas)

    monkeypatch.setattr(notifications.requests, "post", post_falso)
    monkeypatch.setattr(notifications.time, "sleep",
                        lambda seconds: esperas.append(seconds))
    notifications.notify("evt-f14: reparado")
    assert len(llamadas) == 2  # Un único reintento acotado.
    assert esperas == [1]  # Respetó Retry-After una sola vez.
    assert llamadas[0]["timeout"] == (2, 5)

    monkeypatch.setattr(
        notifications.requests, "post",
        lambda url, json=None, timeout=None: _RespuestaFalsa(
            429, headers={"Retry-After": "120"}))  # Excede el máximo de 30 s.
    with pytest.raises(RetryableError):
        notifications.notify("evt-f14: pendiente")


# --- F16: rotación gzip ----------------------------------------------------

def test_f16_rotacion_gzip_json_valido(tmp_path):
    """Fuerza rotación con tamaño reducido y parsea cada línea del .gz."""
    destino = tmp_path / "rotacion.jsonl"
    handler = RotatingFileHandler(destino, maxBytes=250, backupCount=2,
                                  encoding="utf-8")
    handler.setFormatter(JSONFormatter())  # Mismo contrato que el logger real.
    handler.namer, handler.rotator = gzip_name, gzip_rotate
    logger = logging.getLogger("test-f16")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        for nivel in ("debug", "info", "warning", "error", "critical"):
            getattr(logger, nivel)("evento de rotación", extra={
                "event_id": "evt-f16-001",
                "message_id": None,
                "resource_id": "node-01",
                "attempt": 1,
                "action": "rotacion",
                "status": nivel,
            })
    finally:
        handler.close()
        logger.removeHandler(handler)

    comprimidos = sorted(tmp_path.glob("*.gz"))
    assert comprimidos, "la rotación no produjo ningún histórico gzip"
    filas = []
    for archivo in comprimidos:
        with gzip.open(archivo, "rt", encoding="utf-8") as historico:
            filas.extend(json.loads(linea) for linea in historico if linea.strip())
    assert filas  # El histórico conserva registros legibles.
    for fila in filas:
        assert fila["timestamp"].endswith("+00:00")  # UTC explícito.
        assert fila["event_id"] == "evt-f16-001"
        assert fila["message_id"] is None  # Campo no aplicable como null.
        assert fila["action"] == "rotacion"
    assert not any("token" in json.dumps(fila).lower() for fila in filas)


# --- Integración con LocalStack (F01, F02, F03, F08, F10) ------------------

def _requiere_localstack() -> None:
    """Salta el caso si el emulador no responde en el endpoint configurado."""
    try:
        client("sqs").list_queues()
    except (BotoCoreError, ClientError):
        pytest.skip("requiere LocalStack")


def _inventario_real() -> dict:
    """Lee el inventario del grupo o salta el caso si setup-demo no corrió."""
    ruta = Path("data/inventory.json")
    if not ruta.exists():
        pytest.skip("requiere 'setup-demo' (data/inventory.json)")
    inventario = json.loads(ruta.read_text(encoding="utf-8"))
    if "node-01" not in inventario or "bucket-01" not in inventario:
        pytest.skip("requiere inventario con node-01 y bucket-01")
    return inventario


def _borrar_cola(sqs, url: str) -> None:
    """Elimina una cola efímera del propio test, informando un fallo de limpieza."""
    try:
        sqs.delete_queue(QueueUrl=url)
    except ClientError as error:
        print("limpieza de cola:", error.response.get("Error", {}).get("Code"))


def _estado_instancia(instance_id: str) -> str | None:
    """Consulta el estado real de la instancia sin suponer el resultado."""
    reservas = client("ec2").describe_instances(
        InstanceIds=[instance_id])["Reservations"]
    instancias = [fila for reserva in reservas for fila in reserva["Instances"]]
    return instancias[0]["State"]["Name"] if instancias else None


def _redrive_a_dlq() -> None:
    """Publica un veneno en una cola efímera y espera su derivación real a la DLQ."""
    _requiere_localstack()
    prefijo = f"efimero-{uuid.uuid4().hex[:8]}"
    sqs = client("sqs")
    work, dead = ensure_queues(prefijo)
    try:
        arn = sqs.get_queue_attributes(
            QueueUrl=dead, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sqs.set_queue_attributes(QueueUrl=work, Attributes={
            "RedrivePolicy": json.dumps({
                "deadLetterTargetArn": arn, "maxReceiveCount": "1"}),
        })  # Límite bajo para no esperar varias ventanas de visibilidad.
        sqs.send_message(QueueUrl=work, MessageBody="{veneno")
        logger = _LoggerFalso()
        worker.run(work, lambda event: pytest.fail("el veneno no debe ejecutarse"),
                   logger, once=True)
        time.sleep(3)  # Deja vencer la visibilidad programada por retry_later.
        worker.run(work, lambda event: pytest.fail("el veneno no debe ejecutarse"),
                   logger, once=True)  # Esta recepción dispara el redrive.
        recibido = None
        limite = time.time() + 30
        while recibido is None and time.time() < limite:
            mensajes = sqs.receive_message(
                QueueUrl=dead, MaxNumberOfMessages=1,
                WaitTimeSeconds=1).get("Messages", [])
            if mensajes:
                recibido = mensajes[0]
            else:
                sqs.receive_message(QueueUrl=work, MaxNumberOfMessages=1,
                                    WaitTimeSeconds=1)  # Empuja el redrive si falta.
                time.sleep(1)
        assert recibido is not None, "el mensaje venenoso no llegó a la DLQ"
        assert recibido["Body"] == "{veneno"  # Se conserva el cuerpo original.
        sqs.delete_message(QueueUrl=dead,
                           ReceiptHandle=recibido["ReceiptHandle"])  # Limpieza.
    finally:
        _borrar_cola(sqs, work)
        _borrar_cola(sqs, dead)


@pytest.mark.integracion
def test_f01_init_queues_idempotente():
    """Dos init-queues con el mismo prefijo reutilizan colas sin duplicarlas."""
    _requiere_localstack()
    prefijo = f"efimero-{uuid.uuid4().hex[:8]}"
    sqs = client("sqs")
    creadas: list[str] = []
    try:
        primera = ensure_queues(prefijo)
        creadas.extend(primera)
        segunda = ensure_queues(prefijo)
        creadas.extend(segunda)
        assert primera == segunda  # Mismas URLs devueltas por el SDK.
        nombres = [url.rsplit("/", 1)[-1]
                   for url in sqs.list_queues().get("QueueUrls", [])]
        assert nombres.count(f"{prefijo}-events") == 1
        assert nombres.count(f"{prefijo}-dlq") == 1
        atributos = sqs.get_queue_attributes(
            QueueUrl=primera[0], AttributeNames=["RedrivePolicy"])["Attributes"]
        assert json.loads(atributos["RedrivePolicy"])["maxReceiveCount"] == "3"
    finally:
        for url in dict.fromkeys(creadas):
            _borrar_cola(sqs, url)


@pytest.mark.integracion
def test_f02_reparacion_ec2_converge():
    """Un nodo detenido vuelve a running y repetir repair no lo reinicia."""
    _requiere_localstack()
    recurso = _inventario_real()["node-01"]
    inject_fault("node-01")  # Falla verificada por SDK antes de reparar.
    assert remediation.repair(recurso, "instance.stopped") == "repaired"
    assert _estado_instancia(recurso["id"]) == "running"
    assert remediation.repair(recurso, "instance.stopped") == "already_ok"


@pytest.mark.integracion
def test_f03_reparacion_tags_bucket():
    """Un bucket sin Owner recupera las etiquetas obligatorias y conserva el resto."""
    _requiere_localstack()
    recurso = _inventario_real()["bucket-01"]
    inject_fault("bucket-01")
    assert remediation.repair(recurso, "bucket.tags_missing") == "repaired"
    s3 = client("s3")
    actuales = {tag["Key"]: tag["Value"]
                for tag in s3.get_bucket_tagging(Bucket=recurso["id"])["TagSet"]}
    assert all(actuales.get(key) == value
               for key, value in recurso["expected_tags"].items())
    assert remediation.repair(recurso, "bucket.tags_missing") == "already_ok"


@pytest.mark.integracion
def test_f08_json_corrupto_en_sqs_worker_sigue():
    """Un cuerpo inyectado directo a SQS se reintenta sin ACK y sin matar el worker."""
    _requiere_localstack()
    prefijo = f"efimero-{uuid.uuid4().hex[:8]}"
    sqs = client("sqs")
    work, dead = ensure_queues(prefijo)
    try:
        sqs.send_message(QueueUrl=work, MessageBody="{veneno")
        logger = _LoggerFalso()
        worker.run(work, lambda event: pytest.fail("no debe ejecutarse"),
                   logger, once=True)
        assert not any(row.get("action") == "acknowledge"
                       for row in logger.registros)
        assert any(row.get("action") == "retry_later"
                   for row in logger.registros)
    finally:
        _borrar_cola(sqs, work)
        _borrar_cola(sqs, dead)


@pytest.mark.integracion
def test_f10_redrive_a_dlq():
    """Un mensaje venenoso persistente termina en la DLQ, no se confirma."""
    _redrive_a_dlq()


@pytest.mark.integracion
def test_redrive_with_localstack():
    """Comprueba DLQ efectiva usando el broker, no una lista simulada en Python.

    Returns:
        None si un mensaje venenoso termina en la DLQ y el worker sigue vivo.
    Side Effects:
        Crea colas de prueba exclusivas y las limpia después de guardar evidencia.
    Notes:
        Usa un prefijo efímero nuevo y no purga colas del laboratorio del grupo.
    """
    _redrive_a_dlq()  # Misma verificación real de F10 sobre un prefijo propio.
