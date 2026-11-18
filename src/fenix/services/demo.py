"""Laboratorio acotado de recursos. Integrante 2 prepara, integrante 6 verifica."""
import time  # Acota las consultas de verificación del fallo simulado.
from botocore.exceptions import ClientError
from fenix.config.settings import client
from fenix.services.cloud_support import (aws_tags, create_bucket, create_group,
                                          create_instance, mock_inputs)
from fenix.utils.errors import PermanentError, RetryableError, ValidationError
from fenix.utils.state import load, save
from fenix.utils.validation import identifier


def setup_demo(group: str) -> dict:
    """Prepara una instancia mock y un bucket a partir de identificadores estables.

    Args:
        group: ID validado del grupo; determina prefijos de los recursos.
    Returns:
        Inventario con aliases node-01 y bucket-01 y sus IDs reales del emulador.
    Raises:
        ClientError: Si el emulador no soporta una operación o falla su ejecución.
        NotImplementedError: Mientras falte TODO I2-C.
    Side Effects:
        Crea recursos de laboratorio etiquetados y data/inventory.json.
    Notes:
        Reutilizar por tags antes de crear y guardar IDs después de cada creación.
        Obtener una ImageId con describe_images; no copiar una AMI de AWS real.
    """
    # TODO I2-C: validar group; localizar VPC predeterminada mediante describe_vpcs.
    # Crear/reusar SG fenix-{group}-sg con tag ManagedBy=ppa2 y GroupId=group.
    # Usar describe_images y registrar la ImageId mock elegida en inventory.json.
    # Crear/reusar instancia por tags Project=fenix/GroupId/Name; ClientToken estable.
    # run_instances: MinCount=MaxCount=1, tipo t3.micro y SecurityGroupIds=[sg_id].
    # Crear/reusar bucket fenix-{group}-demo; región us-east-1 para este laboratorio.
    # Aplicar tags ManagedBy=ppa2, Project=fenix, GroupId=group y Owner=group.
    # Publicar inventario solo con recursos cuya propiedad haya sido verificada.
    group = identifier(group)  # Un ID inválido no debe convertirse en nombre de recurso.
    expected = {"ManagedBy": "ppa2", "Project": "fenix",
                "GroupId": group, "Owner": group}
    sg_name = f"fenix-{group}-sg"
    instance_name = f"fenix-{group}-node-01"
    bucket_name = f"fenix-{group}-bucket-01"
    ec2, s3 = client("ec2"), client("s3")

    # 1. SG exclusivo del grupo, recuperado por nombre y verificado por tags.
    try:
        security_groups = ec2.describe_security_groups(Filters=[
            {"Name": "group-name", "Values": [sg_name]}])["SecurityGroups"]
    except ClientError as error:
        raise RetryableError("No se pudo consultar el SG del grupo.") from error
    security_groups = [row for row in security_groups
                       if row.get("GroupName") == sg_name]
    if len(security_groups) > 1:
        raise PermanentError("Hay más de un SG con el nombre del grupo.")
    sg_id = None
    if security_groups:
        found_tags = {tag["Key"]: tag["Value"]
                      for tag in security_groups[0].get("Tags", [])}
        if any(found_tags.get(key) != value for key, value in expected.items()):
            raise PermanentError("El SG existente no coincide con el inventario.")
        sg_id = security_groups[0]["GroupId"]

    # 2. Instancia mock, recuperada por tags Project=fenix y GroupId.
    try:
        reservations = ec2.describe_instances(Filters=[
            {"Name": "tag:Project", "Values": ["fenix"]},
            {"Name": "tag:GroupId", "Values": [group]},
            {"Name": "instance-state-name",
             "Values": ["pending", "running", "stopping", "stopped"]},
        ])["Reservations"]
    except ClientError as error:
        raise RetryableError("No se pudo consultar la instancia del grupo.") from error
    instances = []
    for reservation in reservations:  # Se verifica también del lado del cliente.
        for candidate in reservation.get("Instances", []):
            candidate_tags = {tag["Key"]: tag["Value"]
                              for tag in candidate.get("Tags", [])}
            if (candidate_tags.get("Project") == "fenix"
                    and candidate_tags.get("GroupId") == group
                    and candidate.get("State", {}).get("Name")
                    not in ("terminated", "shutting-down")):
                instances.append(candidate)
    if len(instances) > 1:
        raise PermanentError("Hay más de una instancia para el grupo.")
    instance_id = None
    if instances:
        instance = instances[0]
        instance_tags = {tag["Key"]: tag["Value"]
                         for tag in instance.get("Tags", [])}
        if any(instance_tags.get(key) != value for key, value in expected.items()):
            raise PermanentError("La instancia existente no coincide con el inventario.")
        instance_id = instance["InstanceId"]
        groups = instance.get("SecurityGroups") or []
        instance_sg = groups[0].get("GroupId") if groups else None
        if instance_sg and sg_id and instance_sg != sg_id:
            raise PermanentError("La instancia no usa el SG exclusivo del grupo.")
        sg_id = sg_id or instance_sg
    if instance_id is not None and sg_id is None:
        raise PermanentError("No se pudo determinar el SG de la instancia.")

    # 3. Crear solo lo ausente; las recetas de apoyo hacen un paso API cada una.
    vpc_id = image_id = None
    if instance_id is None or sg_id is None:
        vpc_id, image_id = mock_inputs()  # Falla claro si falta el entorno mock.
    if sg_id is None:
        sg_id = create_group(sg_name, vpc_id, expected)
    if instance_id is None:
        instance_id = create_instance(image_id, sg_id,
                                      f"fenix-{group}-node-01",
                                      {**expected, "Name": instance_name})
        try:
            described = ec2.describe_instances(InstanceIds=[instance_id])
        except ClientError as error:
            raise RetryableError("No se pudo verificar la instancia creada.") from error
        created = described.get("Reservations", [])
        created_tags = {}
        if created and created[0].get("Instances"):
            created_tags = {tag["Key"]: tag["Value"]
                            for tag in created[0]["Instances"][0].get("Tags", [])}
        if any(created_tags.get(key) != value for key, value in expected.items()):
            raise RetryableError("La instancia creada no verifica sus etiquetas.")

    inventory = load("data/inventory.json")
    if not isinstance(inventory, dict):
        raise PermanentError("El inventario existente no es un objeto JSON.")
    inventory["node-01"] = {
        "type": "ec2",
        "id": instance_id,
        "security_group_id": sg_id,
        "expected_tags": dict(expected),
    }
    save("data/inventory.json", inventory)  # Persiste el ID antes de seguir.

    # 4. Bucket estable por nombre; un fallo de etiquetado previo se completa aquí.
    try:
        tag_set = s3.get_bucket_tagging(Bucket=bucket_name)["TagSet"]
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in ("NoSuchBucket", "NoSuchBucketError", "404", "NotFound"):
            create_bucket(bucket_name, expected)
            try:
                tag_set = s3.get_bucket_tagging(Bucket=bucket_name)["TagSet"]
            except ClientError as verify_error:
                raise RetryableError(
                    "No se verificaron las etiquetas del bucket creado.") from verify_error
        elif code == "NoSuchTagSet":
            tag_set = []  # El bucket existe sin etiquetas: se completan abajo.
        else:
            raise RetryableError(
                "No se pudieron leer las etiquetas del bucket.") from error
    current = {tag["Key"]: tag["Value"] for tag in tag_set}
    if any(current.get(key) not in (None, value) for key, value in expected.items()):
        raise PermanentError("El bucket existente no coincide con el inventario.")
    if any(current.get(key) != value for key, value in expected.items()):
        merged = dict(current)
        merged.update(expected)  # Las ajenas se conservan; las obligatorias mandan.
        try:
            s3.put_bucket_tagging(Bucket=bucket_name,
                                  Tagging={"TagSet": aws_tags(merged)})
            verified = s3.get_bucket_tagging(Bucket=bucket_name)["TagSet"]
        except ClientError as error:
            raise RetryableError(
                "No se pudieron completar las etiquetas del bucket.") from error
        final = {tag["Key"]: tag["Value"] for tag in verified}
        if any(final.get(key) != value for key, value in expected.items()):
            raise RetryableError("Las etiquetas del bucket no se verificaron.")

    inventory = load("data/inventory.json")
    if not isinstance(inventory, dict):
        raise PermanentError("El inventario existente no es un objeto JSON.")
    inventory["bucket-01"] = {
        "type": "s3",
        "id": bucket_name,
        "expected_tags": dict(expected),
    }
    save("data/inventory.json", inventory)
    return inventory


