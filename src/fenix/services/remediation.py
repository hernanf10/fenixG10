"""Acciones convergentes a completar por integrante 3 y coordinadas por 5."""
from botocore.exceptions import ClientError
from fenix.config.settings import client
from fenix.utils.state import load, save
from fenix.services.events import validate
from fenix.services.notifications import notify
from fenix.utils.errors import RetryableError, PermanentError


def repair(resource: dict, event_type: str) -> str:
    """Acerca un recurso autorizado a su estado deseado y verifica el resultado.

    Args:
        resource: Registro de inventario con type/id y etiquetas esperadas.
        event_type: instance.stopped o bucket.tags_missing ya validado.
    Returns:
        "repaired" si cambió el estado; "already_ok" si ya cumplía la política.
    Raises:
        RetryableError: La API falla temporalmente o el estado final aún no se observa.
        PermanentError: Recurso inexistente o configuración no autorizada.
        NotImplementedError: Mientras no se complete TODO I3-C.
    Side Effects:
        Arranca una instancia EC2 mock detenida o restaura tags de un bucket S3.
    Notes:
        No usar reboot_instances: repetir un reinicio no es convergencia.
        Un estado pending requiere otra recepción, sin esperas largas en el worker.
    """
    # TODO I3-C EC2: describe_instances por el InstanceId leído del inventario.
    # running -> already_ok; stopped -> start_instances; pending -> RetryableError.
    # Volver a consultar y exigir running; stopping/terminated -> error explicado.
    # TODO I3-C S3: get_bucket_tagging; solo NoSuchTagSet equivale a tags vacías.
    # Combinar tags actuales con obligatorias; put_bucket_tagging si hay desvío.
    # Volver a leer tags; preservar etiquetas ajenas a la política.
    # Clasificar ClientError por código: throttling/5xx transitorio, no todo ClientError.
    if not isinstance(resource, dict):
        raise PermanentError("El recurso del inventario no es válido.")
    resource_type = resource.get("type")
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        raise PermanentError("El recurso del inventario no tiene un id utilizable.")

    if event_type == "instance.stopped":
        if resource_type != "ec2":
            raise PermanentError("instance.stopped no corresponde a este recurso.")
        ec2 = client("ec2")  # El ID proviene solo del inventario, nunca del evento.
        try:
            described = ec2.describe_instances(InstanceIds=[resource_id])
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "InvalidInstanceID.NotFound":
                raise PermanentError("La instancia inventariada no existe.") from error
            raise RetryableError("No se pudo consultar la instancia.") from error
        instances = [instance for reservation in described.get("Reservations", [])
                     for instance in reservation.get("Instances", [])]
        if not instances:  # Algunos emuladores responden vacío en vez de error.
            raise PermanentError("La instancia inventariada no existe.")
        state = instances[0].get("State", {}).get("Name")
        if state == "running":
            return "already_ok"  # Una repetición no vuelve a arrancar.
        if state == "pending":
            raise RetryableError("La instancia está pendiente; se reintentará.")
        if state in ("stopping", "shutting-down", "terminated"):
            raise PermanentError(f"La instancia está en estado {state}; no se arranca.")
        if state != "stopped":
            raise PermanentError("La instancia está en un estado no admitido.")
        try:
            ec2.start_instances(InstanceIds=[resource_id])  # Convergencia, no reboot.
            described = ec2.describe_instances(InstanceIds=[resource_id])
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "InvalidInstanceID.NotFound":
                raise PermanentError("La instancia inventariada no existe.") from error
            raise RetryableError("No se pudo arrancar la instancia.") from error
        instances = [instance for reservation in described.get("Reservations", [])
                     for instance in reservation.get("Instances", [])]
        state = instances[0].get("State", {}).get("Name") if instances else None
        if state != "running":  # El cambio se confirma por API, no por suposición.
            raise RetryableError("La instancia todavía no está running; reintentar.")
        return "repaired"

    if event_type == "bucket.tags_missing":
        if resource_type != "s3":
            raise PermanentError("bucket.tags_missing no corresponde a este recurso.")
        expected = resource.get("expected_tags")
        if not isinstance(expected, dict):
            raise PermanentError("El inventario no define etiquetas esperadas válidas.")
        s3 = client("s3")
        try:
            described = s3.get_bucket_tagging(Bucket=resource_id)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "NoSuchTagSet":
                described = {"TagSet": []}  # Sin etiquetas no es un fallo de lectura.
            elif code == "NoSuchBucket":
                raise PermanentError("El bucket inventariado no existe.") from error
            else:
                raise RetryableError("No se pudieron leer las etiquetas.") from error
        current = {tag["Key"]: tag["Value"] for tag in described.get("TagSet", [])}
        merged = dict(current)  # Se conservan etiquetas ajenas a la política.
        merged.update({str(key): str(value) for key, value in expected.items()})
        if merged == current:
            return "already_ok"  # Las obligatorias ya estaban presentes.
        try:
            s3.put_bucket_tagging(Bucket=resource_id, Tagging={
                "TagSet": [{"Key": key, "Value": value}
                           for key, value in merged.items()],
            })
            verified = s3.get_bucket_tagging(Bucket=resource_id)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "NoSuchBucket":
                raise PermanentError("El bucket inventariado no existe.") from error
            raise RetryableError("No se pudieron escribir las etiquetas.") from error
        actual = {tag["Key"]: tag["Value"] for tag in verified.get("TagSet", [])}
        if any(actual.get(key) != value for key, value in merged.items()):
            raise RetryableError("Las etiquetas no se verificaron tras la escritura.")
        return "repaired"

    raise PermanentError("event_type no admitido para reparación.")


