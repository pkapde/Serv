import re

from semantic_mcp.contracts import CannotAnswer, QuerySpec


def normalize_question(question):
    # Preserve case: filter values can be case-sensitive business identifiers.
    return " ".join(question.split())


def phrase_present(phrase, question):
    phrase = normalize_question(phrase).casefold()
    question = normalize_question(question).casefold()
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", question))


def explicit_variants(model, members, question):
    return [
        name
        for name in members
        if any(
            phrase_present(term, question)
            for term in [name, model.metrics[name]["label"], *model.metrics[name]["explicit_terms"]]
        )
    ]


def validate_filter(item, dimension):
    if item.operator not in {"in", "not_in"} and len(item.values) != 1:
        raise CannotAnswer("invalid_filter_value", "Comparison filters require one value.")
    for value in item.values:
        datatype = dimension["type"]
        if datatype in {"string", "date", "datetime"} and not isinstance(value, str):
            raise CannotAnswer("invalid_filter_value", f"{dimension['label']} requires a text value.")
        if datatype in {"integer", "decimal", "float"} and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise CannotAnswer("invalid_filter_value", f"{dimension['label']} requires a numeric value.")
        allowed = dimension["allowed_values"]
        if allowed is not None and value not in allowed:
            raise CannotAnswer(
                "invalid_filter_value", f"Invalid value for {dimension['label']}.", [str(v) for v in allowed]
            )


def validate_spec(spec: QuerySpec, model, question: str | None = None):
    spec = spec.model_copy(deep=True)
    defaults = []
    selected_names = set()
    for selected in spec.metrics:
        selected.name = model.canonical(selected.name, "metric")
        if selected.name in selected_names:
            raise CannotAnswer("unsupported_combination", "Duplicate metric selections are not supported.")
        selected_names.add(selected.name)
        metric = model.metrics[selected.name]
        group = metric["variant_group"]
        if group:
            members = model.groups[group]
            explicit = explicit_variants(model, members, question) if question is not None else []
            if len(explicit) > 1 and selected.basis == "default":
                raise CannotAnswer(
                    "ambiguous_no_default",
                    "Multiple variants were named; no default can replace them.",
                    members,
                )
            if selected.basis == "default":
                if explicit:
                    raise CannotAnswer(
                        "ambiguous_no_default", "A named variant cannot be replaced by a default.", explicit
                    )
                if not metric["is_default"]:
                    raise CannotAnswer(
                        "ambiguous_no_default", "This variant has no valid declared default.", members
                    )
                defaults.append(
                    {"variant_group": group, "chosen": selected.name, "reason": "declared default"}
                )
            elif question is not None:
                valid_terms = [selected.name, metric["label"], *metric["explicit_terms"]]
                if (
                    not selected.evidence
                    or not phrase_present(selected.evidence, question)
                    or not any(
                        normalize_question(term).casefold()
                        == normalize_question(selected.evidence).casefold()
                        for term in valid_terms
                    )
                ):
                    raise CannotAnswer(
                        "ambiguous_no_default", "The selected variant was not explicitly identified.", members
                    )
        elif selected.basis == "default":
            raise CannotAnswer("unknown_name", "Only a declared variant can be selected as a default.")
    seen_groups = set()
    for group in spec.group_by:
        group.name = model.canonical(group.name, "dimension")
        if group.name in seen_groups:
            raise CannotAnswer("unsupported_combination", "A dimension may appear only once in grouping.")
        seen_groups.add(group.name)
        if not model.dimensions[group.name]["is_time"] and group.grain:
            raise CannotAnswer("unsupported_combination", "Only time dimensions accept a time grain.")
    for item in spec.filters:
        item.dimension = model.canonical(item.dimension, "dimension")
        validate_filter(item, model.dimensions[item.dimension])
    for order in spec.order_by:
        if order.name.casefold() in model.metric_names:
            order.name = model.canonical(order.name, "metric")
        else:
            order.name = model.canonical(order.name, "dimension")
        if order.name not in selected_names | seen_groups:
            raise CannotAnswer(
                "unsupported_combination", "Ordering must reference a selected metric or grouping."
            )
    return spec, defaults