def inject_fault(alias: str) -> None:
    """Detiene el nodo o elimina una etiqueta del bucket de laboratorio.

    Args:
        alias: node-01 o bucket-01 presente en inventory.json.
    Returns:
        None cuando el fallo simulado se verifica mediante el SDK.
    Raises:
        ValidationError: Si el alias no pertenece al inventario.
        NotImplementedError: Mientras falte TODO I6-A.
    Side Effects:
        Modifica únicamente el recurso de prueba de ese grupo.
    """
    # TODO I6-A: resolver el alias en inventario; jamás recibir InstanceId libre.
    # node-01: stop_instances y comprobar stopped, con consultas limitadas.
    # bucket-01: quitar Owner conservando el resto de etiquetas.
    # Esta función fabrica el problema; no debe enviar el evento automáticamente.
    inventory = load("data/inventory.json")
    if not isinstance(inventory, dict):
        raise PermanentError("El inventario existente no es un objeto JSON.")
    record = inventory.get(alias)  # Solo se acepta lo inventariado por el grupo.
    if not isinstance(record, dict):
        raise ValidationError("El alias no pertenece al inventario del grupo.")
    resource_type = record.get("type")
    resource_id = record.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        raise PermanentError("El recurso inventariado no tiene un ID utilizable.")

    if alias == "node-01":  # Detener es la falla que el worker debe reparar.
        if resource_type != "ec2":
            raise PermanentError("node-01 no es una instancia en el inventario.")
        ec2 = client("ec2")
        try:
            ec2.stop_instances(InstanceIds=[resource_id])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "InvalidInstanceID.NotFound":
                raise PermanentError("La instancia inventariada no existe.") from error
            raise RetryableError("No se pudo detener la instancia.") from error
        state = None
        for attempt in range(10):  # Consultas acotadas: el mock suele cambiar rápido.
            try:
                described = ec2.describe_instances(InstanceIds=[resource_id])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == "InvalidInstanceID.NotFound":
                    raise PermanentError("La instancia inventariada no existe.") from error
                raise RetryableError("No se pudo verificar la instancia.") from error
            instances = [instance for reservation in described.get("Reservations", [])
                         for instance in reservation.get("Instances", [])]
            if not instances:  # Algunos emuladores responden vacío en vez de error.
                raise PermanentError("La instancia inventariada no existe.")
            state = instances[0].get("State", {}).get("Name")
            if state == "stopped":
                break
            if state in ("terminated", "shutting-down"):
                raise PermanentError(f"La instancia está en estado {state}.")
            if attempt < 9:  # Última vuelta sin esperar; el límite ya está agotado.
                time.sleep(0.5)
        if state != "stopped":  # Se verifica por SDK, no se asume el cambio.
            raise RetryableError("La instancia todavía no figura detenida.")
        return

    if alias == "bucket-01":  # Quitar Owner conserva las etiquetas ajenas a la política.
        if resource_type != "s3":
            raise PermanentError("bucket-01 no es un bucket en el inventario.")
        s3 = client("s3")
        try:
            tag_set = s3.get_bucket_tagging(Bucket=resource_id)["TagSet"]
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in ("NoSuchBucket", "NoSuchBucketError", "404", "NotFound"):
                raise PermanentError("El bucket inventariado no existe.") from error
            if code == "NoSuchTagSet":
                return  # Sin etiquetas no hay Owner: la falla ya está presente.
            raise RetryableError("No se pudieron leer las etiquetas del bucket.") from error
        current = {tag["Key"]: tag["Value"] for tag in tag_set}
        if "Owner" not in current:
            return  # Repetir la inyección no cambia el recurso.
        remaining = {key: value for key, value in current.items() if key != "Owner"}
        try:
            if remaining:  # Put reemplaza el conjunto: se envía el resto intacto.
                s3.put_bucket_tagging(Bucket=resource_id,
                                      Tagging={"TagSet": aws_tags(remaining)})
                verified = s3.get_bucket_tagging(Bucket=resource_id)["TagSet"]
            else:  # S3 no acepta un TagSet vacío; se eliminan todas las etiquetas.
                s3.delete_bucket_tagging(Bucket=resource_id)
                verified = []
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "NoSuchTagSet":
                verified = []
            else:
                raise RetryableError("No se pudo quitar la etiqueta Owner.") from error
        final = {tag["Key"]: tag["Value"] for tag in verified}
        if "Owner" in final:  # El fallo simulado se comprueba leyendo el recurso.
            raise RetryableError("La etiqueta Owner todavía figura en el bucket.")
        return

    raise PermanentError("El alias no corresponde a un recurso de prueba conocido.")
