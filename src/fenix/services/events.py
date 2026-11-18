"""Contrato de eventos de autorreparación; integrante 1."""
from fenix.utils.errors import ValidationError
from fenix.utils.validation import identifier, utc_text


def validate(event: dict, inventory: dict) -> dict:
    """Valida esquema cerrado y resuelve exclusivamente aliases autorizados.

    Args:
        event: JSON objeto con schema_version, event_id, event_type, resource_id,
            occurred_at y simulate_failures.
        inventory: Mapa alias -> recurso, preparado por setup-demo y revisado por el grupo.
    Returns:
        Copia validada del evento; no modifica inventory.
    Raises:
        ValidationError: Tipo, campo, timestamp o recurso no permitido.
        NotImplementedError: Mientras no se complete TODO I1-D.
    Notes:
        event_type solo permite instance.stopped y bucket.tags_missing.
        simulate_failures solo es de laboratorio: entero de 0 a 5, excluyendo bool.
    """
    # TODO I1-D: exigir dict, campos exactos y schema_version entero igual a 1.
    # Aplicar identifier a event_id/resource_id y utc_text a occurred_at.
    # Exigir resource_id en inventory; no aceptar ARN/InstanceId desde la petición.
    # Comprobar que el tipo del evento coincide con el tipo del recurso del inventario.
    # Rechazar bool como entero: type(value) is int en schema_version/simulate_failures.
    # Guardar el contenido asociado a event_id; el mismo ID con distinto evento es conflicto.
    if not isinstance(event, dict):
        raise ValidationError("El evento debe ser un objeto JSON.")
    required_fields = {
        "schema_version",
        "event_id",
        "event_type",
        "resource_id",
        "occurred_at",
        "simulate_failures",
    }
    if set(event) != required_fields:
        raise ValidationError("El evento debe tener exactamente los campos del contrato.")
    if type(event["schema_version"]) is not int or event["schema_version"] != 1:
        raise ValidationError("schema_version debe ser el entero 1.")
    allowed_types = {"instance.stopped": "ec2", "bucket.tags_missing": "s3"}
    event_type = event["event_type"]
    if not isinstance(event_type, str) or event_type not in allowed_types:
        raise ValidationError("event_type no permitido por el contrato.")
    event_id = identifier(event["event_id"])
    resource_id = identifier(event["resource_id"])
    utc_text(event["occurred_at"])
    simulate_failures = event["simulate_failures"]
    if type(simulate_failures) is not int or not 0 <= simulate_failures <= 5:
        raise ValidationError("simulate_failures debe ser un entero entre 0 y 5.")
    if not isinstance(inventory, dict) or resource_id not in inventory:
        raise ValidationError("resource_id no pertenece al inventario autorizado.")
    resource = inventory[resource_id]
    if not isinstance(resource, dict):
        raise ValidationError("El recurso del inventario no tiene el formato esperado.")
    if resource.get("type") != allowed_types[event_type]:
        raise ValidationError("event_type no corresponde al tipo del recurso inventariado.")
    return {
        "schema_version": event["schema_version"],
        "event_id": event_id,
        "event_type": event_type,
        "resource_id": resource_id,
        "occurred_at": event["occurred_at"],
        "simulate_failures": simulate_failures,
    }