def handle(event: dict) -> None:
    """Completa reparación y notificación usando un registro durable por evento.

    Args:
        event: Objeto recibido de SQS, que se vuelve a validar aquí.
    Returns:
        None solo cuando reparación y aviso quedaron registrados como exitosos.
    Raises:
        ValidationError: Si el evento fue inyectado directamente a SQS y no es válido.
        RetryableError: Si la simulación o una dependencia temporal falla.
        PermanentError: Conflicto de event_id o recurso no autorizado.
        NotImplementedError: Mientras falte TODO I5-B.
    Side Effects:
        Actualiza recursos, data/events.json y el canal de comunicación.
    Notes:
        Un solo proceso modifica el ledger. Un timeout al notificar puede repetir
        un aviso: no existe una transacción común entre JSON, AWS y Telegram/Slack.
    """
    # TODO I5-B: cargar inventario y validar; cargar ledger por event_id.
    # Vincular event_id al contenido del evento; rechazar reutilización con otro contenido.
    # Guardar attempts incrementado antes de simular fallas; persiste entre reinicios.
    # Si attempts <= simulate_failures, lanzar RetryableError sin modificar recursos.
    # Si repair_done es falso: repair; guardar resultado y repair_done=True.
    # Si notified es falso: notify resumen; guardar notified=True después del éxito.
    # Si ambas etapas ya estaban completas, retornar sin efectos repetidos.
    # Cada save debe preservar el resto del ledger; no sobrescribir otros eventos.
    inventory = load("data/inventory.json")
    validated = validate(event, inventory)  # Copia validada; no modifica la entrada.

    path = "data/events.json"
    ledger = load(path)
    if not isinstance(ledger, dict):
        raise PermanentError("El ledger de eventos no es un objeto JSON.")
    event_id = validated["event_id"]
    entry = ledger.get(event_id)
    if entry is not None:
        if not isinstance(entry, dict):
            raise PermanentError("El ledger contiene una entrada de evento inválida.")
        if entry.get("contenido") != validated:
            raise PermanentError("conflicto de event_id")  # No se altera el original.
    else:
        entry = {
            "contenido": validated,
            "attempts": 0,
            "repair_done": False,
            "repair_result": None,
            "notified": False,
            "status": "pending",
        }
        ledger[event_id] = entry  # Cada save conserva las demás entradas.

    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    save(path, ledger)  # El intento queda durable antes de simular o reparar.

    if entry["attempts"] <= validated["simulate_failures"]:
        entry["status"] = "retry"
        save(path, ledger)
        raise RetryableError("falla simulada")  # Sin efectos sobre el recurso.

    if not entry.get("repair_done"):
        resource = inventory[validated["resource_id"]]  # Solo el alias inventariado.
        entry["repair_result"] = repair(resource, validated["event_type"])
        entry["repair_done"] = True
        entry["status"] = "repaired"
        save(path, ledger)  # Verificado antes de continuar con el aviso.

    if not entry.get("notified"):
        summary = (
            f"Fénix reparación: event_id={event_id} "
            f"resource_id={validated['resource_id']} "
            f"event_type={validated['event_type']} "
            f"resultado={entry.get('repair_result')}"
        )  # Resumen público, sin tokens, URLs privadas ni cuerpo completo.
        notify(summary)  # Un timeout aquí deja notified=False para reintentar.
        entry["notified"] = True
        entry["status"] = "done"
        save(path, ledger)  # Solo después del éxito confirmado del proveedor.

    # Si ambas etapas ya estaban completas, no se repiten reparación ni aviso.
