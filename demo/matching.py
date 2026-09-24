"""Conservative matching for the three distinct demonstration processes."""


def _value(fields, key):
    return fields.get(key, {}).get("value")


def _payload_check(scenario, robot, checks):
    cargo_key = "pallet_mass" if scenario.slug == "warehouse" else "container_mass"
    cargo_mass = _value(scenario.parameters, cargo_key)
    capacity = _value(robot, "payload")
    if cargo_mass is None or capacity is None:
        checks.append(("requires_verification", "Неизвестна масса груза или грузоподъёмность робота."))
    elif capacity < cargo_mass:
        checks.append(("reject", f"Грузоподъёмность {capacity} кг меньше массы груза {cargo_mass} кг."))
    else:
        checks.append(("fit", f"Грузоподъёмность {capacity} кг покрывает массу груза {cargo_mass} кг."))


def _warehouse_checks(scenario, robot, checks):
    width = _value(robot, "width")
    route_widths = [edge.get("width_m") for edge in scenario.topology["edges"]]
    if width is None or not route_widths or any(value is None for value in route_widths):
        checks.append(("requires_verification", "Неизвестна ширина робота или складского прохода."))
    elif width > min(route_widths):
        checks.append(("reject", f"Ширина робота {width} м превышает узкий проход {min(route_widths)} м."))
    else:
        checks.append(("fit", f"Ширина робота {width} м проходит через проезд {min(route_widths)} м."))

    # Total robot mass (kg) cannot be compared with a floor area rating (kg/m²).
    # Contact pressure, wheel loads and the actual covering need their own contract.
    checks.append((
        "requires_verification",
        "Нагрузка на пол не подтверждена: нужны контактное давление робота и допуск покрытия.",
    ))


def _airport_checks(scenario, robot, checks):
    required_zones = {edge["requires_access"] for edge in scenario.topology["edges"] if edge.get("requires_access")}
    zones = _value(robot, "access_zones")
    if zones is None:
        checks.append(("requires_verification", "Неизвестны зоны доступа демонстрационной модели."))
    elif not required_zones.issubset(set(zones)):
        checks.append(("reject", "Модель не заявлена для всех контролируемых зон маршрута."))
    else:
        checks.append(("fit", "Заявленные зоны модели покрывают условный маршрут."))

    permission = _value(scenario.parameters, "access_permission")
    if permission is None:
        checks.append(("requires_verification", "Доступ к контролируемой зоне должен подтвердить владелец аэропорта."))
    elif permission is False:
        checks.append(("reject", "Доступ к контролируемой зоне запрещён."))
    else:
        checks.append(("fit", "Разрешение на доступ указано в сценарии."))


def _hospital_checks(scenario, robot, checks):
    if any(node.get("flow") != "clean" for node in scenario.topology["nodes"]):
        checks.append(("reject", "Маршрут пересекает зону вне чистого потока."))
    clean_transport = _value(robot, "clean_transport")
    if clean_transport is None:
        checks.append(("requires_verification", "Санитарная совместимость чистого потока неизвестна."))
    elif clean_transport is False:
        checks.append(("reject", "Модель не совместима с чистым потоком."))
    else:
        checks.append(("fit", "Модель заявлена для условного чистого потока."))

    lift_compatible = _value(robot, "lift_compatible")
    lift_wait = _value(scenario.parameters, "lift_wait")
    if lift_compatible is False:
        checks.append(("reject", "Модель несовместима с лифтом."))
    elif lift_compatible is None or lift_wait is None:
        checks.append(("requires_verification", "Не подтверждены совместимость с лифтом и время ожидания лифта."))
    else:
        checks.append(("fit", "Лифт и его ожидание указаны в сценарии."))


def _critical_evidence(scenario, robot):
    cargo_key = "pallet_mass" if scenario.slug == "warehouse" else "container_mass"
    fields = [scenario.topology, scenario.parameters.get(cargo_key, {}), robot["payload"]]
    if scenario.slug == "warehouse":
        fields.append(robot["width"])
    elif scenario.slug == "airport":
        fields.extend((scenario.parameters.get("access_permission", {}), robot["access_zones"]))
    elif scenario.slug == "hospital":
        fields.extend((
            scenario.parameters.get("lift_wait", {}),
            robot["clean_transport"],
            robot["lift_compatible"],
        ))
    return (
        scenario.data_status == "verified"
        and robot.get("data_status") == "verified"
        and all(field.get("status") == "verified" for field in fields)
    )


def evaluate_match(scenario, robot):
    """Return a conservative status and reasons without treating unknown as zero."""
    checks = []
    if scenario.slug not in robot["purposes"]:
        checks.append(("reject", "Модель не заявлена для этого предметного процесса."))
    _payload_check(scenario, robot, checks)
    if scenario.slug == "warehouse":
        _warehouse_checks(scenario, robot, checks)
    elif scenario.slug == "airport":
        _airport_checks(scenario, robot, checks)
    elif scenario.slug == "hospital":
        _hospital_checks(scenario, robot, checks)
    else:
        raise ValueError(f"Unknown object type: {scenario.slug}")

    if not _critical_evidence(scenario, robot):
        checks.append((
            "requires_verification",
            "Критичные характеристики и топология содержат синтетические допущения или неподтверждённые данные.",
        ))

    statuses = {status for status, _ in checks}
    if "reject" in statuses:
        status = "reject"
    elif "requires_verification" in statuses:
        status = "requires_verification"
    else:
        status = "fit"
    return {"robot": robot, "status": status, "reasons": [message for _, message in checks]}
